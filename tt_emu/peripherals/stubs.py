"""Constant-returning stubs for the boot-constants checklist (``index.md``).

These are the *minimal* models needed for the unmodified firmware to clear its
early self-tests, per the index's "Boot-critical constants" table. Each is a
clean seam: the real peripheral (audio DMA, USB) is a later task. The former
NAND/NFC, ECC, and L2-DMA stubs have been replaced by the real storage models
in :mod:`tt_emu.peripherals.nand`.

Every value here is a **hardware-model answer**, never a firmware patch:

* audio-clock block ``0x04036000``: ``+0x04`` bit19 reads 1 (else
  ``hal_audio_clk_enable`` spins — ``audio-dac-dma.md`` §5,
  ``system-control-and-clock.md`` §10);
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


class OrBitsRegisterStub(WordRegisterPeripheral):
    """RAM-like registers with a fixed set of *sticky-high* read bits.

    ``or_reads`` maps a word-aligned offset to a bitmask the model **OR**\\ s into
    every read of that offset (preserving whatever the firmware last wrote there,
    unlike :class:`ConstRegisterStub`, which replaces it). Models a peripheral
    whose command/handshake register reflects the written command bits but always
    reads back its "ready"/"complete" status bits set.
    """

    def __init__(
        self, name: str, base: int, size: int, or_reads: dict[int, int] | None = None
    ) -> None:
        super().__init__()
        self.name = name
        self.base = base
        self._size = size
        self._or_reads = dict(or_reads or {})

    @property
    def regions(self) -> tuple[MmioRegion, ...]:
        return (MmioRegion(self.base, self._size),)

    def read_reg(self, offset: int) -> int:
        return super().read_reg(offset) | self._or_reads.get(offset, 0)


#: ZC3201 audio-codec command register ``0x04036004`` ready/complete bits. The
#: 1st-gen nandboot codec-command HAL (alias ``0x08005bf0``) writes a command
#: (``val & 0x3fe00000 | 0x10000 | 0x10``) to ``+0x04`` and then **spins until
#: bit 27 (command-complete)** reads set — and the audio-clock bring-up (like MT)
#: gates on bit 19. The real codec sets both once the command latches; the model
#: reads them back sticky-high so the unmodified handshake completes.
ZC_CODEC_READY_BITS = (1 << 27) | (1 << 19)


def make_zc3201_audio_codec_stub() -> OrBitsRegisterStub:
    """``0x04036000`` ZC3201 audio codec; ``+0x04`` reads back bits 19+27 set."""
    return OrBitsRegisterStub(
        "audiocodec", 0x0403_6000, 0x1000,
        or_reads={AUDIO_CLK_STATUS: ZC_CODEC_READY_BITS},
    )


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
