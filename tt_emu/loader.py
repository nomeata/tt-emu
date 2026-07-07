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
from dataclasses import dataclass
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

    fw = Firmware(path=path, nandboot=nandboot, prog=prog, codepage=codepage, producer=producer)
    generation = fw.boot_generation
    if not generation.startswith("ANYKANB"):
        raise UpdFormatError(
            f"{path}: nandboot generation magic {generation!r} at +0x20 looks wrong"
        )
    return fw
