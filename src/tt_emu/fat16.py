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
memory, cross-platform. Short 8.3 names only (no LFN); directory trees are
supported so host directories can be mirrored in (§5.1/§5.2 content).
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


def _short_name(name: str) -> bytes:
    """Encode a host name as an 8.3 directory-entry name (11 bytes, no LFN)."""
    name = name.upper()
    if name in (".", ".."):
        return name.ljust(11).encode("ascii")
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    keep = "".join(c if c.isalnum() or c in "_-~!#$%&@" else "_" for c in stem)[:8]
    kext = "".join(c if c.isalnum() or c in "_-~!#$%&@" else "_" for c in ext)[:3]
    if not keep:
        raise Fat16BuildError(f"cannot express {name!r} as an 8.3 name")
    return (keep.ljust(8) + kext.ljust(3)).encode("ascii")


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
) -> Fat16Volume:
    """Build a FAT16 superfloppy volume of ``total_bytes`` (multiple of 512).

    Defaults mirror the recipe's mkfs.fat invocation
    (``nand-image-layout.md`` §6 step 4: ``-F 16 -s 4 -R 4 -r 512 -f 2``).
    ``files`` maps slash-separated relative paths to file contents;
    intermediate directories are created.
    """
    if total_bytes % SECTOR_SIZE:
        raise Fat16BuildError("volume size must be a multiple of 512")
    total_sectors = total_bytes // SECTOR_SIZE
    spc = sectors_per_cluster
    root_sectors = (root_entries * DIR_ENTRY_SIZE + SECTOR_SIZE - 1) // SECTOR_SIZE
    fat_sectors = _fat_sectors_for(total_sectors, spc, reserved_sectors, root_entries, num_fats)
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
        """Allocate a cluster chain for ``nbytes`` (>=1 cluster); return its head."""
        nonlocal next_free
        count = max(1, (nbytes + cluster_bytes - 1) // cluster_bytes)
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
        if is_root:
            entries.append(_dir_entry(
                label.upper().encode("ascii", "replace")[:11].ljust(11),
                _ATTR_VOLUME_ID, 0, 0))
        else:
            entries.append(_dir_entry(_short_name("."), _ATTR_DIRECTORY, 0, 0))
            entries.append(_dir_entry(_short_name(".."), _ATTR_DIRECTORY, 0, 0))
        for name, child in node.items():
            name11 = _short_name(name)
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
