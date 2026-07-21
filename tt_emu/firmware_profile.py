"""Firmware-target profiles: what differs between the pen generations.

tt-emu was built around the 2nd-generation "MT" pen (ZC3202N / AK1050) and its
``update3202MT.upd`` firmware. Extending it to other pens (the 1st-generation
ZC3201) means a handful of build-specific constants — the PROG/nandboot **load
addresses**, the PROG **entry point**, the pinned **download URL + SHA-256**,
and the **boot generation** — stop being global and become *selectable*. This
module collects them into a :class:`FirmwareProfile` keyed off the parsed
container, so the loader / fetcher / boot recipe can ask "which pen is this?"
instead of hard-coding the MT values.

The profile is intentionally **data only** (no behaviour): the machine build
still owns the hardware models. It exists so a second firmware can be *loaded
and fetched* through the same paths, and so the growing body of ZC3201 findings
(the GME-interpreter twins, the HAL entry points) has one authoritative home.

Detection is by the nandboot **generation magic** at blob ``+0x20``
(``ANYKANB1`` = 2N-MT, ``ANYKANB0`` = 1st-gen ZC3201), cross-checked against the
PROG image's build fingerprint at offset 0 — byte-exact, so a wrong image is
never silently accepted.
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = [
    "FetchInfo",
    "FirmwareProfile",
    "MT",
    "PROFILES",
    "ZC3201",
    "by_key",
    "detect",
]


@dataclass(frozen=True)
class FetchInfo:
    """Where a firmware container is downloaded from, and how it is verified.

    The URL is convenience; the SHA-256 is the authority (see
    :mod:`tt_emu.firmware_fetch`). ``urls`` is tried in order.
    """

    urls: tuple[str, ...]
    sha256: str
    filename: str
    size: int | None = None  #: expected byte count (diagnostics only)


@dataclass(frozen=True)
class FirmwareProfile:
    """The build-specific constants of one supported pen firmware."""

    key: str  #: short stable id ("mt", "zc3201")
    label: str  #: human description
    boot_generation: str  #: nandboot magic at blob +0x20
    build_prefix: bytes  #: byte-exact fingerprint at PROG offset 0

    #: Load layout (``memory-map-and-boot.md`` §5.1 for MT).
    prog_load: int
    prog_entry: int
    nandboot_load: int
    #: A second mapping of the nandboot bytes (the MT "HAL alias"); ``None`` when
    #: the generation loads nandboot only once (ZC3201: PROG occupies the MT
    #: alias slot, so nandboot lives only at ``nandboot_load``).
    nandboot_alias: int | None
    codepage_size: int

    fetch: FetchInfo

    #: Whether tt-emu can boot this firmware to book mode authentically today.
    #: MT: yes (the whole §5 recipe). ZC3201: not yet — the from-entry boot RE
    #: (seed state, SoC MMIO map, NAND geometry, OID/audio register addresses)
    #: does not exist yet; the ``firmware-re`` lab reaches its GME interpreter
    #: only by direct-calling functions with hooked storage, which tt-emu's
    #: hook-free model rules out. See ``docs/zc3201-boot-feasibility.md``.
    boots_to_book: bool = False

    #: Reference addresses recovered for this build (firmware-absolute). Empty
    #: for MT (its addresses live in :mod:`tt_emu.firmware.mt`); populated for
    #: ZC3201 from the ``firmware-re`` lab + ``tt-firmware-reveng`` so the future
    #: bring-up has them in one place. Not wired into any hardware model.
    symbols: dict[str, int] = field(default_factory=dict)

    def recognize(self, prog: bytes) -> bool:
        """True iff ``prog`` carries this profile's build fingerprint."""
        return prog[: len(self.build_prefix)] == self.build_prefix


# --- 2nd-generation "MT" (ZC3202N / AK1050) -----------------------------------------

MT = FirmwareProfile(
    key="mt",
    label="N0038MT / 20131009 (2N 'MT', ZC3202N)",
    boot_generation="ANYKANB1",
    build_prefix=b"N0038MT\x00\x00\x00" + b"20131009" + b"\x00\x00" + b"Tiptoi",
    prog_load=0x0800_9000,
    prog_entry=0x0803_9100,
    nandboot_load=0x0800_0000,
    nandboot_alias=0x07FF_8000,
    codepage_size=0xD6CCC,
    fetch=FetchInfo(
        urls=("https://cdn.ravensburger.de/db/Firmware-Files/de/38/REV3/update3202MT.upd",),
        sha256="8e37af0a3d3c126189447964784fd84ccf0356cb7425b5ab478e86b3352741f9",
        filename="update3202MT.upd",
        size=11_303_276,
    ),
    boots_to_book=True,
)


# --- 1st-generation ZC3201 ----------------------------------------------------------
#
# Load layout from the ``firmware-re`` lab (nandboot @ 0x07ff8000 reset entry
# 0x07ff8094; PROG @ 0x08000000 identity, 1 MiB). ``prog_entry`` is the
# statechart's init leaf ``state_init_power_on``; running from it executes 132
# instructions and returns (the QP init returns to let the event pump drive) —
# NOT the pre-init boot entry MT uses (which for ZC3201 is not yet identified).
# See docs/zc3201-boot-feasibility.md.

ZC3201 = FirmwareProfile(
    key="zc3201",
    label="v0136 / 120117 (1st-gen ZC3201)",
    boot_generation="ANYKANB0",
    build_prefix=b"v0136\x00\x00\x00\x00\x00" + b"120117" + b"\x00\x00\x00\x00" + b"Tiptoi",
    prog_load=0x0800_0000,
    prog_entry=0x0803_0E48,  # state_init_power_on
    nandboot_load=0x07FF_8000,
    nandboot_alias=None,  # PROG occupies 0x08000000; nandboot loads once
    codepage_size=0xD6CCC,
    fetch=FetchInfo(
        urls=("https://cdn.ravensburger.de/db/firmware/update%20encrypt%20normal%20freq.upd",),
        sha256="03c12f41b6bc9ab78ee206c2bdfdbe45eb9ad7c3ab8337e7a8661555aae4a4a6",
        filename="update_zc3201.upd",
        size=6_396_452,
    ),
    boots_to_book=False,
    symbols={
        # HAL / FSLib (names.csv, lab hook points)
        "fs_open": 0x0804_00EC,
        "fs_read": 0x0804_01C4,
        "fs_seek": 0x0804_01D4,
        "voice_play_sample": 0x0809_7068,
        "voice_load_and_play": 0x0809_716C,
        "play_chomp_voice": 0x0809_7374,
        "game_play_oid_voice": 0x0805_4730,
        "akoid_open_check": 0x0802_19A0,
        "state_init_power_on": 0x0803_0E48,
        "state_stdb_standby": 0x0803_6454,
        "event_post": 0x0800_1544,
        "nand_disk_mount": 0x0802_B45C,
        # Generic scripted-GME interpreter (twins of the 2N engine)
        "gme_parse_header": 0x0804_572C,
        "gme_parse_oidrange": 0x0804_5648,
        "gme_reset_regs": 0x0804_56B4,
        "gme_clear": 0x0804_5420,
        "gme_oid_to_playscript": 0x0804_5358,
        "gme_parse_check_conditions": 0x0804_523C,
        "gme_parse_actions": 0x0804_4EC8,
        "gme_parse_playlist": 0x0804_4DE8,
        "gme_parse_media_offsets": 0x0804_4D38,
        "gme_exec_command": 0x0804_46E4,
        "gme_oid_tap_handler": 0x0804_3358,
        # App-context globals (RAM)
        "gb_app_context": 0x0800_779C,
        "p_pMeGame_slot": 0x081D_8854,
        "gme_file_handle_ptr": 0x080D_20A0,
        "chomp_handle_ptr": 0x080D_28FC,
    },
)


PROFILES: tuple[FirmwareProfile, ...] = (MT, ZC3201)


def by_key(key: str) -> FirmwareProfile:
    """The profile with this ``key`` (raises :class:`KeyError` if unknown)."""
    for profile in PROFILES:
        if profile.key == key:
            return profile
    raise KeyError(f"no firmware profile {key!r} (known: {[p.key for p in PROFILES]})")


def detect(prog: bytes, boot_generation: str) -> FirmwareProfile | None:
    """Identify the firmware from its PROG image + nandboot generation magic.

    Both must agree: the generation magic selects the candidate, the PROG build
    fingerprint confirms it. Returns ``None`` for an unrecognized image (the
    caller then falls back to the MT defaults / generic behaviour).
    """
    for profile in PROFILES:
        if profile.boot_generation == boot_generation and profile.recognize(prog):
            return profile
    # Generation matched but fingerprint did not (or vice versa): unknown build.
    return None
