"""Hand-built ZC3201 (1st-gen) NAND image — the small-page ``MtdLib_Base`` layout.

The 2nd-gen "MT" pen is fed its games by :func:`tt_emu.nand_image.build_nand_image`,
which lays down MT's large-page NFTL on-media format + FAT16 A:/B: volumes for the
*unmodified* firmware to mount. This module is the ZC3201 twin: it lays down the
**small-page ``MtdLib`` format** the 1st-gen firmware's mount path validates, for a
Samsung **K9F5608** (512-byte page + 16-byte OOB, 32 pages/block, 2048 blocks =
32 MiB).

The acceptance spec is the firmware's own mount path (``docs/zc3201-boot-feasibility.md``
Leg 10; the RE is in ``docs/zc3201-producer-addresses.md`` §9-12 + the decomp):

* ``fs_storage_mount_init`` ``0x0802d0e0`` → ``mtd_helper59`` ``0x0802d408`` installs
  the device op-vtable, then the **map read** ``(*(dev+0x2c))(dev,0,0,dev+0x14-1,buf)``
  = ``FUN_0800c208`` reads **block 0, page 31** (``abs = 32·0 + 31``) — the
  **superblock**. The mount consumes ``buf+4`` = reserve-block count, ``buf+8`` = the
  shift, ``buf+0xa`` = partition count, and a stride-0x10 partition-entry array at
  ``buf+0x10`` (``FUN_0802ccf0`` ``0x0802ccf0``: entry ``+4`` = size in 512-B sectors,
  ``+0xb`` = partition id; id 0 = A: SYSTEM, id 1 = B: user).
* the whole-disk map object (``FUN_0802f0c0``) spans blocks ``[reserve, 2048)``; the
  map-table scan (``FUN_0802cbd8`` → ``FUN_0802edb8``) reads each mapped page's 4-byte
  OOB tag and classifies ``tag & 0xFFFF0000 == 0x12560000`` as a **valid mapped page
  whose low 16 bits are its logical page address** (``0x1256`` = the live-data OOB
  magic; erased ``0xFFFFFFFF`` reads back "blank/unmapped").
* A: and B: are then mounted as **FAT16** (``FUN_0802c09c``/``FUN_0802c35c``): each
  needs a valid boot sector — ``0x55AA`` at ``0x1FE`` and ``"FAT16   "`` at ``0x36``
  — at its logical sector 0.

Geometry note: the device geometry (``dev+0x14`` = 32 pages/block, ``dev+0x1c`` = 512
page size, ``dev+8·dev+0x10`` = 2048 blocks) is populated by the **nandboot chip-detect
HAL** from the READ-ID → ``flash_ic`` table (``update.upd[0x200]``, K9F5608), not from
NAND content — so the emulator only has to serve READ-ID ``0xBDA575EC`` and lay down
the map/FS metadata below.
"""

from __future__ import annotations

import struct
from typing import Mapping

from .fat16 import build_fat16
from .nand_image import NandImage

__all__ = [
    "build_zc3201_nand_image",
    "ZC_PAGE",
    "ZC_OOB",
    "ZC_PAGES_PER_BLOCK",
    "ZC_BLOCK",
    "ZC_NBLOCKS",
    "ZC_RESERVE_BLOCKS",
    "ZC_PLANES",
    "ZC_BLOCKS_PER_PLANE",
    "ZC_SPARE_BLOCKS",
    "MAP_OOB_MAGIC",
    "MAP_FREE_SENTINEL",
    "MAP_TAG_PAGE",
    "BBT_PAGES",
]

# --- K9F5608 geometry (update.upd flash_ic row; docs §10) ----------------------------

ZC_PAGE = 512               #: bytes per page
ZC_OOB = 16                 #: spare/OOB bytes per page
ZC_PAGES_PER_BLOCK = 32     #: → 16 KiB erase block
ZC_BLOCK = ZC_PAGE * ZC_PAGES_PER_BLOCK  # 0x4000
ZC_NBLOCKS = 2048           #: 32 MiB device
ZC_TOTAL_PAGES = ZC_NBLOCKS * ZC_PAGES_PER_BLOCK

#: Reserve blocks withheld below the FS map area (BurnTool ``config_researcher.txt``:
#: "64 reserve blocks"). The whole-disk map spans blocks ``[ZC_RESERVE_BLOCKS, 2048)``.
ZC_RESERVE_BLOCKS = 64

#: The FS-info superblock lives at the **last page of block 0** (``dev+0x14-1`` = 31),
#: with redundant copies at pages 29/30 (``disk_sector_io`` ``0x0802f694`` reads the
#: last three pages of block 0).
SUPERBLOCK_BLOCK = 0
SUPERBLOCK_PAGES = (29, 30, 31)
SHIFT = 9  #: log2(512) — the page/sector shift the mount reads at ``buf+8``.

#: The on-media **bad-block table (BBT)**. The map-table build gates its OOB scan
#: (``FUN_0802edb8`` ``0x0802edb8``) on a per-page bad-block check (``dev+0x40`` =
#: ``FUN_08030108`` ``0x08030108``), which — on first use — *reads the BBT from
#: NAND* via the nandboot leaf ``func_0x080006fc`` ``0x080006fc`` into a manager
#: bitmap. That leaf reads absolute pages from ``32·flash_ic[0] + flash_ic[2] +
#: block`` (``flash_ic`` = the descriptor at ``0x08007c3c``); with the from-entry
#: boot's zeroed ``flash_ic`` (``[0]=[2]=0``) it reads **device pages 0..3**, and
#: the manager bitmap is exactly ``page-0 bytes [0:256]`` (2048 blocks / 8). A byte
#: of **0x00** there = all-good; the blank ``0xFF`` we would otherwise serve marks
#: every block bad → the scan reads no OOB tag → ``map_table_build`` returns NULL →
#: ``fs_storage_mount_init`` hangs at ``0x0802d208``. So an authentic formatted
#: image lays a zeroed BBT across block-0 pages 0..3. (This mirrors what the
#: producer's format writes into the reserve zone; the read address tracks the
#: seeded ``flash_ic`` descriptor — see ``docs/zc3201-boot-feasibility.md`` Leg 11.)
BBT_PAGES = (0, 1, 2, 3)

#: OOB map magic: a mapped (live) **block** carries ``0x12560000 | logical_block``
#: in the 4-byte OOB tag of its **map-tag page** (``FUN_0802edb8`` ``0x0802edb8``
#: classifier). Recovered by tracing the readspare leaf ``FUN_08030224``
#: ``0x08030224`` → ``nandboot 0x08002bac`` and the classifier: the scan reads,
#: per logical scan-index ``B`` of a partition, **page ``32·phys + 28``** (the
#: ``dev[0x14]-4`` tag page) of physical block ``phys``, column 0, then strobes
#: GPIO-out bit 3 (``0x080030d8``) to surface that page's **OOB[0:4]** into the
#: NAND window head (``NfcController.surface_spare``) and reads the tag there.
#: Physical block for scan-index ``B`` = ``planes·reserve + planes·B + plane``
#: (plane 0 = even blocks from ``2·reserve``); the tag's low 16 bits are the
#: **logical block** ``B``.
MAP_OOB_MAGIC = 0x1256_0000

#: Free/spare-block sentinel (``DAT_0802f0a0``): a block whose map-tag page OOB
#: carries ``0x12345678`` is a free spare, added to the partition's free-block
#: ring (``FUN_0802e574`` → consumed by ``mtd_helper_d244`` ``0x08035244``).
#: A partition needs ~9 of these (``hi − lo``) or ``mtd_MapTblInit`` cannot
#: allocate a map-table home and the mount fails ("MtdLib15:0").
MAP_FREE_SENTINEL = 0x1234_5678

#: The map-tag page inside each block: ``dev[0x14] − 4`` = 32 − 4 = 28. The
#: readspare computes ``page = dev[0x14]·B + (param_4 ÷ dev[0x18])`` with
#: ``param_4 = dev[0x14]−4 = 28``, ``dev[0x18] = 1`` → ``page = 32·B + 28``.
MAP_TAG_PAGE = 28

#: Small-page geometry the mount derives (dev fields + superblock): 2 planes,
#: 1024 blocks/plane. Per plane a partition spans ``1024 − reserve = 960``
#: usable blocks (``hi``), of which the top ``9`` are free spares → ``lo = 951``
#: valid logical blocks. (docs/zc3201-boot-feasibility.md Leg 12.)
ZC_PLANES = 2
ZC_BLOCKS_PER_PLANE = 1024
ZC_SPARE_BLOCKS = 9


def _superblock_payload(reserve_blocks: int, a_sectors: int, b_sectors: int) -> bytes:
    """The 512-byte FS-info superblock (block 0, page 31) the mount consumes.

    ``buf+4`` reserve-block count, ``buf+8`` shift (u16), ``buf+0xa`` partition count
    (u8), then a stride-0x10 partition-entry array at ``buf+0x10``: entry ``+4`` = size
    in 512-B sectors (u32), ``+0xb`` = partition id (u8). ``buf+0`` (a header magic the
    mount does **not** read) is left 0 — see ``docs`` open-ambiguity note.
    """
    p = bytearray(ZC_PAGE)
    struct.pack_into("<I", p, 0x04, reserve_blocks)
    struct.pack_into("<H", p, 0x08, SHIFT)
    p[0x0A] = 2  # partition count
    # entry[0] = A: (SYSTEM), id 0
    struct.pack_into("<I", p, 0x10 + 0x04, a_sectors)
    p[0x10 + 0x0B] = 0
    # entry[1] = B: (tiptoi/user), id 1
    struct.pack_into("<I", p, 0x20 + 0x04, b_sectors)
    p[0x20 + 0x0B] = 1
    return bytes(p)


def _place_page(img: NandImage, abs_page: int, data: bytes, oob: bytes) -> None:
    """Place one 512-byte page + its 16-byte OOB tag at ``abs_page`` (row-keyed)."""
    img.place(abs_page * ZC_PAGE, (bytes(data) + b"\xff" * ZC_PAGE)[:ZC_PAGE])
    img.set_tag(abs_page, (bytes(oob) + b"\xff" * ZC_OOB)[:ZC_OOB])


def _phys_block(logical_block: int, plane: int) -> int:
    """Physical block holding ``logical_block`` of ``plane`` (the map-scan formula).

    ``phys = planes·reserve + planes·logical + plane`` — plane 0 = even blocks
    starting at ``2·reserve`` (= 128 for reserve 64), plane 1 = the odd blocks.
    """
    return ZC_PLANES * ZC_RESERVE_BLOCKS + ZC_PLANES * logical_block + plane


def _lay_map_tags(img: NandImage, plane: int) -> None:
    """Lay one plane's per-block map tags at each block's map-tag page (28) OOB.

    Logical blocks ``0..lo-1`` are live (``0x12560000 | logical``); ``lo..hi-1``
    are free spares (``0x12345678``) so the mount's free-block ring is non-empty.
    """
    hi = ZC_BLOCKS_PER_PLANE - ZC_RESERVE_BLOCKS      # 960 usable blocks/plane
    lo = hi - ZC_SPARE_BLOCKS                          # 951 valid logical blocks
    for logical in range(hi):
        phys = _phys_block(logical, plane)
        row = phys * ZC_PAGES_PER_BLOCK + MAP_TAG_PAGE
        tag = MAP_OOB_MAGIC | logical if logical < lo else MAP_FREE_SENTINEL
        img.set_tag(row, struct.pack("<I", tag))


def _place_volume(img: NandImage, volume: bytes, plane: int) -> int:
    """Place a FAT16 volume identity-mapped onto ``plane``'s live blocks.

    FAT logical block ``L`` (16 KiB) lands at physical block ``_phys_block(L,
    plane)``, matching the map so a post-mount logical read resolves to the data.
    Returns the number of logical blocks placed.
    """
    nblocks = (len(volume) + ZC_BLOCK - 1) // ZC_BLOCK
    for L in range(nblocks):
        phys = _phys_block(L, plane)
        img.place(phys * ZC_BLOCK, volume[L * ZC_BLOCK : (L + 1) * ZC_BLOCK])
    return nblocks


def build_zc3201_nand_image(
    firmware: object,
    *,
    a_files: Mapping[str, bytes] | None = None,
    b_files: Mapping[str, bytes] | None = None,
    a_blocks: int = 512,
    b_blocks: int = 1024,
) -> NandImage:
    """Build a mountable ZC3201 NAND image (small-page ``MtdLib`` + FAT16 A:/B:).

    ``a_files`` / ``b_files`` are ``{relative/path: bytes}`` trees for A: (SYSTEM) and
    B: (the user ``.gme``). ``a_blocks`` / ``b_blocks`` size the two FAT16 volumes (in
    16-KiB NAND blocks); both partitions live in the mapped region ``[reserve, 2048)``.
    """
    img = NandImage()

    a_bytes = a_blocks * ZC_BLOCK
    b_bytes = b_blocks * ZC_BLOCK
    # 512-B page == 1 FAT sector, so no allocation-unit coupling is needed (au=1)
    # and a 1-sector cluster keeps the FAT16 cluster count in range for these
    # 8/16-MiB volumes.
    vol_a = build_fat16(a_bytes, label="SYSTEM", files=a_files,
                        sectors_per_cluster=1, au_sectors=1)
    vol_b = build_fat16(b_bytes, label="tiptoi", files=b_files,
                        sectors_per_cluster=1, au_sectors=1)

    a_sectors = a_bytes // ZC_PAGE
    b_sectors = b_bytes // ZC_PAGE

    # Bad-block table (block 0, pages 0..3) — an all-good (zeroed) BBT the mount's
    # per-page bad-block check reads from NAND before scanning OOB tags. Without it
    # every block reads "bad" and the map-table build returns NULL (see BBT_PAGES).
    for page in BBT_PAGES:
        _place_page(img, SUPERBLOCK_BLOCK * ZC_PAGES_PER_BLOCK + page,
                    b"\x00" * ZC_PAGE, struct.pack("<I", 0x1234_5678))

    # Superblock (block 0, last pages) — read by the map read + FUN_0802ccf0.
    sb = _superblock_payload(ZC_RESERVE_BLOCKS, a_sectors, b_sectors)
    for page in SUPERBLOCK_PAGES:
        _place_page(img, SUPERBLOCK_BLOCK * ZC_PAGES_PER_BLOCK + page, sb,
                    struct.pack("<I", 0x1234_5678))  # system/reserved sentinel tag

    # Per-block map tags at each block's map-tag page (28) OOB, one partition
    # per plane: plane 0 (even blocks) = A:, plane 1 (odd blocks) = B:. This is
    # the on-media logical->physical map the scan (FUN_0802edb8) consumes; with
    # it the map-table build's scan is fully consistent (fe2c=1, 951 valid, 9
    # free spares — verified against the firmware's own MtdLib diagnostics).
    _lay_map_tags(img, plane=0)
    _lay_map_tags(img, plane=1)

    # FAT volumes on the mapped physical blocks (A: plane 0, B: plane 1).
    _place_volume(img, bytes(vol_a.data), plane=0)
    _place_volume(img, bytes(vol_b.data), plane=1)

    return img
