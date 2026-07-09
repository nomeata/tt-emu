"""Parser for the ``update3202MT.upd`` firmware update container.

Implements ``memory-map-and-boot.md`` §3: the container is an ``ANYKA106``
archive with a 0xA4-byte header describing the artifacts. The fields we need
(byte-verified against the shipping file):

===========  =====================================================
header off   field
===========  =====================================================
``+0x00``    magic ``"ANYKA106"``
``+0x0C``    header size (0xA4)
``+0x18``    producer offset, ``+0x1C`` producer size
``+0x20``    nandboot offset, ``+0x24`` nandboot size
``+0x30``    to_udisk file count, ``+0x34`` to_udisk TOC offset
``+0x48``    PROG offset, ``+0x4C`` PROG size
===========  =====================================================

The **codepage** bin follows PROG directly in the container (offset
``PROG offset + PROG size`` = 0x3A8000). Its size (0xD6CCC) is *not* carried in
the header — the doc's table value is used as a constant (doc gap: §3 says all
offsets/sizes come from the header, but the codepage size demonstrably is not
among the header fields).

Load addresses (§5.1): PROG → 0x08009000 flat; nandboot → 0x08000000 *and*
the HAL alias 0x07FF8000. The codepage is not loaded to RAM (it is read from
the NAND model at runtime, §5.7).
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path

#: Container magic (``memory-map-and-boot.md`` §3).
UPD_MAGIC = b"ANYKA106"
#: Expected header size in bytes.
UPD_HEADER_SIZE = 0xA4
#: Codepage size; not present in the container header (see module docstring).
CODEPAGE_SIZE = 0xD6CCC

#: PROG load address (``memory-map-and-boot.md`` §2/§5.1).
PROG_LOAD_ADDR = 0x08009000
#: Resident boot-blob load address (also the vector table base).
NANDBOOT_LOAD_ADDR = 0x08000000
#: The HAL alias: the same nandboot bytes mapped a second time (§2 note).
NANDBOOT_ALIAS_ADDR = 0x07FF8000
#: PROG entry point (§5.2).
PROG_ENTRY = 0x08039100


@dataclass(frozen=True)
class Artifact:
    """One artifact extracted from the update container."""

    name: str
    offset: int
    data: bytes

    @property
    def size(self) -> int:
        return len(self.data)


@dataclass(frozen=True)
class Firmware:
    """The artifact set the emulator boots (``memory-map-and-boot.md`` §3)."""

    path: Path
    nandboot: Artifact
    prog: Artifact
    codepage: Artifact
    producer: Artifact
    #: A:'s factory content — the container's ``to_udisk`` payload keyed by
    #: partition-relative path (``VOIMG/Chomp_Voice.bin``, ``Language/*.wav``);
    #: see :func:`extract_udisk_files`.
    udisk_files: dict[str, bytes] = field(default_factory=dict)

    @property
    def build_id(self) -> str:
        """PROG build identifier (e.g. ``N0038MT``), from the image's first bytes."""
        return self.prog.data[:16].split(b"\x00", 1)[0].decode("ascii", "replace")

    @property
    def boot_generation(self) -> str:
        """The nandboot generation magic at blob offset +0x20 (e.g. ``ANYKANB1``)."""
        return self.nandboot.data[0x20:0x28].decode("ascii", "replace")


class UpdFormatError(ValueError):
    """The file is not a well-formed ANYKA106 update container."""


def _u32(buf: bytes, off: int) -> int:
    return struct.unpack_from("<I", buf, off)[0]


def _cstr(buf: bytes) -> str:
    return buf.split(b"\x00", 1)[0].decode("latin-1")


# --- to_udisk payload (A:'s factory content) --------------------------------------

#: ``to_udisk`` directory-mapping records: count @+0x28, table @+0x2c, 0x210 B
#: each = ``{u32; char pcpath[260]; char udiskpath[260]; u32 checksum}``.
_UDISK_DIRS_COUNT, _UDISK_DIRS_ADR = 0x28, 0x2C
_UDISK_DIR_STRIDE = 0x210
#: ``to_udisk`` file records: count @+0x30, table @+0x34, 0x110 B each =
#: ``{char path[260]; u32 size; u32 adr; u32 checksum}``. The payloads sit in
#: the container tail at each record's ``adr``.
_UDISK_FILES_COUNT, _UDISK_FILES_ADR = 0x30, 0x34
_UDISK_FILE_STRIDE = 0x110
_UDISK_PATH_LEN = 260


def extract_udisk_files(raw: bytes) -> dict[str, bytes]:
    """Extract the container's ``to_udisk`` payload as ``{relpath: bytes}``.

    These are the factory files the pen's own updater writes to A: after
    reformatting it: the system-voice archive (``voice\\Chomp_Voice.bin`` →
    ``VOIMG/Chomp_Voice.bin``) and the language prompt WAVs (``Language\\*.wav``).
    Each file's first path component is remapped through the directory records
    (``voice`` → ``A:VOIMG``, ``Language`` → ``A:Language``); the drive prefix is
    dropped and separators normalised so the result drops straight onto A:.
    """
    dir_map: dict[str, str] = {}
    dcount, dadr = _u32(raw, _UDISK_DIRS_COUNT), _u32(raw, _UDISK_DIRS_ADR)
    for i in range(dcount):
        o = dadr + i * _UDISK_DIR_STRIDE
        pcpath = _cstr(raw[o + 4 : o + 4 + _UDISK_PATH_LEN])
        udiskpath = _cstr(raw[o + 4 + _UDISK_PATH_LEN : o + 4 + 2 * _UDISK_PATH_LEN])
        dir_map[pcpath.lower()] = udiskpath

    def to_rel(path: str) -> str:
        parts = path.replace("\\", "/").split("/")
        head, rest = parts[0], "/".join(parts[1:])
        mapped = dir_map.get(head.lower(), head).replace("\\", "/")
        if len(mapped) >= 2 and mapped[1] == ":":  # strip a drive prefix (A:)
            mapped = mapped[2:]
        mapped = mapped.strip("/")
        return f"{mapped}/{rest}" if rest else mapped

    out: dict[str, bytes] = {}
    fcount, fadr = _u32(raw, _UDISK_FILES_COUNT), _u32(raw, _UDISK_FILES_ADR)
    for i in range(fcount):
        o = fadr + i * _UDISK_FILE_STRIDE
        path = _cstr(raw[o : o + _UDISK_PATH_LEN])
        size, adr = struct.unpack_from("<II", raw, o + _UDISK_PATH_LEN)
        if not path or adr + size > len(raw):
            raise UpdFormatError(
                f"to_udisk file {path!r} [{adr:#x}+{size:#x}] exceeds the container"
            )
        out[to_rel(path)] = raw[adr : adr + size]
    return out


def load_upd(path: str | Path | None = None) -> Firmware:
    """Parse an ``update3202MT.upd``-style container and extract the boot artifacts.

    With ``path=None`` the official firmware is resolved via
    :func:`tt_emu.firmware_fetch.ensure_firmware` (cached download, SHA-256
    verified against the pinned hash).

    Raises :class:`UpdFormatError` on structural problems; sanity-checks the
    artifacts against the documented invariants (nandboot generation magic at
    +0x20, artifact bounds).
    """
    if path is None:
        from .firmware_fetch import ensure_firmware

        path = ensure_firmware(None)
    path = Path(path)
    raw = path.read_bytes()
    if len(raw) < UPD_HEADER_SIZE:
        raise UpdFormatError(f"{path}: too small for an ANYKA106 header")
    if raw[:8] != UPD_MAGIC:
        raise UpdFormatError(f"{path}: bad magic {raw[:8]!r} (want {UPD_MAGIC!r})")
    header_size = _u32(raw, 0x0C)
    if header_size != UPD_HEADER_SIZE:
        raise UpdFormatError(f"{path}: unexpected header size {header_size:#x}")

    def artifact(name: str, offset: int, size: int) -> Artifact:
        if offset + size > len(raw):
            raise UpdFormatError(
                f"{path}: artifact {name} [{offset:#x}+{size:#x}] exceeds file size"
            )
        return Artifact(name, offset, raw[offset : offset + size])

    producer = artifact("producer", _u32(raw, 0x18), _u32(raw, 0x1C))
    nandboot = artifact("nandboot", _u32(raw, 0x20), _u32(raw, 0x24))
    prog = artifact("PROG", _u32(raw, 0x48), _u32(raw, 0x4C))
    # Codepage: directly after PROG; size is a documented constant (module docstring).
    codepage = artifact("codepage", prog.offset + prog.size, CODEPAGE_SIZE)

    fw = Firmware(
        path=path,
        nandboot=nandboot,
        prog=prog,
        codepage=codepage,
        producer=producer,
        udisk_files=extract_udisk_files(raw),
    )
    generation = fw.boot_generation
    if not generation.startswith("ANYKANB"):
        raise UpdFormatError(
            f"{path}: nandboot generation magic {generation!r} at +0x20 looks wrong"
        )
    return fw
