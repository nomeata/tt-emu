"""Audio DAC/DMA engine at ``0x04010000`` (``audio-dac-dma.md``).

Extends the NAND L2-buffer model (:class:`~tt_emu.peripherals.nand.L2NandBuffer`
— the register block is shared between audio DMA and NAND staging, §2 /
``nand-and-nfc-controller.md`` §7) with the audio side of the engine:

* **submit protocol** (§2): the firmware polls ``+0x0c`` bit13 (reads clear when
  idle), writes source/destination (``+0x04``/``+0x08``), writes
  ``(len/4) | 0x2000`` to ``+0x0c`` (START), then kicks ``+0x00 |= 0x10000``;
* **capture** (§7 items 2/3): on a START whose destination is the DAC port
  (``0x08086200``), the chunk's source bytes — final, post-volume S16LE stereo
  (§1/§5) — are read and appended to an
  :class:`~tt_emu.audio_capture.AudioCapture`. The DAC engine is a **physical**
  bus master: its source register holds ``phys & 0x3ffff``, an 18-bit offset
  into the hardware's 256-KiB physical-RAM aperture (base
  :data:`~tt_emu.machine.PHYS_RAM_BASE`). The model reconstructs the physical
  address ``window_base | (src & 0x3ffff)`` and reads it via
  :meth:`~tt_emu.machine.Machine.read_phys`, which translates physical→flat
  through the firmware's active address map (§2). This resolves **every** DAC
  source uniformly — content ring, system-voice buffer, teardown flush — with
  no reference to any firmware ring/struct;
* **teardown flush** (§6): a submit made while the swallow flag ``0x08008c91``
  is set is the silence flush — completion is still delivered (the firmware
  spins on the flag), but the bytes are not captured;
* **paced completion** (§4, ``interrupts-and-timers.md`` §7.4): top-level line 0
  is asserted ``len_bytes / (4 × rate)`` seconds after the submit, converted to
  machine clock via ``instructions_per_tick`` (one tick = 20 ms) under
  ``MachineConfig.dac_pacing='faithful'``; the default ``'fast'`` signals
  completion as soon as the run loop can, so the firmware feeds audio at the
  emulator's own speed — identical PCM, far less lag. The system tick is never
  touched (§4 ★ warning). The firmware's ISR clears the kick bit
  (``+0x00 &= ~0x10000``) — that write is the line-0 ACK (§3);
* **sample rate** (§7 item 5): decoded from the DAC rate-divider field
  ``0x04000008`` bits[20:13] (``system-control-and-clock.md`` §4), scaled from
  the live-Observed data point (divider ``0x28`` → 22050 Hz during GME media
  playback; the idle divider ``0x74`` then decodes to the 8000 Hz bring-up
  default, consistently) and snapped to the standard rate set. DOC GAP: both
  the doc's older 0x46 → 22050 point and its §5 OSR-table formula
  (``rate ≈ master/(div+1)/OSR``, master ≤ 14 MHz) fail to reproduce the live
  programming (the OSR field stays 0), so the emulator scales the live data
  point instead (exact for GME content, which is all 22050 Hz).

Memory-to-memory transfers are not performed (both addresses are 18-bit window
offsets into the physical aperture); they are logged if ever seen. None occur on
the boot/playback paths exercised so far.
"""

from __future__ import annotations

import logging

from ..audio_capture import AudioCapture
from ..machine import PHYS_RAM_BASE
from .gpio import GpioBlock
from .intc import IntcTimer, LINE_AUDIO
from .nand import L2NandBuffer, NfcController
from .syscon import SysCon

log = logging.getLogger(__name__)

__all__ = ["AudioDma", "rate_from_divider", "DAC_PORT_DST"]

# --- Register offsets within 0x04010000 (§2) -----------------------------------------

DMA_CTRL = 0x00       #: control; bit16 = kick/GO, cleared by the ISR = line-0 ACK
DMA_SRC = 0x04        #: source (memory: phys & 0x3ffff; port: code | 0x08080000)
DMA_DST = 0x08        #: destination, same encoding
DMA_WORDCOUNT = 0x0C  #: (len/4) & 0x7ff | bit13 START/BUSY
DMA_PORT_STATUS = 0x10  #: per-port status word (shared with the L2 fill level)

DMA_KICK = 1 << 16
DMA_START = 1 << 13
WORDCOUNT_MASK = 0x7FF

#: Peripheral-port destination for DAC playback: port 1 = 0x6200 | 0x08080000 (§2).
DAC_PORT_DST = 0x0808_6200
PORT_FLAG = 0x0808_0000

# --- DMA source addressing + teardown handshake (§2/§6) -------------------------------

#: 18-bit physical-window mask of the source-address encoding: the DAC engine's
#: source register indexes a 256-KiB physical-RAM aperture based at
#: :data:`~tt_emu.machine.PHYS_RAM_BASE` (§2, proven at runtime).
SRC_WINDOW_MASK = 0x3FFFF

#: Teardown "swallow one done" flag (§6): a submit made while this is set is the
#: silence flush — completion is delivered but the constant bytes are not
#: captured. The one firmware datum the model still consults (a documented
#: handshake, verified separately), independent of source resolution.
SWALLOW_FLAG_ADDR = 0x0800_8C91

# --- Sample-rate decode (§7 item 5 + system-control-and-clock.md §4) -------------------

#: Observed data point (live GME playback under emulation): DAC divider code
#: **0x28 → 22050 Hz** (the media track rate; source OGGs byte-verified
#: 22050). Cross-check: the idle/bring-up divider the same run programs is
#: 0x74 → estimate 7727 → snaps to the §1 8000 Hz bring-up default. DOC GAP:
#: the doc's earlier 0x46 → 22050 point does not fit this scheme (nor its own
#: OSR formula — the OSR field 0x04000064 stays 0 throughout the run); the
#: live pair above supersedes it.
_DIV_REF, _RATE_REF = 0x28, 22050
_RATE_K = _RATE_REF * (_DIV_REF + 1)
STANDARD_RATES = (8000, 11025, 16000, 22050, 32000, 44100, 48000)
REG_CLK_AUDIO = 0x08  #: SysCon register holding bits[20:13] = DAC rate divider

#: Amp/mute pins for the "audible" annotation (§1; gpio-buttons-led.md).
PIN_AMP_ENABLE = 16
PIN_MUTE = 13


def rate_from_divider(divider: int) -> int:
    """Decode the achieved DAC rate from the ``0x04000008`` divider code.

    Scaled from the live-Observed 0x28 → 22050 Hz point (see module docstring
    DOC GAP note) and snapped to the standard rate set when within 8 %.
    """
    if divider <= 0:
        return _RATE_REF  # divider never programmed: assume the GME track rate
    estimate = _RATE_K / (divider + 1)
    best = min(STANDARD_RATES, key=lambda r: abs(r - estimate))
    if abs(best - estimate) / best <= 0.08:
        return best
    return round(estimate)


class AudioDma(L2NandBuffer):
    """The 0x04010000 DMA engine: NAND L2 staging + audio playback capture."""

    name = "audiodma"

    def __init__(
        self,
        nfc: NfcController,
        intc: IntcTimer,
        syscon: SysCon,
        gpio: GpioBlock | None = None,
        capture: AudioCapture | None = None,
    ) -> None:
        super().__init__(nfc)
        self._intc = intc
        self._syscon = syscon
        self._gpio = gpio
        self.capture = capture if capture is not None else AudioCapture()
        self._complete_at: int | None = None
        self._warned_mem2mem = False
        self.dac_submits = 0       #: DAC-port START writes seen
        self.flush_submits = 0     #: teardown silence flushes (§6, not captured)
        self.unresolved_submits = 0  #: DAC submits whose physical source mapped nowhere
        self.completions = 0       #: line-0 completion asserts delivered
        self.last_dac_submit_at = 0  #: machine clock of the last DAC submit

    # --- register behaviour ----------------------------------------------------------------

    def write_reg(self, offset: int, value: int) -> None:
        if offset == DMA_CTRL:
            old = self._regs.get(DMA_CTRL, 0)
            super().write_reg(offset, value)  # parent handles the L2 strobe bits
            if old & DMA_KICK and not value & DMA_KICK:
                # The ISR's kick-clear is the line-0 ACK (§3).
                self._intc.clear_line(LINE_AUDIO)
            return
        if offset == DMA_WORDCOUNT:
            if value & DMA_START and value & WORDCOUNT_MASK:
                self._on_start(value)
            # Bit13 reads back clear: the transfer is "in flight" and the
            # firmware only polls it before the *next* submit (§7 item 3).
            super().write_reg(offset, value & ~DMA_START)
            return
        super().write_reg(offset, value)

    # --- the START write (§7 item 3) -----------------------------------------------------------

    def _on_start(self, value: int) -> None:
        machine = self.machine
        if machine is None:
            return
        length = (value & WORDCOUNT_MASK) * 4
        dst = self._regs.get(DMA_DST, 0)
        src = self._regs.get(DMA_SRC, 0)
        if dst != DAC_PORT_DST:
            if (dst & ~0xFFFF) == PORT_FLAG or (src & ~0xFFFF) == PORT_FLAG:
                log.warning("DMA to/from unmodelled port (src=%#x dst=%#x len=%#x)",
                            src, dst, length)
            elif not self._warned_mem2mem:
                self._warned_mem2mem = True
                log.warning("memory-to-memory DMA seen (src=%#x dst=%#x len=%#x) — "
                            "not performed (18-bit window base unresolved, §2 caveat)",
                            src, dst, length)
            return

        self.dac_submits += 1
        self.last_dac_submit_at = machine.clock
        rate = self.current_rate()
        if machine.read_u8(SWALLOW_FLAG_ADDR):
            self.flush_submits += 1  # §6 silence flush: complete, don't capture
        else:
            data = self._resolve_and_read(src, length)
            if data is None:
                self.unresolved_submits += 1
                log.warning("DAC submit source %#07x (len %#x) maps to no physical "
                            "RAM page — chunk not captured", src, length)
            else:
                self.capture.append(machine.clock, rate, data, audible=self._audible())

        # Signal the completion. Faithful pacing waits len/(4·rate) seconds so
        # the firmware runs on the pen's real audio timeline (§4); fast pacing
        # signals as soon as the tick loop can deliver it, so the firmware
        # feeds audio at the emulator's own speed — same PCM, far less lag.
        if machine.config.dac_pacing == "faithful":
            ticks_x_rate = length * 50  # (len / (4·rate)) / 0.02 s == len·12.5/rate ticks
            delay = max(1, (ticks_x_rate * machine.config.instructions_per_tick
                            + 4 * rate - 1) // (4 * rate))
        else:
            delay = 1
        self._complete_at = machine.clock + delay

    def _resolve_and_read(self, src: int, length: int) -> bytes | None:
        """Read the chunk the DAC engine plays, as a physical bus master (§2/§7).

        The source register holds ``phys & 0x3ffff`` — an 18-bit offset into the
        256-KiB physical aperture based at :data:`~tt_emu.machine.PHYS_RAM_BASE`.
        Reconstruct the physical address and read it through
        :meth:`~tt_emu.machine.Machine.read_phys`, which resolves physical→flat
        via the firmware's active address map. ``None`` when the source maps to
        no physical page (not a live DMA buffer).
        """
        machine = self.machine
        assert machine is not None
        phys = PHYS_RAM_BASE | (src & SRC_WINDOW_MASK)
        return machine.read_phys(phys, length)

    def _audible(self) -> bool:
        """Amp on (GPIO16=1) and mute released (GPIO13=0) — §1 audibility."""
        if self._gpio is None:
            return True
        return self._gpio.out_level(PIN_AMP_ENABLE) == 1 and self._gpio.out_level(PIN_MUTE) == 0

    def current_rate(self) -> int:
        """The DAC rate per the divider field ``0x04000008`` bits[20:13] (§7 item 5)."""
        return rate_from_divider((self._syscon.read_reg(REG_CLK_AUDIO) >> 13) & 0xFF)

    # --- completion delivery (§3/§4) --------------------------------------------------------------

    def tick(self, now: int) -> None:
        if self._complete_at is not None and now >= self._complete_at:
            if self._regs.get(DMA_CTRL, 0) & DMA_KICK:
                self._complete_at = None
                self.completions += 1
                self._intc.assert_line(LINE_AUDIO)
            # else: the kick write hasn't landed yet (submit in progress);
            # keep the completion pending until it does.
