"""ZC90B anti-clone auth chip (``zc90b-auth.md``).

A GPIO-attached bit-banged slave on GPIO10 (clock) / GPIO5 (data). It owns no
MMIO range; instead it watches the GPIO output-latch and direction changes and
drives the GPIO5 input level, exactly as §4 prescribes.

Protocol (§2/§4): MSB-first, 8 clocks/byte, 3 challenge bytes out then 3
response bytes back. The challenge is delimited by the GPIO5 **direction**, not
by a raw clock count (§4): GPIO5 → output starts the challenge, GPIO5 → input
ends it. During the challenge phase GPIO5 is a firmware *output*; each GPIO10
falling edge (1→0) shifts the current GPIO5 output into the buffer. When GPIO5
switches back to input the response is computed from three 256-byte S-boxes read
out of the loaded firmware image (§3.2):

    B = tableB[c2 & 0xbe]
    C = tableC[(c1 ^ B) & 0xff]
    A = tableA[c3 & 0xd7]
    reply bits = MSB-first C, then B, then A   (R1=C, R2=B, R3=A)

GPIO5 is driven high for the ready handshake, then each response bit is
presented around the clock's rising edge and held through the firmware's
low-phase sample. The gate is fatal on the quiet boot path (§1), so answering
correctly is mandatory for boot.
"""

from __future__ import annotations

import logging

from ..peripheral import Peripheral
from .gpio import GpioBlock

log = logging.getLogger(__name__)

PIN_CLOCK = 10  # GPIO10 = clock (§2)
PIN_DATA = 5  # GPIO5 = data (bidirectional, §2)

#: S-box addresses inside the loaded PROG image (§3.2; generation-specific, §5).
TABLE_A_ADDR = 0x080B_0078  # indexed by c3 & 0xd7
TABLE_B_ADDR = 0x080B_0178  # indexed by c2 & 0xbe
TABLE_C_ADDR = 0x080B_0278  # indexed by (c1 ^ B) & 0xff
TABLE_SIZE = 256

MASK_A = 0xD7
MASK_B = 0xBE

_PHASE_CHALLENGE = "challenge"
_PHASE_RESPONSE = "response"


class Zc90bAuth(Peripheral):
    """The bit-banged ZC90B challenge/response device (no MMIO range)."""

    name = "zc90b"

    def __init__(self, gpio: GpioBlock) -> None:
        super().__init__()
        self._gpio = gpio
        self._table_a = b""
        self._table_b = b""
        self._table_c = b""
        self._reset_state()
        gpio.watch_output(PIN_CLOCK, self._on_clock)
        gpio.watch_direction(PIN_DATA, self._on_data_dir)

    def _reset_state(self) -> None:
        self._phase = _PHASE_CHALLENGE
        self._challenge_bits: list[int] = []
        self._response_bits: list[int] = []
        self._response_index = 0
        self._gpio.clear_input(PIN_DATA)

    def reset(self) -> None:
        self._reset_state()

    def load_tables(self, image_reader) -> None:
        """Read the three S-boxes from the loaded firmware image (§4 startup).

        ``image_reader(addr, size) -> bytes`` reads from emulated memory.
        """
        self._table_a = image_reader(TABLE_A_ADDR, TABLE_SIZE)
        self._table_b = image_reader(TABLE_B_ADDR, TABLE_SIZE)
        self._table_c = image_reader(TABLE_C_ADDR, TABLE_SIZE)
        for name, table in (("A", self._table_a), ("B", self._table_b), ("C", self._table_c)):
            if len(table) != TABLE_SIZE:
                raise ValueError(f"ZC90B S-box {name} short read ({len(table)} bytes)")

    # --- wire model ---------------------------------------------------------------------

    def _on_clock(self, _pin: int, old: int, new: int) -> None:
        if self._phase == _PHASE_CHALLENGE:
            if old == 1 and new == 0:
                # Falling edge latches the challenge bit: sample GPIO5 output level.
                self._challenge_bits.append(self._gpio.out_level(PIN_DATA))
        elif self._phase == _PHASE_RESPONSE:
            if old == 0 and new == 1:
                # Rising edge: present the next response bit, hold it through the
                # firmware's low-phase sample (§4).
                if self._response_index < len(self._response_bits):
                    self._gpio.set_input(PIN_DATA, self._response_bits[self._response_index])
                    self._response_index += 1

    def _on_data_dir(self, _pin: int, _old: int, new: int) -> None:
        # The challenge is delimited by the GPIO5 DIRECTION, not by a bit count
        # (§4: "detect this via the direction-register write that sets GPIO5 to
        # input"). GPIO5 → output = the firmware starting to clock the challenge
        # out; GPIO5 → input = challenge complete, ready-handshake, read the
        # response. Counting raw clock edges instead is wrong: the firmware emits
        # a spurious leading clock fall *before* it drives GPIO5 to output, which
        # a bit-count model would miscount as challenge bit 0 (Observed —
        # off-by-one challenge → wrong reply → the fatal power-off at 0x804e50c).
        if new == 0:
            # GPIO5 → output: (re)start a fresh challenge. Discard any bits
            # latched before this point (the pre-output spurious edge, or a prior
            # exchange / event-pump idling — §4 "require 24 fresh challenge bits
            # per exchange"). The event pump also idles GPIO5 this way, harmlessly
            # (it toggles no GPIO10 clock, so no challenge bits accumulate).
            self._phase = _PHASE_CHALLENGE
            self._challenge_bits = []
            self._response_index = 0
            self._gpio.clear_input(PIN_DATA)
        else:  # new == 1: GPIO5 → input
            if self._phase == _PHASE_CHALLENGE and len(self._challenge_bits) >= 24:
                # Challenge complete: compute the response (drives DATA = 1 for
                # the ready handshake and enters the response phase).
                self._compute_response()
            elif self._phase == _PHASE_RESPONSE:
                # Ready handshake re-assert: pull DATA high so the firmware's
                # ≤48-try poll loop breaks.
                self._gpio.set_input(PIN_DATA, 1)

    def _compute_response(self) -> None:
        # The challenge is the first 24 bits latched since GPIO5 went to output
        # (a trailing extra clock fall before the direction change is ignored).
        c1 = self._bits_to_byte(self._challenge_bits[0:8])
        c2 = self._bits_to_byte(self._challenge_bits[8:16])
        c3 = self._bits_to_byte(self._challenge_bits[16:24])
        b = self._table_b[c2 & MASK_B]
        c = self._table_c[(c1 ^ b) & 0xFF]
        a = self._table_a[c3 & MASK_A]
        reply = bytes((c, b, a))  # R1=C, R2=B, R3=A (§3.2)
        self._response_bits = [(byte >> (7 - i)) & 1 for byte in reply for i in range(8)]
        self._response_index = 0
        self._phase = _PHASE_RESPONSE
        log.debug("ZC90B challenge %02x %02x %02x -> reply %02x %02x %02x", c1, c2, c3, c, b, a)
        # Assert ready immediately; the firmware polls GPIO5 for non-zero (§2).
        self._gpio.set_input(PIN_DATA, 1)

    @staticmethod
    def _bits_to_byte(bits: list[int]) -> int:
        value = 0
        for bit in bits:  # MSB-first
            value = (value << 1) | (bit & 1)
        return value
