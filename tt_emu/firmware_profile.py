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

    #: Crt0-equivalent from-entry seed: ``(addr, size)`` of the low working-RAM
    #: ``.bss`` window the C-runtime / boot ROM zeroes before handing control to
    #: the boot task. A from-entry boot skips crt0, so this region keeps the
    #: PROG image's stale bytes; the firmware's HAL IRQ-nesting struct lives here
    #: (ZC3201: depth byte ``0x08007d8c``), and a non-zero depth trips the
    #: nesting-overflow guard (``irq_mask_push`` ``0x07ffdb00`` → hang) on the
    #: first critical section. Zeroing it is the ZC3201 analogue of the MT boot's
    #: §5.6 RAM seeds. ``None`` for firmwares whose entry runs after crt0 (MT).
    #: Verified no PROG code executes in this window (pure scratch/bss).
    bss_seed: tuple[int, int] | None = None

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
# **PROG runtime link base is 0x08008000, not 0x08000000** — the ``firmware-re``
# lab and the ``tt-firmware-reveng`` ghidra project both loaded ZC3201 PROG at
# 0x08000000, which is the *same* wrong-base mistake the MT project made before it
# was corrected to 0x08009000 (see ``docs/zc3201-boot-feasibility.md`` "Leg 3").
# Loading at 0x08000000 makes **code** execute (it is PC-relative) but leaves every
# absolute **data** literal pointing 0x8000 too low: e.g. the clock code reads its
# CPU-frequency table through a baked pointer ``0x08025xxx`` that only resolves to
# the real ``.rodata`` table when PROG sits at 0x08008000; at 0x08000000 that
# pointer lands in ``fs_storage_mount_init``'s code and the clock-set helper spins
# on a garbage frequency. Loading PROG at its true base 0x08008000 (with nandboot
# aliased at 0x08000000 exactly like MT, so the ``bl 0x0800xxxx`` HAL veneers —
# ``= 0x07ffxxxx + 0x8000`` — resolve) makes every absolute reference correct
# (``memory-map-and-boot.md`` §1.3.1).
#
# Because the reveng project's base is 0x8000 low, every PROG address it reports is
# 0x8000 below the runtime address; the symbols below are therefore the reveng
# values **+ 0x8000** (HAL/nandboot ``0x07ffxxxx`` targets are unaffected — the
# lab loaded nandboot at the correct 0x07ff8000). ``prog_entry`` is the pre-init
# boot task ``boot_task_main`` (reveng 0x080238bc → runtime 0x0802b8bc): SoC
# bring-up, then ``app_init_main`` (mounts storage + installs the statechart), then
# the infinite event-pump loop dispatching statechart events. From this entry, with
# the reused SoC core peripherals + the NFC/ECC/L2 storage trio, the unmodified
# firmware boots through app_init_main → subsystem inits → clock setup → MTD/storage
# init and drives the NFC command-list sequencer (same registers as MT: NFC
# 0x0404a000, ECC 0x0405b000). See docs/zc3201-boot-feasibility.md.

#: Runtime-vs-reveng base delta: the reveng project loads at 0x08000000, the true
#: PROG link base is 0x08008000, so a reveng PROG address ``a`` is at runtime
#: ``a + PROG_BASE_FIX``. HAL/nandboot addresses (0x07ffxxxx) are unaffected.
PROG_BASE_FIX = 0x8000


def _z(reveng_addr: int) -> int:
    """A reveng-reported ZC3201 PROG address lifted to its runtime address.

    Adds :data:`PROG_BASE_FIX` for PROG-range addresses (>= 0x08000000); leaves
    HAL/nandboot targets (0x07ffxxxx) untouched.
    """
    return reveng_addr + PROG_BASE_FIX if reveng_addr >= 0x0800_0000 else reveng_addr


ZC3201 = FirmwareProfile(
    key="zc3201",
    label="v0136 / 120117 (1st-gen ZC3201)",
    boot_generation="ANYKANB0",
    build_prefix=b"v0136\x00\x00\x00\x00\x00" + b"120117" + b"\x00\x00\x00\x00" + b"Tiptoi",
    prog_load=0x0800_8000,  # true PROG link base (reveng used 0x08000000 — wrong)
    prog_entry=_z(0x0802_38BC),  # boot_task_main (ROM entry: SoC init + event pump)
    nandboot_load=0x07FF_8000,
    nandboot_alias=0x0800_0000,  # HAL veneers call 0x0800xxxx (= 0x07ffxxxx+0x8000)
    codepage_size=0xD6CCC,
    fetch=FetchInfo(
        urls=("https://cdn.ravensburger.de/db/firmware/update%20encrypt%20normal%20freq.upd",),
        sha256="03c12f41b6bc9ab78ee206c2bdfdbe45eb9ad7c3ab8337e7a8661555aae4a4a6",
        filename="update_zc3201.upd",
        size=6_396_452,
    ),
    # Low working-RAM / HAL globals between the nandboot alias (ends 0x08006fe4)
    # and PROG (0x08008000) — the C-runtime zeroes this before boot_task_main; a
    # from-entry boot reproduces it. (With the correct base PROG no longer clobbers
    # this window, so it is small; the HAL IRQ-nesting depth byte lives here.)
    bss_seed=(0x0800_6FE4, 0x0800_8000 - 0x0800_6FE4),
    boots_to_book=False,
    symbols={
        # HAL / FSLib (names.csv, lab hook points) — reveng PROG addrs + 0x8000
        "fs_open": _z(0x0804_00EC),
        "fs_read": _z(0x0804_01C4),
        "fs_seek": _z(0x0804_01D4),
        "voice_play_sample": _z(0x0809_7068),
        "voice_load_and_play": _z(0x0809_716C),
        "play_chomp_voice": _z(0x0809_7374),
        "game_play_oid_voice": _z(0x0805_4730),
        "akoid_open_check": _z(0x0802_19A0),
        # Boot / statechart-pump path (recovered this bring-up; unnamed FUN_* in
        # the ZC3201 decomp — see docs/zc3201-boot-feasibility.md):
        "boot_task_main": _z(0x0802_38BC),  # = prog_entry; ROM entry, tail event pump
        "app_init_main": _z(0x0802_36C0),  # SoC init + mount + install statechart
        "fs_storage_mount_init": _z(0x0802_50E0),  # hangs forever on mount failure
        "sm_dispatch_hierarchy": _z(0x0800_16A8),  # per-event dispatch to current state
        "mtd_extra_bitmap": _z(0x0802_2A8C),  # MTD/NFTL bitmap init (reached at boot)
        "irq_mask_push": 0x07FF_DB00,  # HAL: save+zero INT_ENABLE (nandboot — no shift)
        "irq_mask_pop": 0x07FF_DB48,  # HAL: restore INT_ENABLE (nandboot — no shift)
        "state_init_power_on": _z(0x0803_0E48),  # INIT-state LEAF handler (dispatched)
        "state_stdb_standby": _z(0x0803_6454),
        "event_post": _z(0x0800_1544),
        "nand_disk_mount": _z(0x0802_B45C),
        # Generic scripted-GME interpreter (twins of the 2N engine)
        "gme_parse_header": _z(0x0804_572C),
        "gme_parse_oidrange": _z(0x0804_5648),
        "gme_reset_regs": _z(0x0804_56B4),
        "gme_clear": _z(0x0804_5420),
        "gme_oid_to_playscript": _z(0x0804_5358),
        "gme_parse_check_conditions": _z(0x0804_523C),
        "gme_parse_actions": _z(0x0804_4EC8),
        "gme_parse_playlist": _z(0x0804_4DE8),
        "gme_parse_media_offsets": _z(0x0804_4D38),
        "gme_exec_command": _z(0x0804_46E4),
        "gme_oid_tap_handler": _z(0x0804_3358),
        # App-context globals (RAM) — reveng addrs + 0x8000
        "gb_app_context": _z(0x0800_779C),
        "p_pMeGame_slot": _z(0x081D_8854),
        "gme_file_handle_ptr": _z(0x080D_20A0),
        "chomp_handle_ptr": _z(0x080D_28FC),
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
