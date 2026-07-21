"""NAND flash controller (NFC) + ECC engine + L2 buffer — register-level model.

Implements the reactive model of ``nand-and-nfc-controller.md`` §10 (option A,
hook-free): the three MMIO blocks

* NFC ``0x0404A000`` — command-list sequencer (§5): micro-ops staged at
  ``+0x100..``, GO/status at ``+0x158``, DATA_RD at ``+0x150/+0x154``;
* ECC engine ``0x0405B000`` — config-word latch + always-pass status (§6);
* L2 buffer controller ``0x04010000`` — BUF_CTRL strobes and the buffer-4
  fill level (§7); also carries the audio-DMA idle defaults the former DmaStub
  served (``audio-dac-dma.md`` §2, ``interrupts-and-timers.md`` §2.3);

plus the **(row, col) → flat byte offset** AU decode of §4.2, serving reads,
programs, block erases, copy-back, READ-ID and STATUS from a
:class:`~tt_emu.nand_image.NandImage` backing store. Data crosses the 512-byte
circular SRAM window at ``0x08006800`` (plain RAM; §7) exactly as on hardware —
the model deposits/captures bytes there, the firmware memcpys them itself.

Everything is purely reactive (every poll happens after the GO/strobe that
triggers the work, §1); no timers, no ECC parity, no randomizer, no read-retry
(§8.7/§10 "safe simplifications").
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..nand_image import AU_SIZE, NandImage, SECTOR_SIZE
from ..peripheral import MmioRegion, WordRegisterPeripheral

if TYPE_CHECKING:  # pragma: no cover - import cycle guard
    from ..machine import Machine

log = logging.getLogger(__name__)

__all__ = ["NfcController", "EccEngine", "L2NandBuffer", "decode_byte_offset", "tag_key"]

# --- Address decode (nand-and-nfc-controller.md §4.2) --------------------------------

#: Sectors per allocation unit under the authentic geometry
#: (dev[0x18]*dev[0x1c] >> 9 = 8; the decode is identity when this is 1, §4.4).
AU_SECS = AU_SIZE // SECTOR_SIZE
#: Row stride per block (dev[0x14] = 256).
ROW_STRIDE = 256


def decode_byte_offset(row: int, col: int, eccsize: int) -> int:
    """Map an NFC ``(row, col)`` request to a flat data-image byte offset (§4.2).

    ``block = row >> 8``; ``au = row & 0xFF``; ``sub = col // eccsize`` is the
    512-B sub-sector inside the AU (clamped to AU_SECS-1 — a tag-only read's
    column points *past* all data records, §4.1). ``eccsize`` is the on-bus
    record size (512 + parity) — the model derives it from the data micro-op's
    byte count, which is exactly ``payload + parity`` (§5.2/§5.3).
    """
    block = row >> 8
    au = row & 0xFF
    sub = min(col // eccsize, AU_SECS - 1) if eccsize > 0 and col > 0 else 0
    return (ROW_STRIDE * block + AU_SECS * au + sub) * SECTOR_SIZE


def tag_key(row: int) -> int:
    """Tag-store key for a row: the **raw row itself** (tags are keyed by the
    row base, never by sub-sector — §4.2; and never flattened to sector
    indices — a row with unit ≥ 32 aliases the next blocks' *data* linearly
    but owns a spare tag distinct from their page-0 tags)."""
    return row


def _sector_eccsize(nbytes: int, payload: int) -> int:
    """Per-sector on-bus record size (512 + parity — the §4.1 ``eccsize``).

    A data record moves ``payload/512`` consecutive sectors; ``nbytes`` (the
    data micro-op's byte count) is payload + parity for **all** of them, so
    one sector's eccsize is ``nbytes / nsec`` (identity for 512-B records).
    """
    nsec = max(payload // SECTOR_SIZE, 1)
    return nbytes // nsec


# --- ECC engine 0x0405B000 (§6) -------------------------------------------------------

ECC_STATUS_PASS = 0x40 | 0x0100_0000 | 0x0200_0000 | 0x0400_0000  # = 0x7000040


class EccEngine(WordRegisterPeripheral):
    """ECC engine: latch the config word, always answer complete+pass (§6).

    The latched config word carries the data-phase **payload length** in
    bits[18:7] (512 for a data sector, 8 for the tag record) — the NFC model
    reads it to classify each record. Reads of ``+0x00`` return ``0x7000040``
    (complete + both directions done + decode pass) so the firmware never
    touches the correction FIFO (``+0x04..`` reads 0) or the retry paths.
    """

    name = "ecc"
    base = 0x0405_B000

    def __init__(self) -> None:
        super().__init__()
        self.config = 0

    @property
    def regions(self) -> tuple[MmioRegion, ...]:
        return (MmioRegion(self.base, 0x1000),)

    @property
    def payload_len(self) -> int:
        """Payload length in bytes of the currently configured record (bits[18:7])."""
        return (self.config >> 7) & 0xFFF

    def read_reg(self, offset: int) -> int:
        if offset == 0x00:
            return ECC_STATUS_PASS
        return 0  # correction FIFO +0x04.. reads 0 (§6)

    def write_reg(self, offset: int, value: int) -> None:
        if offset == 0x00:
            self.config = value  # the |8 start-bit rewrite latches the same fields
        super().write_reg(offset, value)


# --- NFC 0x0404A000 (§5, §8, §10) -----------------------------------------------------

NFC_CMD_LIST = 0x100      # ..+0x14F: micro-op FIFO
NFC_CMD_LIST_END = 0x150
NFC_DATA_RD0 = 0x150
NFC_DATA_RD1 = 0x154
NFC_CTRL_STATUS = 0x158
NFC_STATUS_READY = 0x8000_0000     # bit31 = sequencer ready/done (§5.1)
NFC_GO = 1 << 30                   # GO word bit30

#: READ-ID answer the update image's boot probe expects (Samsung K9GAG08U0M, §8.5).
NAND_READ_ID = 0x9551_D3EC
#: STATUS byte: bit7 not-write-protected + bit6 ready (§8.4).
NAND_STATUS_BYTE = 0xC0

#: The 512-byte circular L2 SRAM window, buffer 4 (§7) — plain RAM. This is the
#: default (2N-MT) address; the L2 buffer block sits at a *fixed hardware* SRAM
#: location that differs per pen generation, so :class:`NfcController` takes it as
#: a per-instance parameter. ZC3201 stages into ``0x08005800`` (buffer-0 base
#: ``0x08005000`` + 4·``0x200``), recovered from its nandboot NAND data-transfer
#: leaf which memcpys 512 bytes from that address paired with the ECC engine.
SRAM_WINDOW = 0x0800_6800
SRAM_WINDOW_SIZE = 512

# NAND command bytes (§8)
_CMD_READ_SETUP = 0x00
_CMD_READ_CONFIRM = 0x30
_CMD_CACHE_READ = 0x35      # copy-back source confirm
_CMD_PROGRAM_SETUP = 0x80
_CMD_COPYBACK_DST = 0x85
_CMD_PROGRAM_CONFIRM = 0x10
_CMD_ERASE_SETUP = 0x60
_CMD_ERASE_CONFIRM = 0xD0
_CMD_STATUS = 0x70
_CMD_READ_ID = 0x90
_CMD_RESET = 0xFF
_NOOP_CMDS = frozenset({_CMD_RESET, 0x36, 0x37, 0x16})  # reset / read-retry (§8.7)


class NfcController(WordRegisterPeripheral):
    """The NFC command-list sequencer, serving a :class:`NandImage` (§10 model A).

    State per §10: the staged ``cmdlist``, latched ``row``/``col``, the pending
    address-consuming command, the transaction's served-bytes cursor, a
    pending-program byte count, the copy-back source, DATA_RD words, and the
    L2 buffer-4 fill level (exposed to :class:`L2NandBuffer`).
    """

    name = "nfc"
    base = 0x0404_A000

    def __init__(
        self, flash: NandImage, ecc: EccEngine, sram_window: int = SRAM_WINDOW
    ) -> None:
        super().__init__()
        self.flash = flash
        self.ecc = ecc
        #: L2 buffer-4 SRAM staging address (per-generation; see SRAM_WINDOW).
        self.sram_window = sram_window
        self._datard = [0, 0]
        self._addr: list[int] = []
        self._pending_cmd: int | None = None
        self._row = 0
        self._col = 0
        self._bytes_done = 0  # data bytes already served/captured this transaction
        self._pend_prog = 0          # payload bytes awaiting the L2 flush strobe
        self._pend_prog_eccsize = SECTOR_SIZE
        #: Program-record bytes already drained from the window (a >512-B
        #: record is staged through the circular window in 512-B slabs, one
        #: drain poll each — see :meth:`on_level_poll`).
        self._prog_staged = bytearray()
        self._cb_src: tuple[int, int] | None = None
        #: Outstanding >512-B read record streaming through the circular
        #: window, with the fill-poll count since its data op (see
        #: :meth:`on_level_poll`).
        self._stream: bytes | None = None
        self._stream_polls = 0
        #: Buffer-4 fill level in 64-B chunks: 8 while a read deposit is
        #: outstanding, else 0 (satisfies the !=0/==8 read waits and the ==0
        #: program-drain wait simultaneously, §10).
        self.l2_level = 0

    @property
    def regions(self) -> tuple[MmioRegion, ...]:
        return (MmioRegion(self.base, 0x200),)

    def attach(self, machine: Machine) -> None:
        super().attach(machine)
        # The ready-status poll (a constant in this reactive model) runs
        # between every micro-op of a NAND stream: the realtime pace-serve
        # point that keeps chunk ends flowing through I/O phases no other
        # stop-safe callback covers. Reading it is pure, so the rolled-back
        # load's replay is harmless (no-op in deterministic mode).
        machine.add_pace_serve_mmio(self.base + NFC_CTRL_STATUS)

    # --- registers (§5.1) --------------------------------------------------------------

    def read_reg(self, offset: int) -> int:
        if offset == NFC_CTRL_STATUS:
            return NFC_STATUS_READY  # bit31: always ready (reactive model, §10)
        if offset == NFC_DATA_RD0:
            return self._datard[0]
        if offset == NFC_DATA_RD1:
            return self._datard[1]
        return super().read_reg(offset)

    def write_reg(self, offset: int, value: int) -> None:
        super().write_reg(offset, value)
        if offset == NFC_CTRL_STATUS and value & NFC_GO:
            self._execute()
        # +0x15c/+0x160 timing words and the bit14 RMW clears need no behaviour.

    # --- command-list execution (§5.2, §10) ---------------------------------------------

    def _execute(self) -> None:
        """Run the staged micro-op list: GO → walk until the first LAST word."""
        self.l2_level = 0  # cleared by the next GO (§10)
        self._stream = None
        offset = NFC_CMD_LIST
        while offset < NFC_CMD_LIST_END:
            word = self._regs.get(offset, 0)
            self._step(word)
            if word & 1:  # LAST
                break
            offset += 4
        # A program setup list (cmd 0x80 + address cycles) has no trailing
        # confirm command — latch its address at end-of-list (§8.2 step 2).
        if self._pending_cmd == _CMD_PROGRAM_SETUP and self._addr:
            self._latch_addr(has_col=True)
            self._bytes_done = 0

    def _step(self, word: int) -> None:
        op = word & 0x7FF & ~0x400  # low bits select the op; 0x400 = wait-R/B flag
        if op in (0x64, 0x65):        # command cycle (CLE)
            self._on_command((word >> 11) & 0xFF)
        elif op in (0x62, 0x63):      # address cycle (ALE; 0x63 = final program cycle)
            self._addr.append((word >> 11) & 0xFF)
        elif op in (0x118, 0x119):    # data NAND -> ECC -> L2
            self._data_read(nbytes=(word >> 11) + 1)
        elif op in (0x128, 0x129):    # data L2 -> ECC -> NAND
            self._data_program(nbytes=(word >> 11) + 1)
        elif op in (0x58, 0x59):      # data read NAND -> DATA_RD regs
            pass                      # DATA_RD latched at command time (0x90/0x70)
        elif op in (0x200, 0x201, 0x000, 0x001):  # wait R/B / bare delay
            pass
        else:
            log.warning("nfc: unrecognized micro-op %#x", word)

    def _on_command(self, cmd: int) -> None:
        if cmd in (_CMD_READ_SETUP, _CMD_PROGRAM_SETUP, _CMD_ERASE_SETUP, _CMD_COPYBACK_DST):
            self._pending_cmd = cmd
            self._addr = []
        elif cmd == _CMD_READ_ID:
            self._pending_cmd = cmd
            self._addr = []
            self._datard = [NAND_READ_ID, 0]  # §8.5: +0x154 = 0
        elif cmd == _CMD_STATUS:
            self._datard = [NAND_STATUS_BYTE, 0]  # §8.4
        elif cmd == _CMD_READ_CONFIRM:  # 0x30: read page/AU (§8.1)
            self._latch_addr(has_col=True)
            self._bytes_done = 0
            self._pending_cmd = None
        elif cmd == _CMD_CACHE_READ:    # 0x35: copy-back source latched (§8.6)
            self._latch_addr(has_col=True)
            self._cb_src = (self._row, self._col)
            self._pending_cmd = None
        elif cmd == _CMD_PROGRAM_CONFIRM:  # 0x10: program commit or copy-back dst
            if self._pending_cmd == _CMD_COPYBACK_DST and self._addr:
                self._latch_addr(has_col=True)
                self._copy_back()
            self._pending_cmd = None   # plain program commit: records already captured
        elif cmd == _CMD_ERASE_CONFIRM:  # 0xD0: erase (§8.3)
            if self._pending_cmd == _CMD_ERASE_SETUP:
                self._latch_addr(has_col=False)  # erase emits row cycles only (§5.2)
                self.flash.erase_block(self._row >> 8)
            self._pending_cmd = None
        elif cmd in _NOOP_CMDS:
            pass
        else:
            log.warning("nfc: unrecognized command byte %#04x", cmd)

    def _latch_addr(self, *, has_col: bool) -> None:
        """Consume collected address cycles: 2 column (LE) + row cycles LSB-first (§5.2)."""
        cycles = self._addr
        self._addr = []
        if has_col and len(cycles) >= 2:
            self._col = cycles[0] | (cycles[1] << 8)
            rows = cycles[2:]
        else:
            self._col = 0
            rows = cycles
        self._row = sum(b << (8 * i) for i, b in enumerate(rows))

    # --- data phases (§8.1/§8.2, §10) ----------------------------------------------------

    def _data_read(self, nbytes: int) -> None:
        """0x119: deposit one record into the SRAM window (§8.1 / §10).

        ``payload`` (the bytes that cross the window) comes from the ECC config
        word; ``nbytes`` is payload+parity, from which the per-sector record
        size (``eccsize``, §4.1) that decodes the column into a sub-sector is
        derived. payload >= 512 → the next data record (records advance by
        their own payload size — the boot loader and the runtime FAT driver
        use 1024-B records, single-sector paths 512-B ones); < 512 → the
        row's tag, truncated to the descriptor's tag length (the boot-stage
        metadata reads use 4-byte tags = the §2.1 spare magics; the NFTL
        uses 8).
        """
        payload = self.ecc.payload_len or min(nbytes, SECTOR_SIZE)
        if payload >= SECTOR_SIZE:
            offset = decode_byte_offset(self._row, self._col, _sector_eccsize(nbytes, payload))
            data = self.flash.read(offset + self._bytes_done, payload)
            self._bytes_done += payload
        else:
            data = self.flash.get_tag(tag_key(self._row))[:payload]
        # Deposit up to a window's worth eagerly; a longer record streams
        # through the circular window, refilled per fill-level poll
        # (:meth:`on_level_poll`). Observed: the firmware polls the level once
        # before every 64-byte chunk it copies (§7/§8.1) — 16 polls for the
        # boot loader's 1024-byte records, wrapping the window twice.
        assert self.machine is not None
        self.machine.write_bytes(self.sram_window, data[:SRAM_WINDOW_SIZE])
        self._stream = data if len(data) > SRAM_WINDOW_SIZE else None
        self._stream_polls = 0
        self.l2_level = 8

    def _data_program(self, nbytes: int) -> None:
        """0x129: arm a program record.

        The CPU stages the record through the circular window in <=512-B
        slabs, polling the drain (level == 0) once after each slab —
        :meth:`on_level_poll` captures them; the L2 flush strobe commits the
        assembled record (§8.2 steps b–d).
        """
        self._pend_prog = self.ecc.payload_len or min(nbytes, SECTOR_SIZE)
        self._pend_prog_eccsize = _sector_eccsize(nbytes, self._pend_prog)
        self._prog_staged = bytearray()
        self.l2_level = 0  # program path polls level == 0 (drained), §7

    def on_level_poll(self) -> None:
        """A BUF_STATUS fill-level read: advance the record streaming through
        the 512-B circular window (§7), in whichever direction is armed.

        **Read path**: the k-th poll since the data op precedes the CPU's copy
        of 64-byte chunk k-1 (Observed, one poll per chunk). Chunks 0..7 were
        deposited eagerly; each later poll deposits the chunk about to be
        consumed at its wrapped window offset — modelling the controller
        refilling the circular buffer as the CPU drains it.

        **Program path**: the CPU memcpys each <=512-B slab of the armed
        record to window offset 0, then polls the drain once (Observed:
        ``(memcpy 512, poll) × 2, strobe`` for the runtime driver's 1024-B
        records; §8.2 step c). The poll is the moment the controller has
        consumed the slab — capture it into the staged-record buffer, or a
        longer record would wrap the window and overwrite its first half
        (the write round-trip corruption of a capture-at-strobe model).
        """
        if self._pend_prog:
            remaining = self._pend_prog - len(self._prog_staged)
            if remaining > 0:
                assert self.machine is not None
                chunk = min(remaining, SRAM_WINDOW_SIZE)
                self._prog_staged += self.machine.read_bytes(self.sram_window, chunk)
            return
        if self._stream is None:
            return
        chunk = self._stream_polls  # 0-based index of the chunk copied next
        self._stream_polls += 1
        off = chunk * 64
        if chunk >= SRAM_WINDOW_SIZE // 64:
            assert self.machine is not None
            self.machine.write_bytes(
                self.sram_window + (off & (SRAM_WINDOW_SIZE - 1)),
                self._stream[off : off + 64],
            )
        if off + 64 >= len(self._stream):
            self._stream = None

    def l2_strobe(self, op: int) -> None:
        """BUF_CTRL strobe on buffer 4 (from :class:`L2NandBuffer`, §7/§10).

        op 1 = flush/commit the armed program record — the slabs drained at
        the level polls, plus any tail still sitting in the window (§8.2
        steps c–d guarantee a poll per slab, so the tail is at most one
        window's worth): >= 512 B → program the next data record at the
        decoded offset; < 512 B → store the row's tag. op 0 = attach/reset
        (clears the level).
        """
        self.l2_level = 0
        if op != 1 or not self._pend_prog:
            return
        assert self.machine is not None
        payload = self._pend_prog
        self._pend_prog = 0
        data = bytes(self._prog_staged[:payload])
        self._prog_staged = bytearray()
        while len(data) < payload:  # un-polled tail (a slab per iteration)
            data += self.machine.read_bytes(
                self.sram_window, min(payload - len(data), SRAM_WINDOW_SIZE)
            )
        if payload >= SECTOR_SIZE:
            offset = decode_byte_offset(self._row, self._col, self._pend_prog_eccsize)
            self.flash.program(offset + self._bytes_done, data)
            self._bytes_done += payload
        else:
            self.flash.set_tag(tag_key(self._row), data)

    # --- copy-back (§8.6) -----------------------------------------------------------------

    def _copy_back(self) -> None:
        """Copy one program unit — AU_SECS sectors *and the unit's tag* — src → dst.

        The data never crosses the SRAM window; a copy-back that loses the tag
        breaks the next mount (§8.6).
        """
        if self._cb_src is None:
            log.warning("nfc: copy-back confirm without a latched source")
            return
        src_row, src_col = self._cb_src
        self._cb_src = None
        src = decode_byte_offset(src_row, src_col, self._pend_prog_eccsize)
        dst = decode_byte_offset(self._row, self._col, self._pend_prog_eccsize)
        self.flash.program(dst, self.flash.read(src, AU_SIZE))
        self.flash.set_tag(tag_key(self._row), self.flash.get_tag(tag_key(src_row)))


# --- L2 buffer controller 0x04010000 (§7) ----------------------------------------------

L2_BUF_CTRL = 0x00
L2_BUF_STATUS = 0x10
DMA_WORDCOUNT = 0x0C   # audio: bit13 = START/BUSY, reads 0 when idle
DMA_SPURIOUS = 0x1C    # audio ISR spurious check, reads 0


class L2NandBuffer(WordRegisterPeripheral):
    """L2 buffer controller: buffer-4 strobes + fill level for the NAND path (§7).

    Also keeps the audio-DMA idle defaults previously served by the DmaStub
    (``audio-dac-dma.md`` §2: +0x0c bit13 reads clear; +0x1c reads 0) — the
    block is shared between audio DMA and NAND staging (§1); full audio DMA is
    a later task.
    """

    name = "l2dma"
    base = 0x0401_0000

    def __init__(self, nfc: NfcController) -> None:
        super().__init__()
        self._nfc = nfc

    @property
    def regions(self) -> tuple[MmioRegion, ...]:
        return (MmioRegion(self.base, 0x1000),)

    def read_reg(self, offset: int) -> int:
        if offset == L2_BUF_STATUS:
            self._nfc.on_level_poll()  # streaming refill of >512-B records
            return (self._nfc.l2_level & 0xF) << 16  # bits[19:16] = fill in 64-B chunks
        if offset == DMA_WORDCOUNT:
            return super().read_reg(offset) & ~(1 << 13)
        if offset == DMA_SPURIOUS:
            return 0
        return super().read_reg(offset)

    def write_reg(self, offset: int, value: int) -> None:
        super().write_reg(offset, value)
        if offset == L2_BUF_CTRL and value & 0x800:  # bit11 = strobe (§7)
            buf = (value >> 8) & 7
            op = (value >> 12) & 0xF
            if buf == 4:
                self._nfc.l2_strobe(op)
