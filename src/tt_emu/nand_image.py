"""NAND backing store + bootable-image builder.

Two halves:

* :class:`NandImage` — the flat-image/tag-store backing model of
  ``nand-and-nfc-controller.md`` §10: a sparse 4096-block data image
  (``0xFF``-filled where unwritten = erased), a per-AU 8-byte tag map, and the
  program/erase/copy-back layering rules (programs overlay static content,
  erase shadows data *and* tags — §9/§10 and ``nand-image-layout.md`` §6 step 7).
* :func:`build_nand_image` — the image-builder recipe of
  ``nand-image-layout.md`` §6: boot blob, system bins (PROG + codepage, flat),
  the block-0 metadata rows (zone/partition table with AddrCnt in **allocation
  units**, bin header/entry table, per-bin maplists), NFTL head tags, and the
  two FAT16 superfloppy partitions A: (system) / B: (user).

Addressing note: the metadata rows 200/201/253/254/255 live in the sparse row
space of block 0 (au > 31 — the open point of ``nand-image-layout.md`` §2.1/§7).
Under the controller's §4.2 decode they land at flat byte offsets
``row * 0x1000`` (blocks 6/7, unused by this layout), so the builder simply
*places them there* and the controller needs no metadata special case.
"""

from __future__ import annotations

import struct
from typing import Iterable, Mapping

from .fat16 import Fat16Volume, build_fat16
from .loader import Firmware

__all__ = [
    "NandImage",
    "build_nand_image",
    "make_head_tag",
    "BLOCK_SIZE",
    "NUM_BLOCKS",
    "SECTOR_SIZE",
    "AU_SIZE",
    "FS_START",
    "A_BLOCKS",
    "B_BLOCKS",
]

# --- Geometry (nand-and-nfc-controller.md §2) --------------------------------------

SECTOR_SIZE = 512
PAGE_SIZE = 2048
AU_SIZE = 4096              #: allocation unit = 8 sectors = 2 pages
AUS_PER_BLOCK = 32
BLOCK_SIZE = 0x2_0000       #: 128 KiB data per block
SECTORS_PER_BLOCK = 256
NUM_BLOCKS = 4096           #: 512 MiB device

# --- Layout constants (nand-image-layout.md §1.2/§6 — Inferred placement,
#     Observed to boot; the firmware reads fs_start etc. from the image itself) ------

BOOT_BLOCK = 1              #: SPL/boot blob, single copy
SYS_START = 8               #: first bin block (PROG)
PROG_BLOCKS = 28            #: PROG = 0x380000 B exactly
CODEPAGE_BLOCKS = 7         #: codepage = 0xd6ccc B, 0xFF-padded
FS_START = 134              #: first block of the NFTL FS area
A_BLOCKS = 480              #: partition A span (AddrCnt 0x3C00 AU)
B_BLOCKS = 1024             #: partition B span (AddrCnt 0x8000 AU)
A_ADDRCNT = A_BLOCKS * AUS_PER_BLOCK   # 0x3C00
B_ADDRCNT = B_BLOCKS * AUS_PER_BLOCK   # 0x8000

#: FAT volume sizes (§6 step 4: A 60 MiB = full span; B 64 MiB example size).
A_VOLUME_BYTES = A_BLOCKS * BLOCK_SIZE
B_VOLUME_BYTES = 64 * 1024 * 1024

#: Metadata rows (nand-image-layout.md §2.1; map rows 200/201 are the free
#: emulator choice named in the bin entry table).
ROW_ZONE_TABLE = 255
ROW_BIN_HEADER = 254
ROW_BIN_ENTRIES = 253
ROW_MAP_PROG = 200
ROW_MAP_CODEPAGE = 201
METADATA_PAYLOAD = 0x1000

#: Factory bad-block bitmap row — DOC GAP (not in ``nand-image-layout.md``;
#: found empirically): ``mtd_init``'s InitPlane reads a 0x1000-byte bitmap at
#: row 2 (one bit per block, MSB-first within each byte, **bit set = factory
#: bad**) with a small replica-counter spare tag, before the partition build.
#: An erased page there (all 0xFF) marks *every* block factory-bad ("MtdLib -
#: InitPlane:: factory badblk" per block) and the mount loops forever. A
#: zero-filled bitmap = no bad blocks. This is presumably the runtime home of
#: the ASA bad-block data (§2.5), which the doc says can be omitted — true for
#: the boot loader, not for the mount.
ROW_BADBLOCK_BITMAP = 2

#: 4-byte spare magics of the metadata rows (§2.1).
MAGIC_MAP = 0x12121212
MAGIC_BIN_INFO = 0x34343434
MAGIC_BIN_HEADER = 0x56565656
MAGIC_ZONE_TABLE = 0x5A5A5A5A

_ERASED_BLOCK = bytes([0xFF]) * BLOCK_SIZE
BLANK_TAG = b"\xff" * 8


def make_head_tag(logical_block: int, *, seq: int = 1, own: int = 0xFFFF) -> bytes:
    """Build an NFTL chain-head spare tag (``nand-image-layout.md`` §4.1/§6).

    ``[0]`` = seq (bit 7 **clear** — set means obsolete), ``[1]`` = 0,
    ``[2:4]`` = 0xFFFD (chain end), ``[4:6]`` = logical | 0x8000 (head-valid),
    ``[6:8]`` = own/id (ignored by every consumer; firmware writes 0xFFFF).
    """
    if seq & 0x80:
        raise ValueError("seq bit 7 marks the copy obsolete (§4.1)")
    return struct.pack("<BBHHH", seq, 0, 0xFFFD, (logical_block & 0x7FFF) | 0x8000, own)


class NandImage:
    """Sparse NAND backing store: data blocks + per-AU spare tags (§10).

    Data is addressed by **flat byte offset** into the concatenated 2048-byte
    data pages (the controller's §4.2 decode produces these offsets); tags are
    keyed by the AU's flat 512-B sector base ``256*block + 8*au`` ("keyed by
    the row base, never by sub-sector", §4.2). Unwritten blocks read erased
    (``0xFF`` data, blank tags); an erase drops a block's data *and* tags so it
    shadows any placed content (§6 step 7).
    """

    def __init__(self) -> None:
        self._blocks: dict[int, bytearray] = {}
        self._tags: dict[int, bytes] = {}
        self.reads = 0
        self.programs = 0
        self.erases = 0

    # --- data ------------------------------------------------------------------------

    def read(self, offset: int, length: int) -> bytes:
        """Read ``length`` bytes at flat data ``offset`` (erased regions = 0xFF)."""
        self.reads += 1
        out = bytearray()
        while length > 0:
            block, boff = divmod(offset, BLOCK_SIZE)
            chunk = min(length, BLOCK_SIZE - boff)
            data = self._blocks.get(block)
            if data is None:
                out += _ERASED_BLOCK[boff : boff + chunk]
            else:
                out += data[boff : boff + chunk]
            offset += chunk
            length -= chunk
        return bytes(out)

    def program(self, offset: int, data: bytes) -> None:
        """Program (write) ``data`` at flat ``offset``; last write wins (§8.2)."""
        self.programs += 1
        pos = 0
        while pos < len(data):
            block, boff = divmod(offset + pos, BLOCK_SIZE)
            chunk = min(len(data) - pos, BLOCK_SIZE - boff)
            blk = self._blocks.get(block)
            if blk is None:
                blk = bytearray(_ERASED_BLOCK)
                self._blocks[block] = blk
            blk[boff : boff + chunk] = data[pos : pos + chunk]
            pos += chunk

    def erase_block(self, block: int) -> None:
        """Erase a 128-KiB block: data reads 0xFF, all its tags dropped (§8.3)."""
        self.erases += 1
        self._blocks.pop(block, None)
        base = block * SECTORS_PER_BLOCK
        for key in [k for k in self._tags if base <= k < base + SECTORS_PER_BLOCK]:
            del self._tags[key]

    # --- tags -----------------------------------------------------------------------

    def get_tag(self, sector_base: int) -> bytes:
        """The 8-byte spare tag of the AU at ``sector_base`` (blank = erased)."""
        return self._tags.get(sector_base, BLANK_TAG)

    def set_tag(self, sector_base: int, tag: bytes) -> None:
        self._tags[sector_base] = bytes(tag[:8].ljust(8, b"\xff"))

    def tag_count(self) -> int:
        return len(self._tags)

    # --- builder helpers --------------------------------------------------------------

    def place(self, offset: int, data: bytes) -> None:
        """Static placement (same layering as a program; named for clarity)."""
        self.program(offset, data)

    def tag_au(self, block: int, au: int, tag: bytes) -> None:
        self.set_tag(block * SECTORS_PER_BLOCK + au * (AU_SIZE // SECTOR_SIZE), tag)


# --- Metadata payload builders (nand-image-layout.md §2.2/§2.3, §6 step 6) ----------


def _zone_table_payload() -> bytes:
    """Row 255: the zone/partition table ``fs_storage_mount_init`` parses (§2.2).

    Two zones (A, B); AddrCnt in **allocation units** (the ★ box of §2.2 —
    scaled by f = AU/512 = 8 at mount time); fake-zone map at offset TotalLen
    with zero withheld blocks (full capacity).
    """
    p = bytearray(METADATA_PAYLOAD)
    struct.pack_into("<I", p, 0x000, 0x200)       # TotalLen = offset of fake-zone map
    struct.pack_into("<I", p, 0x004, FS_START)    # fs_start_block
    # u16 @0x008 reserved-size arg = 0
    p[0x00A] = 2                                  # nzones
    # Zone_Group[0] = A: (system) — Type 2 UNSTANDARD, Symbol 0
    struct.pack_into("<II", p, 0x010, 0, A_ADDRCNT)   # StartAddr (unused), AddrCnt
    p[0x018] = 1                                  # Subarea_Flag
    p[0x01A] = 2                                  # Type UNSTANDARD
    p[0x01B] = 0                                  # Symbol 'A' (the mount's lookup key)
    p[0x01D] = 0                                  # Partition_NO
    # Zone_Group[1] = B: (user) — Type 4 STANDARD, Symbol 1
    struct.pack_into("<II", p, 0x020, A_ADDRCNT, B_ADDRCNT)
    p[0x028] = 1
    p[0x02A] = 4                                  # Type STANDARD
    p[0x02B] = 1                                  # Symbol 'B'
    p[0x02D] = 1
    # Fake-zone / bad-block map: 1 group, 0 blocks withheld → full capacity.
    struct.pack_into("<HHHH", p, 0x200, 1, 6, 0, 0)
    return bytes(p)


def _bin_header_payload() -> bytes:
    """Row 254: bin-info header (§2.3 header form)."""
    p = bytearray(METADATA_PAYLOAD)
    struct.pack_into("<I", p, 0x04, 2)                            # entry count
    struct.pack_into("<I", p, 0x08, SYS_START)                    # bin region start block
    struct.pack_into("<I", p, 0x0C, PROG_BLOCKS + CODEPAGE_BLOCKS)  # total bin blocks
    return bytes(p)


def _bin_entries_payload() -> bytes:
    """Row 253: bin entry table, one 0x24-B record per bin (§2.3)."""
    p = bytearray(METADATA_PAYLOAD)
    for i, (nblocks, map_row, name) in enumerate(
        ((PROG_BLOCKS, ROW_MAP_PROG, b"PROG"), (CODEPAGE_BLOCKS, ROW_MAP_CODEPAGE, b"codepage"))
    ):
        rec = i * 0x24
        struct.pack_into("<I", p, rec + 0x00, nblocks * SECTORS_PER_BLOCK)  # size in sectors
        struct.pack_into("<I", p, rec + 0x08, map_row)                      # map row
        p[rec + 0x14 : rec + 0x14 + len(name)] = name                       # NUL-terminated
    return bytes(p)


def _maplist_payload(start_block: int, nblocks: int) -> bytes:
    """Rows 200/201: per-bin log2phy map — {u16 origin, u16 backup} per chunk.

    Single-copy image: both u16 identical, never 0 (§2.3/§2.4).
    """
    p = bytearray(METADATA_PAYLOAD)
    for j in range(nblocks):
        struct.pack_into("<HH", p, j * 4, start_block + j, start_block + j)
    return bytes(p)


def _metadata_rows() -> Iterable[tuple[int, int, bytes]]:
    """(row, spare magic, 0x1000-byte payload) for §6 step 6."""
    yield ROW_MAP_PROG, MAGIC_MAP, _maplist_payload(SYS_START, PROG_BLOCKS)
    yield ROW_MAP_CODEPAGE, MAGIC_MAP, _maplist_payload(SYS_START + PROG_BLOCKS, CODEPAGE_BLOCKS)
    yield ROW_BIN_ENTRIES, MAGIC_BIN_INFO, _bin_entries_payload()
    yield ROW_BIN_HEADER, MAGIC_BIN_HEADER, _bin_header_payload()
    yield ROW_ZONE_TABLE, MAGIC_ZONE_TABLE, _zone_table_payload()


# --- Placement helpers ---------------------------------------------------------------


def _place_bin(img: NandImage, data: bytes, start_block: int, *, logical_base: int) -> None:
    """Place a system bin flat from ``start_block`` with identity head tags (§6 step 3)."""
    img.place(start_block * BLOCK_SIZE, data)
    nblocks = (len(data) + BLOCK_SIZE - 1) // BLOCK_SIZE
    for b in range(nblocks):
        block = start_block + b
        placed = min(len(data) - b * BLOCK_SIZE, BLOCK_SIZE)
        for au in range((placed + AU_SIZE - 1) // AU_SIZE):
            img.tag_au(block, au, make_head_tag(logical_base + b, own=block))


def _place_fat_volume(img: NandImage, vol: Fat16Volume, start_block: int) -> int:
    """Place a FAT16 volume at ``start_block`` per §6 step 5; return blocks placed.

    128-KiB slices that are entirely 0x00 or 0xFF are skipped (unallocated
    clusters stay erased = free pool), **except** slices overlapping the FAT
    system area (reserved + FATs + root directory), which the firmware reads
    even when zero-filled — skipping one would serve 0xFF for real FAT/root
    sectors. Head-tag logical numbers are relative to FS_START for both
    partitions (§6 step 5).
    """
    sys_bytes = vol.system_sectors * SECTOR_SIZE
    placed = 0
    for s in range(0, len(vol.data), BLOCK_SIZE):
        chunk = bytes(vol.data[s : s + BLOCK_SIZE])
        forced = s < sys_bytes
        if not forced and (chunk.count(0) == len(chunk) or chunk.count(0xFF) == len(chunk)):
            continue
        block = start_block + s // BLOCK_SIZE
        img.place(block * BLOCK_SIZE, chunk)
        for au in range((len(chunk) + AU_SIZE - 1) // AU_SIZE):
            img.tag_au(block, au, make_head_tag(block - FS_START))
        placed += 1
    return placed


# --- The recipe (nand-image-layout.md §6) --------------------------------------------


def build_nand_image(
    firmware: Firmware,
    *,
    a_files: Mapping[str, bytes] | None = None,
    b_files: Mapping[str, bytes] | None = None,
) -> NandImage:
    """Build a bootable NAND image from the .upd artifacts (§6 steps 1–6).

    ``a_files`` / ``b_files`` are {relative/path: bytes} trees for the A:
    (system) and B: (user — the ``.gme`` games) partitions; both may be empty
    (an empty formatted A: boots and discovers games, §5.1).
    """
    img = NandImage()

    # Step 2 — boot blob at block 1 (0xFF-padded by the sparse store).
    img.place(BOOT_BLOCK * BLOCK_SIZE, firmware.nandboot.data)

    # Step 3 — system bins, flat, identity head tags. The codepage bin **must**
    # be present: codepage_load reads it from NAND during mount (a hard boot
    # precondition, memory-map-and-boot.md §5.7).
    if len(firmware.prog.data) != PROG_BLOCKS * BLOCK_SIZE:
        raise ValueError(f"PROG size {len(firmware.prog.data):#x} != 28 blocks")
    _place_bin(img, firmware.prog.data, SYS_START, logical_base=0)
    _place_bin(img, firmware.codepage.data, SYS_START + PROG_BLOCKS, logical_base=PROG_BLOCKS)

    # Step 4 — FAT16 superfloppies (pure Python, §5 checks enforced inside).
    vol_a = build_fat16(A_VOLUME_BYTES, label="SYSTEM", files=a_files)
    vol_b = build_fat16(B_VOLUME_BYTES, label="tiptoi", files=b_files)

    # Step 5 — place them: A at FS_START, B at FS_START + A_BLOCKS.
    _place_fat_volume(img, vol_a, FS_START)
    _place_fat_volume(img, vol_b, FS_START + A_BLOCKS)

    # Step 6 — metadata rows. Their §4.2-decoded flat offsets are row*0x1000
    # (block 0's sparse row space maps into unused blocks 6/7 — module docstring).
    for row, magic, payload in _metadata_rows():
        img.place(row * AU_SIZE, payload)
        img.set_tag(row * (AU_SIZE // SECTOR_SIZE), struct.pack("<I", magic) + b"\xff" * 4)

    # Factory bad-block bitmap at row 2 (see ROW_BADBLOCK_BITMAP doc-gap note):
    # all-zero = no factory bad blocks; tag u32 = replica/"times" counter.
    img.place(ROW_BADBLOCK_BITMAP * AU_SIZE, bytes(METADATA_PAYLOAD))
    img.set_tag(
        ROW_BADBLOCK_BITMAP * (AU_SIZE // SECTOR_SIZE),
        struct.pack("<I", 1) + b"\xff" * 4,
    )

    return img
