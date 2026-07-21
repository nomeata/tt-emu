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

#: The FS-info superblock lives at the **last page of block 0** (``dev+0x14-1`` = 31).
#: The mount only requires the superblock at page 31 (pages 29/30 are used by the
#: nandboot system-bin index below — Leg 16).
SUPERBLOCK_BLOCK = 0
SUPERBLOCK_PAGES = (31,)
SHIFT = 9  #: log2(512) — the page/sector shift the mount reads at ``buf+8``.

#: The nandboot **system-bin file index** the boot-file loader (``FUN_0x08000868``,
#: called by ``app_init``'s ``FUN_0x08000dcc``) reads raw from block 0 (Leg 16):
#:
#: * page **30** = the index **header**: ``+0x04`` = record count, ``+0x08`` = the
#:   bin region's start block, ``+0x0c`` = its block count.
#: * page **29** = the **records**, one ``0x24``-byte entry per bin: ``+0x00`` =
#:   size in **512-B sectors** (like MT's ``_bin_entries_payload``), ``+0x08`` =
#:   the abs-page of that bin's **block map**, ``+0x14`` = NUL-terminated name
#:   (``strcmp``-matched; a miss is a fatal ``b .`` spin at ``0x08000944``).
#: * each bin's **block map** (at ``record+0x08``) is a ``{u16 origin, u16 backup}``
#:   array indexed by logical block; the loader reads content page ``phys·32 +
#:   (pageidx & 31)`` from ``origin``.
#:
#: The loader needs the nandboot shift globals seeded (``nandboot_shift_seed``).
#: ``codepage`` is placed; its load lets ``app_init`` reach the statechart INIT
#: leaf ``state_init_power_on`` ``0x08038e48`` and the event pump. ``font_lib`` /
#: ``ImageRes`` (requested later, in book mode) are not yet placed.
SYS_INDEX_HEADER_PAGE = 30
SYS_INDEX_RECORDS_PAGE = 29
#: Block-map pages live in block 0's free pages (BBT is pages 0..3; 29/30/31 are
#: the index/superblock), one per placed bin.
SYS_MAPLIST_PAGE0 = 4
#: The system bins' content lands in the **A: reserve** (even blocks ``[2, 128)``,
#: which the MtdLib map never assigns — A: data starts at physical block 128), so
#: it collides with neither FAT volume. Logical bin block ``L`` → physical block
#: ``SYS_CONTENT_BLOCK0 + 2·L``.
SYS_CONTENT_BLOCK0 = 2

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

#: Small-page geometry the mount derives (dev fields + superblock): 2 interleaved
#: planes (plane = block & 1). The map object (``FUN_0802f0c0``) assigns each
#: partition one plane's blocks and a per-plane reserve, exactly as the firmware's
#: own map-tag scan (``FUN_0802edb8``) reads them back (probed — Leg 13):
#:
#: * **partition 0 (A:, plane 0 = even blocks)** withholds ``ZC_RESERVE_BLOCKS``
#:   even blocks (0,2,…,126 = the boot/superblock/BBT reserve), so logical block
#:   ``L`` lands at physical block **``2·reserve + 2·L``** (=128+2L); ``hi = 960``
#:   usable, top ``9`` free spares → ``lo = 951`` valid.
#: * **partition 1 (B:, plane 1 = odd blocks)** has *no* reserve, so logical block
#:   ``L`` lands at physical block **``1 + 2·L``**; ``hi = 1024`` usable, top ``9``
#:   free spares → ``lo = 1015`` valid.
#:
#: Both partitions are addressed through the *same* plane-0 chip-select (probed:
#: the low-level GO word always sets plane bit 10, the readspare ``plane`` arg is
#: always 0) — the plane is purely the even/odd block interleave, so the flat NAND
#: image needs no plane-stride, only the two physical-block formulas above.
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


def _partition_geom(partition: int) -> tuple[int, int]:
    """``(hi, lo)`` for a partition: usable blocks and valid logical blocks.

    The counts the firmware's own map object derives (probed at ``FUN_0802edb8``
    ``arg2[0x14]``/``[0x16]``): partition 0 withholds the reserve (``hi = 960``),
    partition 1 does not (``hi = 1024``); each keeps ``ZC_SPARE_BLOCKS`` free.
    """
    hi = ZC_BLOCKS_PER_PLANE - (ZC_RESERVE_BLOCKS if partition == 0 else 0)
    return hi, hi - ZC_SPARE_BLOCKS


def _phys_block(logical_block: int, partition: int) -> int:
    """Physical block holding ``logical_block`` of ``partition`` (the map-scan formula).

    Partition 0 (plane 0, even blocks) reserves the low even blocks:
    ``phys = 2·reserve + 2·logical`` (=128+2L). Partition 1 (plane 1, odd blocks)
    has no reserve: ``phys = 1 + 2·logical``. (Probed against the firmware's own
    ``FUN_0802edb8`` readspare rows — Leg 13.)
    """
    if partition == 0:
        return ZC_PLANES * ZC_RESERVE_BLOCKS + ZC_PLANES * logical_block
    return ZC_PLANES * logical_block + 1


def _lay_map_tags(img: NandImage, partition: int) -> None:
    """Lay one partition's per-block map tags at each block's map-tag page (28) OOB.

    Logical blocks ``0..lo-1`` are live (``0x12560000 | logical``); ``lo..hi-1``
    are free spares (``0x12345678``) so the mount's free-block ring is non-empty.
    """
    hi, lo = _partition_geom(partition)
    for logical in range(hi):
        phys = _phys_block(logical, partition)
        row = phys * ZC_PAGES_PER_BLOCK + MAP_TAG_PAGE
        tag = MAP_OOB_MAGIC | logical if logical < lo else MAP_FREE_SENTINEL
        img.set_tag(row, struct.pack("<I", tag))


def _place_volume(img: NandImage, volume: bytes, logical_offset: int = 0) -> int:
    """Place a FAT16 volume into the whole-disk (even-block) map at ``logical_offset``.

    The FatLib layer mounts **both** A: and B: through the *whole-disk* map object
    (``FUN_0802f0c0``/``FUN_0802cbd8``), which is a **single contiguous even-block
    logical space**: A: occupies whole-disk logical blocks ``[0, a_blocks)`` and B:
    ``[a_blocks, a_blocks+b_blocks)`` — the FAT B: partition base (``FUN_0802ca7c``)
    is exactly A:'s block count. So FAT logical block ``L`` of a volume placed at
    ``logical_offset`` maps to **whole-disk logical block ``logical_offset + L`` →
    physical block ``128 + 2·(logical_offset + L)``** (``_phys_block`` partition 0,
    the even-block plane the whole-disk map resolves). Proven: with B: placed on the
    even blocks at ``logical_offset = a_blocks`` the firmware reads B:'s FAT16 boot
    sector at its logical sector 0 (``sig 0x55AA``, ``"FAT16   "``) and registers B:
    as the second FAT drive — whereas placing it on the odd (plane-1) blocks made
    B:'s logical sector 0 read ``0xFF`` (the whole-disk map never routes there;
    ``docs/zc3201-boot-feasibility.md`` Leg 22). Returns the block count placed.

    (The plane-1 odd-block *map tags* are still laid — the lower ``MtdLib`` InitPlane
    scan wants a second plane — but the FAT *data* for both volumes lives on the
    even-block whole-disk map.)
    """
    nblocks = (len(volume) + ZC_BLOCK - 1) // ZC_BLOCK
    for L in range(nblocks):
        logical = logical_offset + L
        phys = _phys_block(logical, partition=0)
        img.place(phys * ZC_BLOCK, volume[L * ZC_BLOCK : (L + 1) * ZC_BLOCK])
        # Ensure the live map tag is present for this whole-disk logical block
        # (``_lay_map_tags`` already tags [0, lo); this is belt-and-braces for
        # placements near the top of the mapped range).
        img.set_tag(phys * ZC_PAGES_PER_BLOCK + MAP_TAG_PAGE,
                    struct.pack("<I", MAP_OOB_MAGIC | logical))
    return nblocks


def _lay_system_bin_index(img: NandImage, bins: list[tuple[str, bytes]]) -> None:
    """Lay the nandboot system-bin file index (header p30, records p29, block maps
    + content) so the boot-file loader finds and reads each ``(name, data)`` bin.

    See :data:`SYS_INDEX_HEADER_PAGE`. Each bin's content is placed on the A:
    reserve even blocks; the record's size is in **bytes** and its block map lists
    the physical origin block per logical block.

    The record ``size`` field (``rec+0``) drives three consumers in the loader
    (``func_0x08000dcc``), all keyed to the **16-KiB block / 512-B page** geometry
    the device reports (``dev+0x14``=32, ``dev+0x1c``=512):

    * the **W1 guard** ``size >> 14 <= max`` (block count vs the caller's cap);
    * the **block-map copy** count ``r7 = 1 + (size-1) >> 14`` — the loader copies
      exactly this many ``{origin,backup}`` entries into the codepage descriptor's
      block-map (``cp+6``);
    * the **content walk** ``FUN_0x08000cd0`` reads ``1 + (size-1) >> 9`` pages.

    The codepage is then **demand-paged** at use: ``codepage_recover(off)``
    (``0x08025b7c``) indexes ``cp+6[off >> 14]`` for the origin block, reads
    ``32·origin + (off>>9 & 31)`` from NAND, and returns ``page[off & 0x1ff]``.
    So every codepage offset the UTF-16 filename converter touches needs its
    logical block's ``cp+6`` entry loaded — i.e. ``size`` big enough that
    ``r7`` spans those blocks. With the size stored as **512-B sectors** (the MT
    convention) ``r7`` collapsed to 1, so only block-map[0] was valid and any
    lookup at offset >= 16 KiB read stale block-0 data (the read *succeeds* on the
    readable block 0, so ``codepage_recover``'s reopen-fallback never fires) — the
    codepage-0 tables live at 0x3bcc..0x4fcc (blocks 0-1), so conversions emptied.
    Storing **bytes capped to ``max`` blocks** loads the full ``max``-block
    block-map (8 blocks = 128 KiB for the codepage), covering every ASCII-filename
    conversion the firmware performs.
    """
    base = SUPERBLOCK_BLOCK * ZC_PAGES_PER_BLOCK
    recs = bytearray(ZC_PAGE)
    maplist_page = SYS_MAPLIST_PAGE0
    content_block = SYS_CONTENT_BLOCK0
    for i, (name, data) in enumerate(bins):
        nblocks = max(1, (len(data) + ZC_BLOCK - 1) // ZC_BLOCK)
        rec = i * 0x24
        # Size in BYTES. When the true size exceeds the loader's per-bin block
        # limit (W1 guard: size>>14 <= max), the load *fails cleanly* (returns 0,
        # no spin — the record is still found by name) and the codepage-type flag
        # stays 0, so the firmware's path converter uses **simple byte-widening**
        # (ASCII -> UTF-16), which is correct for every tiptoi filename. This is
        # authentic: the 879-KiB codepage genuinely does not fit the 8-block
        # (128-KiB) loader, so the pen falls back to simple widening — cf. Leg 16
        # size-unit contradiction / Leg 19 "forcing flag=0 yields correct paths".
        struct.pack_into("<I", recs, rec + 0x00, len(data))            # size in bytes
        struct.pack_into("<I", recs, rec + 0x08, maplist_page)          # abs-page of block map
        enc = name.encode("ascii")
        recs[rec + 0x14 : rec + 0x14 + len(enc)] = enc
        # Block map: {u16 origin, u16 backup} per logical block, origin = physical.
        ml = bytearray(ZC_PAGE)
        for L in range(nblocks):
            phys = content_block + 2 * L
            struct.pack_into("<HH", ml, L * 4, phys, phys)
            img.place(phys * ZC_BLOCK, data[L * ZC_BLOCK : (L + 1) * ZC_BLOCK])
        _place_page(img, maplist_page, ml, struct.pack("<I", 0x1234_5678))
        maplist_page += 1
        content_block += 2 * nblocks
    _place_page(img, base + SYS_INDEX_RECORDS_PAGE, recs, struct.pack("<I", 0x1234_5678))

    hdr = bytearray(ZC_PAGE)
    struct.pack_into("<I", hdr, 0x04, len(bins))                 # record count
    struct.pack_into("<I", hdr, 0x08, SYS_CONTENT_BLOCK0)        # bin region start block
    struct.pack_into("<I", hdr, 0x0C, content_block)             # region block span
    _place_page(img, base + SYS_INDEX_HEADER_PAGE, hdr, struct.pack("<I", 0x1234_5678))


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

    # A:'s authentic factory content is the firmware's own ``to_udisk`` payload
    # (``firmware.udisk_files`` = ``VOIMG/Chomp_Voice.bin``, 4.3 MiB), exactly as
    # MT's ``build_nand_image`` merges it (nand_image.py:384). With the
    # simple-widening codepage (Leg 21) ``A:/VOIMG/Chomp_Voice.bin`` now exists, so
    # the power-on chime's ``fs_open`` succeeds and ``play_chomp_voice`` plays it.
    # (Leg 21 held this because the unmodelled DAC parked the event pump once a voice
    # played; Leg 22's audio-codec model — ``make_zc3201_audio_codec_stub``, the
    # ``0x04036004`` bit-27 command-complete handshake — clears that spin, so the
    # chime plays AND later OID taps still dispatch. Verified: chime + product-tap
    # mount + content-tap GME play all fire.)
    udisk = getattr(firmware, "udisk_files", None) or {}
    a_files = {**udisk, **(a_files or {})}

    # A: and B: FAT volumes share the ONE even-block whole-disk map (the FatLib
    # layer mounts both through it): A: = whole-disk logical [0, a_blocks), B: =
    # [a_blocks, a_blocks+b_blocks). Clamp both so the pair fits inside the mapped,
    # non-free even blocks (lo = 951 valid; the top ZC_SPARE_BLOCKS stay free spares
    # for the MtdLib map-table ring). B: sits above A:, so it is what gets squeezed.
    lo0 = _partition_geom(0)[1]  # 951 valid even-block slots
    a_blocks = min(a_blocks, lo0 - 1)
    b_blocks = min(b_blocks, lo0 - a_blocks)
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
    _lay_map_tags(img, partition=0)
    _lay_map_tags(img, partition=1)

    # FAT volumes on the even-block whole-disk map: A: at logical 0, B: stacked
    # immediately above it at logical a_blocks (its FAT partition base). Both
    # resolve through partition 0's even-block formula (phys = 128 + 2·logical).
    _place_volume(img, bytes(vol_a.data), logical_offset=0)
    _place_volume(img, bytes(vol_b.data), logical_offset=a_blocks)

    # nandboot system-bin index: place ``codepage`` so app_init's boot-file loader
    # finds it (else a fatal spin at 0x08000944) and reaches the statechart (Leg 16).
    codepage = getattr(firmware, "codepage", None)
    if codepage is not None:
        _lay_system_bin_index(img, [("codepage", bytes(codepage.data))])

    return img
