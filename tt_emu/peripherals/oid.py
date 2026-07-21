"""OID sensor: the Sonix decoder ASIC on the two-wire GPIO link (``oid-sensor.md``).

The sensor is a separate decoder ASIC that outputs finished 18-bit OID indices
over a bit-banged serial link — clock on **GPIO2** (host output), data/attention
on **GPIO9** (bidirectional, idle high; §1). The emulator models the sensor as a
small state machine attached to those pins (§6) and lets the firmware's **own**
shift-in and decoders run unmodified:

* a tap is *armed* (:meth:`OidSensor.tap`): GPIO9 reads 0 (attention, §3.1);
  the firmware's 40 ms poll gate opens and its shift-in (§3.2) clocks the frame
  out through the authentic handshake (ready-ACK, host-ACK, bit serve);
* the frame served is the full 32-bit on-wire word
  ``frame32(N) = ((0x400000 | (N & 0x3FFFF)) << 9) | 0x100 | 0xF0`` (§2/§6 —
  never a shortcut, so a wrong code can never validate);
* bits are advanced on the firmware's **falling clock edges** (§3.2: the pin is
  sampled in the clock-low phase, MSB first; §6 blesses either per-read or
  per-edge serving);
* the number of bits served comes from the firmware's own ``bit_count`` byte at
  ``0x08008C09`` (§6 item 4) — 23 on the gameplay path, 32 on the status polls;
  one authentic frame serves both decoders (§4);
* host→sensor command bytes (§3.3) are tolerated: clocking while GPIO9 is a
  firmware *output* (beyond the 2-edge host-ACK pulse) is treated as command
  traffic and a pending frame is re-armed afterwards (§6 "make sure command
  traffic cannot desync a pending frame");
* a trigger pulse (§3.4) that strands an armed frame in the ACK phase recovers
  to attention on the pulse's falling edge, so the following shift-in finds the
  frame again.

After the last bit is read the sensor returns to idle (GPIO9 = 1) — "tap and
immediately lift" (§6 item 5); active game states then read pen-up correctly.
The release is deferred by two machine ticks so the firmware's read of the last
bit (a few instructions after the final falling edge) can never race it.
"""

from __future__ import annotations

import logging
from collections import deque
from enum import Enum, auto

from ..peripheral import Peripheral
from .gpio import GpioBlock

log = logging.getLogger(__name__)

__all__ = ["OidSensor", "frame32", "PIN_CLOCK", "PIN_DATA", "BIT_COUNT_ADDR"]

PIN_CLOCK = 2  #: GPIO2 — host clock output (§1 wiring)
PIN_DATA = 9  #: GPIO9 — bidirectional data / attention (§1 wiring)

#: The firmware's own capture-state ``bit_count`` byte (§4 struct, §6 item 4).
BIT_COUNT_ADDR = 0x0800_8C09
FRAME_BITS = 32

#: Values §6 warns against injecting.
FILLER_INDEX = 0x3FFFC  # silently dropped by the decoder (§2)
SYSTEM_FAMILY_FIRST = 0xFF00  # factory/system codes — special routing (§2)
SYSTEM_FAMILY_LAST = 0xFFFE

#: The on-wire status frame answering the standby sleep handshake (§2/§6:
#: code word 0x60FFF8, type ``11``): the firmware replies with the sleep
#: command sequence and sets its done-latch, which stops the 32-bit polls.
STATUS_FRAME = (0x60FFF8 << 9) | 0x100 | 0xF0

#: Ticks (machine chunk boundaries) to hold the last bit before releasing to
#: idle — must outlast the firmware's final pin read (a few instructions) and
#: stay well inside the 40 ms (= 2-tick) poll cadence.
_RELEASE_DELAY_TICKS = 2

#: Edges allowed on the clock while GPIO9 is a firmware output before the
#: traffic is classified as a command byte instead of the host-ACK pulse
#: (§3.2: the host ACK is exactly one low+high pulse = 2 edges).
_HOST_ACK_MAX_EDGES = 2


def frame32(oid: int) -> int:
    """The authentic 32-bit on-wire frame for OID number ``oid`` (§2/§7).

    Type bits ``10``, 18-bit index, valid bit, check byte ``0xF0`` (nibbles sum
    to 15 — the canonical emulator choice, §2).
    """
    return ((0x400000 | (oid & 0x3FFFF)) << 9) | 0x100 | 0xF0


class _State(Enum):
    """§6 state machine phases (advanced purely by observed firmware GPIO ops)."""

    IDLE = auto()      # no tap pending; GPIO9 reads 1
    PRE = auto()       # tap armed; GPIO9 reads 0 (attention)
    ACK = auto()       # firmware raised the clock; GPIO9 reads 1 (ready-ACK)
    HOST_ACK = auto()  # GPIO9 is a firmware output (host ACK / possible command)
    BITS = auto()      # serving frame bits on falling clock edges
    COMMAND = auto()   # firmware is clocking a command byte out (§3.3)


class OidSensor(Peripheral):
    """The two-wire OID sensor model (no MMIO range — pure GPIO device)."""

    name = "oid"

    def __init__(
        self,
        gpio: GpioBlock,
        *,
        answer_status_polls: bool = False,
        pin_clock: int = PIN_CLOCK,
        pin_data: int = PIN_DATA,
        bit_count_addr: int = BIT_COUNT_ADDR,
    ) -> None:
        """``answer_status_polls`` (default off): when set, the sensor answers a
        standby 32-bit status poll with the §2 status frame, completing the
        firmware's sensor **sleep handshake** (§4.2). This is the authentic idle
        behaviour but tells the pen to sleep, so it is off for tap sessions —
        held taps survive the unanswered polls via the §6 re-serve. Off is also
        what §6 recommends ("status frames ... never needed for tap injection").

        ``pin_clock`` / ``pin_data`` / ``bit_count_addr`` are the wiring, which
        differs by pen generation (the two-wire link + capture-state address are
        the *same protocol* on both, just on different GPIO pins / RAM bytes):
        MT clocks on GPIO2 with data on GPIO9 and reads ``bit_count`` at
        0x08008C09; the 1st-gen ZC3201 clocks on GPIO7 with data on GPIO16 and
        reads ``bit_count`` at 0x08007BF9 (its nandboot OID HAL bit-bang
        ``hal_oid_shift_in`` 0x08005D80 / poll callback 0x08005F48 — see
        ``docs/zc3201-boot-feasibility.md`` "Leg 20"). The defaults are MT's."""
        super().__init__()
        self._gpio = gpio
        self._answer_status = answer_status_polls
        self._pin_clock = pin_clock
        self._pin_data = pin_data
        self._bit_count_addr = bit_count_addr
        self._queue: deque[int] = deque()
        self._hold_oid: int | None = None
        self._state = _State.IDLE
        self._frame = 0
        self._oid = 0
        self._is_status = False
        self._status_pending = False
        self._served = 0
        self._bit_count = 23
        self._ack_edges = 0
        self._release_countdown: int | None = None
        #: Completed tap-frame captures (both 23- and 32-bit reads count;
        #: status frames are not counted).
        self.taps_served = 0
        #: Tap frames consumed by the 23-bit **gameplay** capture (§4.1) — the
        #: only capture that posts event 0x1060. One increment = the firmware
        #: latched the tap; a physical tap-and-lift ends here.
        self.gameplay_frames_served = 0
        #: Status frames consumed by a 32-bit poll (sleep handshakes answered).
        self.status_frames_served = 0
        gpio.watch_output(pin_clock, self._on_clock)
        gpio.watch_direction(pin_data, self._on_data_dir)
        self._gpio.set_input(self._pin_data, 1)  # bus idle: data latched high (§3.1)

    # --- headless API ------------------------------------------------------------------

    def tap(self, oid: int) -> None:
        """Arm a single tap: present ``oid`` on the next capture the firmware runs.

        The frame stays armed (attention low) until the firmware's own poll
        clocks it out — no timing coordination is needed (§6 "Timing"). Taps
        queue; each is a single tap-and-lift.

        Note that a lone frame can be consumed by a **32-bit status poll**,
        which never posts events (§4.2) — at standby, where those polls run,
        use :meth:`hold` / :meth:`lift` (the physical press-and-hold) instead.
        """
        self._check_oid(oid)
        self._queue.append(oid)
        if self._state is _State.IDLE or self._armed_status_only():
            self._arm_next()

    def hold(self, oid: int) -> None:
        """Press-and-hold ``oid``: re-serve the frame after every capture (§6
        "Repeat / anti-repeat": the real sensor re-reports ~every 40 ms while
        the pen is held on a code) until the tap latches or :meth:`lift` is
        called.

        This is the reliable way to tap at standby: frames eaten by the 32-bit
        status polls (§4.2, no event posted) are simply re-served until the
        40 ms gameplay poll (§4.1) gets one and posts event ``0x1060`` — at
        which point the hold ends by itself (tap-and-lift; see
        :meth:`_finalize_serve`). :meth:`lift` remains for withdrawing a tap
        that never latched.
        """
        self._check_oid(oid)
        self._hold_oid = oid
        if self._state is _State.IDLE or self._armed_status_only():
            self._arm(oid)

    def lift(self) -> None:
        """Lift the pen: stop the :meth:`hold` re-serve; an armed-but-untouched
        frame is withdrawn (a frame mid-transfer still completes)."""
        self._hold_oid = None
        if self._state is _State.PRE and not self._is_status and not self._queue:
            self._state = _State.IDLE
            self._gpio.set_input(self._pin_data, 1)
            if self._status_pending:
                self._arm_status()

    @property
    def pending(self) -> bool:
        """True while a *tap* is armed, queued, or held (status frames don't count)."""
        return (
            (self._state is not _State.IDLE and not self._is_status)
            or bool(self._queue)
            or self._hold_oid is not None
        )

    def reset(self) -> None:
        self._queue.clear()
        self._hold_oid = None
        self._state = _State.IDLE
        self._is_status = False
        self._status_pending = False
        self._release_countdown = None
        self._served = 0
        self._gpio.set_input(self._pin_data, 1)

    # --- state machine helpers -----------------------------------------------------------

    def _armed_status_only(self) -> bool:
        """An armed-but-untouched status frame — a tap may replace it."""
        return self._state is _State.PRE and self._is_status

    @staticmethod
    def _check_oid(oid: int) -> None:
        if not 0 <= oid <= 0x3FFFF:
            raise ValueError(f"OID {oid} out of the 18-bit index range (§2)")
        if oid == FILLER_INDEX:
            log.warning("tap(%#x): the filler index is silently dropped (§2)", oid)
        if SYSTEM_FAMILY_FIRST <= oid <= SYSTEM_FAMILY_LAST:
            log.warning("tap(%#x): system-code family — special routing (§2)", oid)

    def _arm(self, oid: int) -> None:
        self._oid = oid
        self._frame = frame32(oid)
        self._is_status = False
        self._served = 0
        self._ack_edges = 0
        self._state = _State.PRE
        self._gpio.set_input(self._pin_data, 0)  # assert attention (§3.1)
        log.debug("OID tap armed: %d (frame %#010x)", oid, self._frame)

    def _arm_status(self) -> None:
        """Arm the §2 status frame — the answer to a standby trigger pulse.

        DOC GAP (found empirically; ``oid-sensor.md`` §6 calls status frames
        "never needed for tap injection"): without this answer the firmware's
        standby entry never completes the sensor sleep handshake — its ~100 ms
        trigger-pulse busy-delay plus poll retries occupy the **main loop**
        indefinitely, the event pump starves (posted ``0x1060`` taps are never
        dispatched) and the pen idles to auto-off. Answering one status frame
        sets the firmware's done-latch (§4.2 step 4) and standby goes quiet.
        """
        self._frame = STATUS_FRAME
        self._is_status = True
        self._served = 0
        self._ack_edges = 0
        self._state = _State.PRE
        self._gpio.set_input(self._pin_data, 0)
        log.debug("OID status frame armed (sleep handshake)")

    def _arm_next(self) -> None:
        self._arm(self._queue.popleft())

    def _rearm_or_idle(self) -> None:
        """Return to PRE if a frame is still owed or held, else to bus idle.

        Taps take priority; a pending (armed but not 32-bit-consumed) status
        frame is re-armed last — e.g. when a 23-bit gameplay poll clocked it
        out mid-pulse (its type bits ``11`` fail the §4.1 check, so it is
        dropped without an event and must be offered again).
        """
        if self._queue:
            self._arm_next()
        elif self._hold_oid is not None:
            self._arm(self._hold_oid)  # press-and-hold: re-serve (§6)
        elif self._status_pending:
            self._arm_status()
        else:
            self._state = _State.IDLE
            self._gpio.set_input(self._pin_data, 1)

    # --- GPIO observers (the §6 phase advance) ---------------------------------------------

    def _on_clock(self, _pin: int, old: int, new: int) -> None:
        state = self._state
        if state is _State.IDLE:
            if old == 0 and new == 1 and self._answer_status:
                # A trigger pulse (§3.4) with nothing else to report: offer the
                # status frame so the standby sleep handshake completes (§4.2).
                # Opt-in only — it puts the pen to sleep (see the ctor).
                self._status_pending = True
                self._arm_status()
            return
        if state is _State.PRE:
            if old == 0 and new == 1:
                # Shift-in step "clk = 1; wait for GPIO9 == 1": the ready-ACK (§3.2).
                self._state = _State.ACK
                self._gpio.set_input(self._pin_data, 1)
        elif state is _State.ACK:
            if old == 1 and new == 0:
                # A trigger pulse (§3.4) ended without the host ACK — the frame
                # would strand with attention high (§6 "Timing"); re-assert it.
                self._state = _State.PRE
                self._gpio.set_input(self._pin_data, 0)
        elif state is _State.HOST_ACK:
            self._ack_edges += 1
            if self._ack_edges > _HOST_ACK_MAX_EDGES:
                # More clocking than the 2-edge host-ACK pulse: this is a
                # command byte (§3.3); don't let it desync the frame (§6).
                self._state = _State.COMMAND
        elif state is _State.BITS:
            if old == 1 and new == 0:
                self._serve_bit()

    def _on_data_dir(self, _pin: int, _old: int, new: int) -> None:
        if new == 0:  # GPIO9 became a firmware output
            if self._state is _State.ACK:
                self._state = _State.HOST_ACK  # host ACK: drive low + clock pulse (§3.2)
                self._ack_edges = 0
            elif self._state in (_State.PRE, _State.IDLE, _State.BITS):
                # Command traffic (wake 0x56 / sleep / per-tap 0xA6, §3.3).
                if self._state is _State.BITS:
                    if self._release_countdown is not None:
                        # Frame fully served, release still pending: finalize now.
                        self._release_countdown = None
                        self._finalize_serve()
                    elif not self._is_status:
                        log.debug("OID: command interrupted a frame mid-serve; re-arming")
                        self._queue.appendleft(self._oid)  # frame not consumed
                self._state = _State.COMMAND
        else:  # GPIO9 released back to input
            if self._state is _State.HOST_ACK:
                if self._ack_edges <= _HOST_ACK_MAX_EDGES:
                    self._begin_bits()
                else:  # was a command after all — keep the frame armed
                    self._state = _State.PRE
                    self._gpio.set_input(self._pin_data, 0)
            elif self._state is _State.COMMAND:
                self._rearm_or_idle()

    def _begin_bits(self) -> None:
        """Enter BITS: the firmware released the line after its host ACK (§6 item 4)."""
        self._state = _State.BITS
        self._served = 0
        self._bit_count = self._read_bit_count()

    def _read_bit_count(self) -> int:
        """The firmware's own ``bit_count`` at ``0x08008C09`` — 23 or 32 (§4/§6)."""
        if self.machine is not None:
            value = self.machine.read_u8(self._bit_count_addr)
            if value in (23, 32):
                return value
            log.warning("OID: unexpected bit_count %#x at %#x; assuming 23",
                        value, self._bit_count_addr)
        return 23

    def _serve_bit(self) -> None:
        """Present the next frame bit for the firmware's clock-low sample (§3.2)."""
        if self._served < self._bit_count:
            bit = (self._frame >> (FRAME_BITS - 1 - self._served)) & 1
            self._gpio.set_input(self._pin_data, bit)
            self._served += 1
            if self._served == self._bit_count:
                # Last bit is on the line; the firmware reads it a few
                # instructions from now. Release to idle after a safe delay.
                self._release_countdown = _RELEASE_DELAY_TICKS
        else:
            # Clocked past the frame — should not happen; serve idle-high.
            log.warning("OID: clocked past %d frame bits", self._bit_count)
            self._gpio.set_input(self._pin_data, 1)

    def _finalize_serve(self) -> None:
        """Account a completed frame capture (tap or status)."""
        if self._is_status:
            if self._bit_count == FRAME_BITS:
                # A 32-bit poll validated it: the firmware runs the sleep
                # command sequence and sets its done-latch (§4.2 step 4).
                self._status_pending = False
                self.status_frames_served += 1
                log.debug("OID status frame served (32-bit poll)")
            # A 23-bit capture of the status frame fails the §4.1 type check
            # and is dropped; keep it pending so the next poll gets it.
        else:
            self.taps_served += 1
            if self._bit_count != FRAME_BITS:
                self.gameplay_frames_served += 1
                # A 23-bit gameplay capture posts event 0x1060 — the tap is
                # latched, so the press-and-hold ends *here* (tap-and-lift).
                # Drivers also lift when they observe the latch, but only at
                # chunk granularity: when the emulation runs many capture
                # cycles per chunk (realtime pacing, oversped clocks), a
                # still-held frame re-serves before that lift lands and the
                # firmware dispatches the tap twice (§6 repeat-on-hold — real,
                # but never what the tap APIs mean). Status-poll-consumed
                # frames (32-bit, no event) keep the hold: re-serving through
                # them is the point of holding (§4.2).
                self._hold_oid = None
            log.debug("OID tap served: %d (%d bits)", self._oid, self._bit_count)

    # --- deferred release (tap-and-lift, §6 item 5) -------------------------------------------

    def tick(self, now: int) -> None:
        if self._release_countdown is None:
            return
        self._release_countdown -= 1
        if self._release_countdown <= 0:
            self._release_countdown = None
            self._finalize_serve()
            self._rearm_or_idle()
