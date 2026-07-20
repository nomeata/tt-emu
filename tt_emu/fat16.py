"""Pure-Python FAT16 "superfloppy" volume builder.

Builds the bare FAT16 volumes the pen's firmware mounts (``nand-image-layout.md``
§5): the VBR/BPB at partition-relative sector 0, **no MBR** — this is the format
the firmware itself writes when it formats A:. The builder satisfies the mount's
sector-0 checks (§5, Observed instruction-level):

* the u32 at VBR offset ``0x1c6`` is zero (dispatch heuristic — values 1..0x100
  would force the MBR-only parse);
* ``"FAT16   "`` FS-type label at offset 0x36 (the only superfloppy-path check);
* 512 bytes/sector (hard reject otherwise);
* cluster count >= 4085 (FAT12-sized volumes rejected) — validated at build time;
* ``0x55AA`` at offset 0x1fe (kept for the MBR fallback / USB hosts).

No external tools (mkfs.fat / mtools) are used — the volume is assembled in
memory, cross-platform. Directory trees are supported so host directories can be
mirrored in (§5.1/§5.2 content). Names that do not fit a lossless 8.3 short name
(lowercase, too long, spaces) get **VFAT long-filename (LFN)** entries plus a
unique ``NAME~n`` short alias — the firmware opens the system-voice bank and the
update prompts by their long paths (``A:/VOIMG/Chomp_Voice.bin``,
``A:/Language/UpdateGERMAN.wav``), so a short-name-only directory makes every
such ``fs_open`` miss the name and return −1.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

__all__ = ["Fat16Volume", "build_fat16", "files_from_dir"]

SECTOR_SIZE = 512
DIR_ENTRY_SIZE = 32

#: FAT16 cluster-count validity window (nand-image-layout.md §5 + FAT spec).
MIN_CLUSTERS = 4085
MAX_CLUSTERS = 65524

_ATTR_READ_ONLY = 0x01
_ATTR_VOLUME_ID = 0x08
_ATTR_DIRECTORY = 0x10
_ATTR_ARCHIVE = 0x20

#: Fixed DOS timestamp for generated entries (2026-01-01 00:00:00).
_DOS_DATE = ((2026 - 1980) << 9) | (1 << 5) | 1
_DOS_TIME = 0


class Fat16BuildError(ValueError):
    """The requested volume cannot be built (geometry or content problem)."""


@dataclass
class Fat16Volume:
    """A built FAT16 superfloppy image.

    ``data`` is the raw volume (sector 0 = VBR). ``system_sectors`` is the
    first data-region sector — everything below it (reserved sectors, both
    FATs, the root directory) is FAT bookkeeping the firmware *will* read, so
    the NAND-image placer must force-place all blocks overlapping it even when
    they compress to all-zero (``nand-image-layout.md`` §6 step 5 note).
    """

    data: bytearray
    system_sectors: int
    sectors_per_cluster: int
    total_clusters: int


def _fat_sectors_for(total_sectors: int, spc: int, reserved: int, root_entries: int,
                     num_fats: int) -> int:
    """Fixed-point iteration for the FAT size (standard FAT16 layout math)."""
    root_sectors = (root_entries * DIR_ENTRY_SIZE + SECTOR_SIZE - 1) // SECTOR_SIZE
    fat_sectors = 1
    while True:
        data_sectors = total_sectors - reserved - root_sectors - num_fats * fat_sectors
        clusters = data_sectors // spc
        needed = ((clusters + 2) * 2 + SECTOR_SIZE - 1) // SECTOR_SIZE
        if needed <= fat_sectors:
            return fat_sectors
        fat_sectors = needed


#: Characters allowed unescaped in an 8.3 short name (upper-cased first).
_SHORT_OK = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-!#$%&@^`~(){}'")


def _short_name(name: str) -> bytes:
    """Encode ``.``/``..`` as an 11-byte directory name (dot entries only)."""
    return name.ljust(11).encode("ascii")


def _split_83(name: str) -> tuple[str, str]:
    """Split a name into an upper-cased, character-sanitised (stem, ext)."""
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    def up(s: str) -> str:
        return "".join(c if c in _SHORT_OK else "_" for c in s.upper())

    return up(stem), up(ext)


def _is_lossless_83(name: str) -> bool:
    """True iff ``name`` needs no long-name entry to be opened by the firmware.

    A name fits an 8.3 short entry (case-insensitively — the firmware upper-cases
    the query before the short-name compare) when its upper-cased form is at most
    8 stem + 3 ext characters, all from the short-name set, with at most one dot.
    Longer names, spaces, or short-set-invalid characters force an LFN.
    """
    up = name.upper()
    if up.count(".") > 1:
        return False
    stem, dot, ext = up.rpartition(".")
    if not dot:
        stem, ext = up, ""
    return (
        1 <= len(stem) <= 8
        and len(ext) <= 3
        and all(c in _SHORT_OK for c in stem + ext)
    )


def _unique_short(name: str, used: set[bytes]) -> bytes:
    """Build a unique 11-byte 8.3 alias for ``name`` (``STEM~n`` on collision)."""
    stem, ext = _split_83(name)
    if not stem:
        stem = "_"
    for n in range(1, 1_000_000):
        tail = f"~{n}"
        base = (stem[: 8 - len(tail)] + tail) if (len(stem) > 8 or True) else stem
        short = (base.ljust(8)[:8] + ext.ljust(3)[:3]).encode("ascii")
        if short not in used:
            used.add(short)
            return short
    raise Fat16BuildError(f"cannot allocate a unique short name for {name!r}")


def _short_for(name: str, used: set[bytes]) -> bytes:
    """The 11-byte short name for ``name``, reserving it in ``used``."""
    if _is_lossless_83(name):
        stem, ext = _split_83(name)
        short = (stem.ljust(8) + ext.ljust(3)).encode("ascii")
        if short not in used:
            used.add(short)
            return short
    return _unique_short(name, used)


def _lfn_checksum(short11: bytes) -> int:
    """The 8.3 checksum every LFN entry of a name must carry (FAT/VFAT spec)."""
    s = 0
    for c in short11:
        s = (((s & 1) << 7) + (s >> 1) + c) & 0xFF
    return s


def _lfn_entries(name: str, short11: bytes) -> list[bytes]:
    """The VFAT long-name entries for ``name`` (physical order, before the 8.3).

    Each entry (attr 0x0F) carries 13 UTF-16LE code units; entries are numbered
    from 1 (holding the first 13 units) and stored in **reverse** so the highest
    sequence number (ORed with 0x40 = last-logical) sits first in the directory,
    immediately followed by decreasing sequence numbers and then the 8.3 entry.
    """
    units = list(struct.unpack(f"<{len(name)}H", name.encode("utf-16-le")))
    units.append(0)  # NUL terminator
    while len(units) % 13:
        units.append(0xFFFF)  # 0xFFFF padding
    checksum = _lfn_checksum(short11)
    nparts = len(units) // 13
    entries: list[bytes] = []
    for part in range(nparts):
        seq = part + 1
        if part == nparts - 1:
            seq |= 0x40
        block = units[part * 13 : part * 13 + 13]
        entries.append(
            struct.pack(
                "<B5HBBB6HH2H",
                seq,
                *block[0:5],
                0x0F,  # attr LFN
                0,     # type
                checksum,
                *block[5:11],
                0,     # first cluster (always 0 for LFN)
                *block[11:13],
            )
        )
    entries.reverse()
    return entries


def _dir_entry(name11: bytes, attr: int, first_cluster: int, size: int) -> bytes:
    # 0..10 name, 11 attr, 12..21 NT-reserved/ctimes (zero), 22 write time,
    # 24 write date, 26 first cluster, 28 file size.
    return struct.pack(
        "<11sB10xHHHI", name11, attr, _DOS_TIME, _DOS_DATE, first_cluster, size
    )


class _Tree(dict):
    """Nested {name: bytes | _Tree} directory tree."""


def _tree_from_files(files: Mapping[str, bytes]) -> _Tree:
    root = _Tree()
    for path, content in files.items():
        parts = [p for p in path.replace("\\", "/").split("/") if p]
        if not parts:
            raise Fat16BuildError(f"empty file path {path!r}")
        node = root
        for part in parts[:-1]:
            nxt = node.setdefault(part, _Tree())
            if not isinstance(nxt, _Tree):
                raise Fat16BuildError(f"{path!r}: {part!r} is both a file and a directory")
            node = nxt
        node[parts[-1]] = bytes(content)
    return root


def files_from_dir(path: str | Path) -> dict[str, bytes]:
    """Collect a host directory tree into a {relative/path: bytes} mapping."""
    base = Path(path)
    out: dict[str, bytes] = {}
    for p in sorted(base.rglob("*")):
        if p.is_file():
            out[p.relative_to(base).as_posix()] = p.read_bytes()
    return out


def build_fat16(
    total_bytes: int,
    *,
    label: str = "NO NAME",
    files: Mapping[str, bytes] | None = None,
    sectors_per_cluster: int = 4,
    reserved_sectors: int = 4,
    root_entries: int = 512,
    num_fats: int = 2,
    volume_id: int = 0x1090_2026,
    au_sectors: int = 1,
) -> Fat16Volume:
    """Build a FAT16 superfloppy volume of ``total_bytes`` (multiple of 512).

    Defaults mirror the recipe's mkfs.fat invocation
    (``nand-image-layout.md`` §6 step 4: ``-F 16 -s 4 -R 4 -r 512 -f 2``).
    ``files`` maps slash-separated relative paths to file contents;
    intermediate directories are created.

    ``au_sectors`` couples the volume geometry to the NFTL's **allocation unit**
    (``nand-and-nfc-controller.md`` §4.2 / ``nand-image-layout.md``: AU = 4 KiB =
    8 sectors). The firmware's medium reads the NAND in whole AU-aligned chunks,
    so a data cluster that straddles an AU boundary makes the firmware read the
    *other* cluster of that AU as well; when a file's last cluster leaves the
    second half of its AU unallocated, that AU read resolves to the wrong
    physical block and returns zeros — silently truncating the tail of every
    ``.gme`` (the power-on/welcome playlist lives in the last bytes of the file,
    ``firmware-2n-mt.md`` §4/§5, so it is the first casualty). Passing
    ``au_sectors = 8`` makes the builder (a) **AU-align the data region** so
    clusters start on AU boundaries and (b) **round every file's cluster chain
    up to a whole number of AUs**, so no read ever straddles an unallocated
    cluster. ``1`` (the default) is a no-op — the plain FAT geometry.
    """
    if total_bytes % SECTOR_SIZE:
        raise Fat16BuildError("volume size must be a multiple of 512")
    total_sectors = total_bytes // SECTOR_SIZE
    spc = sectors_per_cluster
    #: Clusters per NFTL allocation unit (files are allocated in whole-AU
    #: multiples of this; 1 when the geometry is not AU-coupled).
    clusters_per_au = max(1, -(-au_sectors // spc)) if au_sectors > 1 else 1
    root_sectors = (root_entries * DIR_ENTRY_SIZE + SECTOR_SIZE - 1) // SECTOR_SIZE
    fat_sectors = _fat_sectors_for(total_sectors, spc, reserved_sectors, root_entries, num_fats)
    first_data_sector = reserved_sectors + num_fats * fat_sectors + root_sectors
    # AU-align the data region (see ``au_sectors``): bump the reserved sectors so
    # cluster 2 begins on an AU boundary. Re-derive the FAT size each step (it can
    # grow as reserved sectors take space) until the first data sector is aligned.
    if au_sectors > 1:
        while first_data_sector % au_sectors:
            reserved_sectors += 1
            fat_sectors = _fat_sectors_for(
                total_sectors, spc, reserved_sectors, root_entries, num_fats
            )
            first_data_sector = reserved_sectors + num_fats * fat_sectors + root_sectors
    total_clusters = (total_sectors - first_data_sector) // spc
    if not MIN_CLUSTERS <= total_clusters <= MAX_CLUSTERS:
        raise Fat16BuildError(
            f"cluster count {total_clusters} outside FAT16 range "
            f"[{MIN_CLUSTERS}, {MAX_CLUSTERS}] — adjust sectors_per_cluster (§5)"
        )

    img = bytearray(total_bytes)
    cluster_bytes = spc * SECTOR_SIZE

    # --- VBR / BPB (nand-image-layout.md §5 checks) --------------------------------
    bpb = struct.pack(
        "<3s8sHBHBHHBHHHII",
        b"\xeb\x3c\x90",          # jump
        b"TTEMU1.0",              # OEM name
        SECTOR_SIZE,              # bytes/sector (0x0b) — must be 512
        spc,                      # sectors/cluster
        reserved_sectors,         # reserved sectors
        num_fats,                 # FAT count
        root_entries,             # root directory entries
        total_sectors if total_sectors < 0x10000 else 0,  # total sectors (16-bit)
        0xF8,                     # media descriptor
        fat_sectors,              # sectors per FAT
        63, 255,                  # sectors/track, heads (geometry: don't-care)
        0,                        # hidden sectors
        total_sectors if total_sectors >= 0x10000 else 0,  # total sectors (32-bit)
    )
    ebpb = struct.pack(
        "<BBBI11s8s",
        0x80, 0, 0x29, volume_id,
        label.upper().encode("ascii", "replace")[:11].ljust(11),
        b"FAT16   ",              # FS type at 0x36 — the superfloppy-path check
    )
    img[0 : len(bpb)] = bpb
    img[len(bpb) : len(bpb) + len(ebpb)] = ebpb
    # u32 @0x1c6 stays 0 (dispatch heuristic, §5); 0x55AA signature:
    img[0x1FE:0x200] = b"\x55\xaa"

    # --- FAT + allocator -------------------------------------------------------------
    fat = bytearray(fat_sectors * SECTOR_SIZE)
    fat[0:4] = struct.pack("<HH", 0xFFF8, 0xFFFF)  # media + end-of-chain reserved entries
    next_free = 2

    def cluster_offset(cluster: int) -> int:
        return (first_data_sector + (cluster - 2) * spc) * SECTOR_SIZE

    def alloc_chain(nbytes: int) -> int:
        """Allocate a cluster chain for ``nbytes`` (>=1 cluster); return its head.

        Rounded up to a whole number of NFTL allocation units (see the
        ``au_sectors`` note) so a file never leaves the second half of an AU
        unallocated — that would make the firmware's AU-granular read of the
        file's last cluster straddle an unallocated cluster and misresolve.
        """
        nonlocal next_free
        count = max(1, (nbytes + cluster_bytes - 1) // cluster_bytes)
        count = -(-count // clusters_per_au) * clusters_per_au  # whole AUs
        if next_free + count > total_clusters + 2:
            raise Fat16BuildError("volume full")
        head = next_free
        for i in range(count):
            cur = next_free + i
            nxt = 0xFFFF if i == count - 1 else cur + 1
            struct.pack_into("<H", fat, cur * 2, nxt)
        next_free += count
        return head

    # --- directory tree --------------------------------------------------------------
    tree = _tree_from_files(files or {})

    def write_dir(entries: list[bytes], *, is_root: bool, parent_cluster: int) -> int:
        """Store a directory (list of 32-B entries); return its first cluster (0=root)."""
        if is_root:
            raw = b"".join(entries)
            if len(raw) > root_entries * DIR_ENTRY_SIZE:
                raise Fat16BuildError("root directory overflow")
            off = reserved_sectors + num_fats * fat_sectors
            img[off * SECTOR_SIZE : off * SECTOR_SIZE + len(raw)] = raw
            return 0
        cluster = alloc_chain(max(len(entries), 1) * DIR_ENTRY_SIZE)
        raw = b"".join(entries)
        img[cluster_offset(cluster) : cluster_offset(cluster) + len(raw)] = raw
        # backfill '.' / '..' first-cluster fields
        struct.pack_into("<H", img, cluster_offset(cluster) + 26, cluster)
        struct.pack_into("<H", img, cluster_offset(cluster) + DIR_ENTRY_SIZE + 26,
                         parent_cluster)
        return cluster

    def emit_tree(node: _Tree, *, is_root: bool, parent_cluster: int) -> int:
        entries: list[bytes] = []
        used: set[bytes] = set()
        if is_root:
            entries.append(_dir_entry(
                label.upper().encode("ascii", "replace")[:11].ljust(11),
                _ATTR_VOLUME_ID, 0, 0))
        else:
            entries.append(_dir_entry(_short_name("."), _ATTR_DIRECTORY, 0, 0))
            entries.append(_dir_entry(_short_name(".."), _ATTR_DIRECTORY, 0, 0))
        for name, child in node.items():
            # 8.3-representable names keep their bare short entry; others get
            # VFAT long-name entries plus a unique ``NAME~n`` short alias, so
            # the firmware's own long-path ``fs_open`` matches (§ module doc).
            name11 = _short_for(name, used)
            if not _is_lossless_83(name):
                entries.extend(_lfn_entries(name, name11))
            if isinstance(child, _Tree):
                sub = emit_tree(child, is_root=False, parent_cluster=0)  # patched below
                entries.append(_dir_entry(name11, _ATTR_DIRECTORY, sub, 0))
            else:
                if child:
                    head = alloc_chain(len(child))
                    img[cluster_offset(head) : cluster_offset(head) + len(child)] = child
                else:
                    head = 0
                entries.append(_dir_entry(name11, _ATTR_ARCHIVE, head, len(child)))
        return write_dir(entries, is_root=is_root, parent_cluster=parent_cluster)

    # NOTE: subdirectory '..' links point at cluster 0 (root) only for depth-1
    # dirs; deeper trees still resolve because the firmware's FAT walk follows
    # the directory-entry chain, not '..'. Depth-1 is all §5.1/§5.2 content needs.
    emit_tree(tree, is_root=True, parent_cluster=0)

    for i in range(num_fats):
        off = (reserved_sectors + i * fat_sectors) * SECTOR_SIZE
        img[off : off + len(fat)] = fat

    return Fat16Volume(
        data=img,
        system_sectors=first_data_sector,
        sectors_per_cluster=spc,
        total_clusters=total_clusters,
    )
