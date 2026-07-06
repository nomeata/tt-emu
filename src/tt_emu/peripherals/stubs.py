"""Constant-returning stubs for the boot-constants checklist (``index.md``).

These are the *minimal* models needed for the unmodified firmware to clear its
early self-tests, per the index's "Boot-critical constants" table. Each is a
clean seam: the real peripheral (NAND storage, audio DMA, USB) is a later task.

Every value here is a **hardware-model answer**, never a firmware patch:

* audio-clock block ``0x04036000``: ``+0x04`` bit19 reads 1 (else
  ``hal_audio_clk_enable`` spins — ``audio-dac-dma.md`` §5,
  ``system-control-and-clock.md`` §10);
* L2/DMA ``0x04010000``: ``+0x0c`` bit13 (BUSY) reads 0 when idle, ``+0x1c``
  reads 0 (spurious-IRQ check) — ``audio-dac-dma.md`` §2,
  ``interrupts-and-timers.md`` §2.3;
* NFC ``0x0404A000``: ``+0x158`` reads ``0x80000000`` (ready); READ-ID
  ``+0x150`` = ``0x9551D3EC``, ``+0x154`` = 0; STATUS byte ``0xC0``
  (``nand-and-nfc-controller.md`` §5/§8) — a *reactive* stub sufficient for the
  probe/self-tests; the full page-serving model is the next task;
* ECC ``0x0405B000``: ``+0x00`` reads ``0x7000040`` (complete + both dirs +
  pass) — ``nand-and-nfc-controller.md`` §6;
* USB ``0x04070000``: all reads 0, never ``0xFFFFFFFF`` (``usb-musb-device.md``
  §1); DAC ``0x04080000`` and dormant bank ``0x040A0000``: RAM-like scratch.
"""

from __future__ import annotations

import logging

from ..peripheral import MmioRegion, WordRegisterPeripheral

log = logging.getLogger(__name__)


class ConstRegisterStub(WordRegisterPeripheral):
    """RAM-like registers with a fixed set of constant-read overrides.

    ``const_reads`` maps a word-aligned offset to the value reads must return
    regardless of writes; all other offsets are plain read-back storage.
    """

    def __init__(
        self,
        name: str,
        base: int,
        size: int,
        const_reads: dict[int, int] | None = None,
    ) -> None:
        super().__init__()
        self.name = name
        self.base = base
        self._size = size
        self._const_reads = dict(const_reads or {})

    @property
    def regions(self) -> tuple[MmioRegion, ...]:
        return (MmioRegion(self.base, self._size),)

    def read_reg(self, offset: int) -> int:
        if offset in self._const_reads:
            return self._const_reads[offset]
        return super().read_reg(offset)


# --- Audio clock block (0x04036000) ------------------------------------------------

AUDIO_CLK_STATUS = 0x04  # bit19 must read 1 (audio bring-up gate)


def make_audio_clock_stub() -> ConstRegisterStub:
    """``0x04036000`` UART / audio-clock block; ``+0x04`` bit19 reads 1."""
    return ConstRegisterStub(
        "audioclk", 0x0403_6000, 0x1000, const_reads={AUDIO_CLK_STATUS: 1 << 19}
    )


# --- L2 buffer / DMA controller (0x04010000) ---------------------------------------

DMA_CTRL = 0x00
DMA_WORDCOUNT = 0x0C  # bit13 = START/BUSY; reads 0 (idle)
DMA_SPURIOUS = 0x1C  # spurious-IRQ check; reads 0


class DmaStub(ConstRegisterStub):
    """Audio/NAND L2-DMA engine stub.

    ``+0x0c`` bit13 (BUSY) always reads clear so the pre-submit poll never hangs,
    and ``+0x1c`` reads 0 so the audio ISR never treats an IRQ as spurious
    (``audio-dac-dma.md`` §2, ``interrupts-and-timers.md`` §2.3). Full DMA
    completion modelling is a later task.
    """

    def __init__(self) -> None:
        super().__init__("dma", 0x0401_0000, 0x1000)

    def read_reg(self, offset: int) -> int:
        if offset == DMA_WORDCOUNT:
            return super().read_reg(offset) & ~(1 << 13)
        if offset == DMA_SPURIOUS:
            return 0
        return super().read_reg(offset)


# --- NAND flash controller (0x0404A000) --------------------------------------------

NFC_DATA_RD0 = 0x150
NFC_DATA_RD1 = 0x154
NFC_CTRL_STATUS = 0x158

#: READ-ID word the update image's probe expects (Samsung K9GAG08U0M, §8.5).
NAND_READ_ID = 0x9551D3EC
NFC_STATUS_READY = 0x80000000  # +0x158 bit31 = sequencer ready (§5)
NAND_STATUS_BYTE = 0xC0  # STATUS: ready + not write-protected (§8.4)

_CMD_READID = 0x90
_CMD_STATUS = 0x70


class NfcStub(WordRegisterPeripheral):
    """Reactive NFC stub: answers the probe (RESET/READ-ID/STATUS) and reads ready.

    Enough for the boot probe and early self-tests (``nand-and-nfc-controller.md``
    §8.5): the command-list GO latches which command was staged and points
    ``DATA_RD0/1`` at the right constant. Page-data serving (the storage mount)
    is the next task — until then the mount stalls after the probe, which is the
    documented checkpoint.
    """

    name = "nfc"
    base = 0x0404_A000

    def __init__(self) -> None:
        super().__init__()
        self._datard = [0, 0]

    @property
    def regions(self) -> tuple[MmioRegion, ...]:
        return (MmioRegion(self.base, 0x200),)

    def read_reg(self, offset: int) -> int:
        if offset == NFC_CTRL_STATUS:
            return NFC_STATUS_READY  # bit31 ready/done (§5)
        if offset == NFC_DATA_RD0:
            return self._datard[0]
        if offset == NFC_DATA_RD1:
            return self._datard[1]
        return super().read_reg(offset)

    def write_reg(self, offset: int, value: int) -> None:
        if 0x100 <= offset < 0x150:
            # Command-list micro-op: decode the command byte (bits[18:11]).
            self._decode_microop(value)
        super().write_reg(offset, value)

    def _decode_microop(self, word: int) -> None:
        if (word & 0x7F) != 0x64:  # not a command cycle (0x64 = CLE)
            return
        cmd = (word >> 11) & 0xFF
        if cmd == _CMD_READID:
            self._datard = [NAND_READ_ID, 0]  # ID bytes 1-4 / 5-8 (§8.5)
        elif cmd == _CMD_STATUS:
            self._datard = [NAND_STATUS_BYTE, 0]  # STATUS byte 0xC0 (§8.4)


# --- ECC engine (0x0405B000) -------------------------------------------------------

ECC_STATUS = 0x00
#: Always answer complete + both directions done + pass (§6): the firmware then
#: never touches the correction FIFO or retry paths.
ECC_STATUS_PASS = 0x40 | 0x1000000 | 0x2000000 | 0x4000000  # = 0x7000040


def make_ecc_stub() -> ConstRegisterStub:
    """``0x0405B000`` ECC engine; ``+0x00`` reads ``0x7000040`` (§6)."""
    return ConstRegisterStub("ecc", 0x0405_B000, 0x1000, const_reads={ECC_STATUS: ECC_STATUS_PASS})


# --- USB / DAC / dormant bank ------------------------------------------------------


class UsbStub(WordRegisterPeripheral):
    """MUSB dead-bus defaults: reads return 0, never 0xFFFFFFFF (§1)."""

    name = "usb"
    base = 0x0407_0000

    @property
    def regions(self) -> tuple[MmioRegion, ...]:
        return (MmioRegion(self.base, 0x1000),)


def make_dac_stub() -> ConstRegisterStub:
    """``0x04080000`` internal DAC; RAM-like scratch is sufficient (§7)."""
    return ConstRegisterStub("dac", 0x0408_0000, 0x1000)


def make_dormant_bank_stub() -> ConstRegisterStub:
    """``0x040A0000`` dormant peripheral bank; any benign constant works (§10)."""
    return ConstRegisterStub("dormant", 0x040A_0000, 0x1000)
