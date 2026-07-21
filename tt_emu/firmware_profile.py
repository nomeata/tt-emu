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
    "ProducerProfile",
    "MT",
    "PROFILES",
    "ZC3201",
    "by_key",
    "detect",
]


@dataclass(frozen=True)
class ProducerProfile:
    """Addresses to DRIVE this generation's factory ``producer.bin`` under Unicorn.

    The factory USB-flash tool formats the NAND with the exact ``MtdLib``/``FatLib``
    layout the firmware later mounts, so running it against a Python NAND model is
    the authoritative way to provision a mountable image (see
    :mod:`tt_emu.nand_provision` and ``docs/producer-run-results.md`` for the MT
    ground truth, ``docs/zc3201-producer-addresses.md`` for the 1st-gen addresses).
    Every address is producer-image absolute (the producer loads flat at
    ``load_base``, unrelated to the PROG firmware's own link base).
    """

    load_base: int          #: producer.bin load/entry address (reset vector)
    entry: int              #: startup entry (== load_base for a `b reset` word0)
    usb_loop: int           #: main's host-command loop — the harness stops here
    svc_sp: int
    irq_sp: int

    disp_burn: int          #: pr_cmd_burn dispatcher (cmds 7 / 0x10 / 0x11 / 0x22)
    disp_media: int         #: pr_cmd_media dispatcher (cmds 5 / 6 / 9 / 10 / …)
    cmd_ctx: int            #: command-context global; command number at +4

    #: OS/HAL vtable NAND-op leaves (the seam the harness hooks to the NAND model).
    n_erase: int            #: vtable +0x20  erase block          (r1=block)
    n_write: int            #: vtable +0x24  write page           (r1=blk,r2=pg,r3=data)
    n_read: int             #: vtable +0x28  read page
    n_bootwr: int           #: vtable +0x2c  boot-page write      (r0=block,r1=data)
    n_wrpg0: int            #: vtable +0x34  write page0 + tag
    n_rdpg0: int            #: vtable +0x38  read page0 / bad-check

    r_malloc: int           #: vtable +0x00  malloc
    r_free: int             #: vtable +0x04  free
    r_printf: int           #: vtable +0x1c  printf (log oracle)

    #: chip read-ID: the NFC data register the producer polls for the chip-ID
    #: dword, the read-id peek site (records the CE index), and the dword to
    #: return for CE0 (the pen has ONE chip; other CEs answer 0xffffffff).
    nfc_readid_reg: int
    readid_peek: int
    chip_id: int

    pr_ctx: int             #: PR ctx global ({+0 chip count; +4 mode; …})
    anyka_ic_gate: int      #: MtdLib "is-Anyka-IC" check → force 1 (real pen is Anyka)
    ready_poll: int         #: is_bad_bitmap/ready poll → force "ready" (0)
    badchk: int             #: chip[+0x38] bad-block-check primitive (0 good / 1 OOB)

    #: Optional: the startup calibration-loop bound literal to shorten (addr, value).
    calib_bound: tuple[int, int] | None = None
    #: FsLib driver object + the null method slot to stub for mkfs (cmd 10).
    fslib_driver_obj: int | None = None


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

    #: Fixed hardware address of the L2 NAND data-staging SRAM window (buffer 4).
    #: The L2 buffer block sits at a generation-specific SRAM base: 2N-MT stages
    #: into ``0x08006800``; ZC3201 into ``0x08005800`` (recovered from its nandboot
    #: data-transfer leaf — see :class:`tt_emu.peripherals.nand.NfcController`).
    #: For ZC3201 the MT window would collide with the nandboot-alias code (which
    #: extends to ``~0x08006fe4``), clobbering a HAL leaf.
    nand_sram_window: int = 0x0800_6800

    #: SoC chip-ID constant read at ``0x04000000`` (SysCon REG_CHIP_ID). Per-
    #: generation: 2N-MT ``0x30393031`` ("1090"), ZC3201 ``0x33323931`` ("1923").
    #: The firmware's FAT/MtdLib version gate (``fw_version_ref`` 0x0802c880 /
    #: ``mtd_helper_eb24`` 0x080b6b24) reads this and zeroes its library descriptor
    #: on mismatch — a wrong value silently corrupts the FS heap during mount. It is
    #: *also* the value the producer's MtdLib pool-init SoC-signature check
    #: (``0x08012a0c``) matches — a wrong value there wipes the allocator pool and
    #: aborts the format (docs/zc3201-producer-addresses.md §10).
    soc_chip_id: int = 0x3039_3031

    #: NAND READ-ID dword the pen's boot probe expects (``NfcController.read_id``).
    #: 2N-MT: Samsung K9GAG08U0M ``0x9551D3EC`` (bytes EC D3 51 95, 4-KiB page).
    #: ZC3201: Samsung K9F5608 ``0xBDA575EC`` (bytes EC 75 A5 BD, 512-byte page) —
    #: recovered from the ``.upd`` ``flash_ic`` descriptor at offset ``0x200`` and
    #: confirmed against the producer's own chip-detect (§10). Small-page geometry:
    #: page 512, spare 16, 32 pages/block, 2048 blocks, planeblocks 1024, 1 column
    #: + 2 row address cycles. Serving it faithfully is the remaining mount step.
    nand_read_id: int = 0x9551_D3EC

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

    #: Addresses to drive this generation's ``producer.bin`` to format a NAND
    #: image (:mod:`tt_emu.nand_provision`); ``None`` until reverse-engineered.
    producer: ProducerProfile | None = None

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
    # Proven against fw/2N-update3202MT/data/producer.bin (mt-producer-addresses.md
    # + firmware-re/tools/ttrun_producer.py); the reusable seam is validated here
    # before retargeting to ZC3201 (which has no reference output).
    producer=ProducerProfile(
        load_base=0x0800_0000,
        entry=0x0800_02F0,
        usb_loop=0x0800_6C58,
        svc_sp=0x0802_E000,
        irq_sp=0x0802_F000,
        disp_burn=0x0800_6D9C,
        disp_media=0x0800_70CC,
        cmd_ctx=0x0802_C9C0,
        n_erase=0x0800_5978,
        n_write=0x0800_59B4,
        n_read=0x0800_5ACC,
        n_bootwr=0x0800_5BA8,
        n_wrpg0=0x0800_6018,
        n_rdpg0=0x0800_60EC,
        r_malloc=0x0800_04AC,
        r_free=0x0800_0510,
        r_printf=0x0800_07B4,
        nfc_readid_reg=0x0404_A150,
        readid_peek=0x0800_35EC,
        chip_id=0x9510_DCAD,
        pr_ctx=0x0802_CA00,
        anyka_ic_gate=0x0801_22B8,
        ready_poll=0x0800_10B8,
        badchk=0x0800_5410,
        calib_bound=(0x0800_03EC, 2),
        fslib_driver_obj=0x0802_CC48,
    ),
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
    nand_sram_window=0x0800_5800,  # L2 buffer 4 (base 0x08005000 + 4·0x200)
    soc_chip_id=0x3332_3931,  # "1923" — the ZC3201 SoC chip-ID (FS version gate)
    nand_read_id=0xBDA5_75EC,  # Samsung K9F5608 (bytes EC 75 A5 BD, 512-byte page)
    boots_to_book=False,
    symbols={
        # HAL / FSLib (names.csv, lab hook points) — reveng PROG addrs + 0x8000
        "fs_open": _z(0x0800_40EC),  # runtime 0x0800c0ec (names.csv, re-based DB)
        "fs_read": _z(0x0800_41C4),  # runtime 0x0800c1c4
        "fs_seek": _z(0x0800_41D4),  # runtime 0x0800c1d4
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
        # App-context globals (RAM) — ABSOLUTE baked literals, NOT shifted. Verified
        # against the image: each value appears as a 4-aligned literal word in
        # ZC3201/data/PROG.bin (gb_app_context 106x, p_pMeGame_slot 2x,
        # gme_file_handle_ptr 74x, chomp_handle_ptr 1x); the +0x8000 candidates
        # appear 0x. Data literals are absolute and already encode the final RAM
        # address, so the PROG_BASE_FIX that lifts *code* addresses does not apply.
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
