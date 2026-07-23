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

import struct
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

    #: Composite idle ``GPIO_IN`` (bank-0) word this generation reads when no
    #: device drives a pin. MT's retail idle is ``0x3201``; ZC3201 clears bit0
    #: (a battery-OK comparator that idles released/0, where MT sets it). ZC3201's
    #: standby state machine (``FUN_0803ef7c``) waits for ``GPIO_IN`` bit0 == 0
    #: before descending INIT→standby→book (nandboot ``func_0x08006978(0)`` =
    #: ``0x040000bc`` bit0); presenting the authentic ZC3201 idle level lets
    #: standby descend rather than spin (``docs/zc3201-boot-feasibility.md``
    #: "Leg 18"). Only the model's GPIO idle base — device overrides still win.
    gpio_in_idle: int = 0x0000_3201

    #: OID sensor wiring (``tt_emu.peripherals.oid.OidSensor``): the two-wire
    #: bit-bang link's clock/data GPIO pins and the firmware's own capture-state
    #: ``bit_count`` byte. Same protocol on both generations, different pins/RAM:
    #: MT clocks GPIO2 / data GPIO9 / ``bit_count`` 0x08008C09; ZC3201 clocks
    #: GPIO7 / data GPIO16 / ``bit_count`` 0x08007BF9 (its nandboot OID HAL —
    #: ``hal_oid_bus_idle`` 0x08005CB0, ``hal_oid_shift_in`` 0x08005D80, 40 ms
    #: poll ``hal_oid_timer_start`` 0x08005CF0 → callback 0x08005F48, armed by
    #: ``state_init_power_on`` (0x08038EAC) and the book-descent SM (0x0803F13C);
    #: ``docs/zc3201-boot-feasibility.md`` "Leg 20"). ``None`` = don't wire an OID
    #: sensor (a firmware whose OID path is not modelled yet).
    oid_pin_clock: int = 2
    oid_pin_data: int = 9
    oid_bit_count_addr: int = 0x0800_8C09

    #: GPIO output pin whose latch is mirrored back into GPIO_IN as the amp-enable
    #: readback (``GpioBlock`` §1.1). MT: GPIO16; ZC3201: GPIO9 (GPIO16 there is
    #: the OID data line, so the mirror must move or it clobbers OID data).
    gpio_amp_pin: int = 16

    #: Initial SVC-mode stack top the boot seed sets (SP grows down from here).
    #: The stack must lie inside the firmware's **valid-pointer window**: the
    #: ``Utl_UStr*`` bounded string-copy routines guard every operand with a
    #: pointer-range check and **silently no-op** on an out-of-range address. MT's
    #: guard admits the full 4-MiB RAM (top ``0x08400000``); **ZC3201's guard
    #: (``FUN_08008a04``) is only the lower 2 MiB — ``addr ∈ [0x08000000,
    #: 0x08200000]``**. With the MT stack top the discovery scan builds its ``"B:"``
    #: root path on a stack ~``0x083f…`` that fails the check, so the copy into the
    #: scan context is skipped, the root stays garbage, the ``B:`` ``*.gme`` scan
    #: enumerates nothing, and no game is ever discovered. The stack must sit below
    #: ``0x08200000`` (above the ~``0x081e…`` heap top).
    svc_stack_top: int = 0x0840_0000

    #: Initial IRQ-mode stack top the machine seeds on the first interrupt (the
    #: from-entry boot skips the reset handler's per-mode stack setup). It must lie
    #: in a resident, mapped window: MT's emulator-chosen ``0x083F0000`` (top of the
    #: 4-MiB RAM, always identity-mapped); ZC3201's authentic reset-handler value
    #: ``0x08008000`` (the MT default would fault outside the 1st-gen demand window
    #: ``[0x08008000, 0x08200000)`` — see ``Machine.irq_stack_top``).
    irq_stack_top: int = 0x083F_0000

    #: Decouple the top-level and second-level timer interrupt latches in the
    #: :class:`~tt_emu.peripherals.intc.IntcTimer` model. ZC3201's timer ISR acks the
    #: top-level line-10 status before reading the second-level timer-fired bit, so
    #: the two must latch independently or the HAL software-timer tick never fires
    #: (``docs/zc3201-boot-feasibility.md``). MT's ISR reads them together, so it
    #: keeps the coupled default.
    intc_timer_ack_decouple: bool = False

    #: SoC chip-ID constant read at ``0x04000000`` (SysCon REG_CHIP_ID). Per-
    #: generation: 2N-MT ``0x30393031`` ("1090"), ZC3201 ``0x33323931`` ("1923").
    #: The firmware's FAT/MtdLib version gate (``fw_version_ref`` 0x0802c880 /
    #: ``mtd_helper_eb24`` 0x080b6b24) reads this and zeroes its library descriptor
    #: on mismatch — a wrong value silently corrupts the FS heap during mount. It is
    #: *also* the value the producer's MtdLib pool-init SoC-signature check
    #: (``0x08012a0c``) matches — a wrong value there wipes the allocator pool and
    #: aborts the format (docs/zc3201-producer-addresses.md §10).
    soc_chip_id: int = 0x3039_3031

    #: Small-page NAND geometry (ZC3201 Samsung K9F5608: 512-B page + 16-B OOB,
    #: 32 pages/block). Selects :class:`NfcController`'s small-page decode (the NFC
    #: ``row`` is the absolute page, offset = ``row·page_size + col``, one combined
    #: 512+16 transfer). ``False`` keeps MT's large-page allocation-unit decode.
    nand_small_page: bool = False
    nand_page_size: int = 512

    #: Firmware-absolute PC of the NAND spare-surface strobe leaf (ZC3201 nandboot
    #: ``0x080030d8``). The small-page ``MtdLib`` readspare presents a page's OOB
    #: at the L2 window head before reading the 4-byte map tag from ``window[0]``;
    #: on hardware the daughterboard advances the window to the spare when this
    #: leaf pulses its dedicated GPIO. The GPIO bit *latches high* (not an
    #: edge/level signal), so the model keys the hardware effect on the leaf's PC,
    #: which runs only in the readspare path (:meth:`NfcController.surface_spare`).
    #: ``None`` for large-page (MT) firmware, which has no such split.
    nand_spare_surface_strobe: int | None = None

    #: DAC destination-port word the audio DMA submit programs into ``0x04010008``
    #: (``AudioDma._on_start``). MT's bootrom port resolver maps the audio port to
    #: code ``0x6200`` (``0x08086200``); the 1st-gen ZC3201 bootrom (``0x08003928``)
    #: maps audio port 1 to ``0x5200`` (``0x08085200``) — proven at runtime. The
    #: rest of the submit (``(len>>2)|0x2000`` START, bit16 kick) is identical.
    dac_port_dst: int = 0x0808_6200

    #: One live divider→rate calibration point ``(divider, rate_hz)`` for the DAC
    #: rate decode (``AudioDma.current_rate`` / ``rate_from_divider``). MT: ``0x28 →
    #: 22050``. ZC3201: ``0x18 → 32000`` (its 1st-gen audio master clock; the
    #: example media's own context declares 32000 Hz mono).
    dac_rate_ref: tuple[int, int] = (0x28, 22050)

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

    #: ``(addr, bytes)`` of the MtdLib device-geometry struct the skipped nandboot
    #: chip-detect would have written (the ZC3201 analogue of MT's §5.6
    #: ``NAND_GEOMETRY`` seed). ``None`` for firmwares whose real boot populates it
    #: itself. Seeded *after* :attr:`bss_seed` (it lives inside that window).
    nand_dev_geometry: tuple[int, bytes] | None = None

    #: ``(addr, bytes)`` of the nandboot NAND-geometry descriptor the skipped
    #: chip-detect fills — distinct from :attr:`nand_dev_geometry` (the MtdLib
    #: device object) in that it is a low-level nandboot struct. Its byte ``+1``
    #: is the **number of 512-byte sub-page transfers per page read**
    #: (``page_size / 512``): the nandboot bulk-read primitive
    #: (``func_0x080028b0``) loops ``dst = buf + i·512`` for ``i in
    #: range(desc[1])`` when assembling a page. The nandboot image ships the
    #: large-page default (**4**, MT's 2-KiB page); ZC3201's 512-byte-page chip
    #: (K9F5608) reads **1** sub-page, so the skipped chip-detect would set it to
    #: 1. Left at 4, the map-read (into a ``dev+0x1c`` = 512-byte buffer) reads
    #: 2 KiB and overruns the buffer into the just-allocated MtdLib manager,
    #: zeroing its pages-per-block divisor → a FatLib divide-by-zero
    #: (``docs/zc3201-boot-feasibility.md`` "Leg 15"). ``None`` for firmwares whose
    #: real boot populates it (MT). Seeded after the nandboot image is loaded.
    nandboot_geom_seed: tuple[int, bytes] | None = None

    #: ``(addr, bytes)`` of the nandboot **system-bin shift globals** the skipped
    #: chip-detect (nandboot init ``FUN_0x08001160``) derives from the device
    #: geometry — three consecutive bytes in the nandboot descriptor struct
    #: (``0x080070e0 + 0x4c2``): ``+0x4c2`` = ``log2(page_size)``, ``+0x4c3`` =
    #: ``log2(pages_per_block)``, ``+0x4c4`` = the plane/interleave factor
    #: (``dev+0xc``). The nandboot **boot-file loader** (``FUN_0x08000868`` →
    #: ``FUN_0x08000cd0``) uses these to walk a system file's block map. From-entry
    #: they read **0** (the init that would set them is skipped), so the loader's
    #: per-page arithmetic collapses. ZC3201's K9F5608 (512-B page, 32 pages/block,
    #: 2 planes) → ``(9, 5, 2)``. ``None`` for firmwares whose real boot populates
    #: them (MT). Seeded after the nandboot image is loaded — see
    #: ``docs/zc3201-boot-feasibility.md`` "Leg 16".
    nandboot_shift_seed: tuple[int, bytes] | None = None

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
    gpio_in_idle=0x0000_3200,  # bit0 (battery-OK comparator) idles released on ZC3201
    oid_pin_clock=7,           # nandboot hal_oid_shift_in clocks GPIO7
    oid_pin_data=16,           # ...and samples data/attention on GPIO16 (MT: GPIO9)
    oid_bit_count_addr=0x0800_7BF9,  # capture-state struct 0x08007bf8, bit_count +1
    gpio_amp_pin=9,            # ZC3201 audio amp is GPIO9 (frees GPIO16 for OID data)
    dac_port_dst=0x0808_5200,  # bootrom port resolver 0x08003928 maps audio port 1 -> 0x5200
    dac_rate_ref=(0x18, 32000),  # 1st-gen DAC clock: divider 0x18 -> 32000 Hz (media ctx: 32000 mono)
    # The AUTHENTIC ZC3201 SVC stack top (Leg 22): the reset handler's own stack
    # setup — nandboot ``0x07ff8108`` does ``mov r1, #0x8200000; mov sp, r1`` in SVC
    # mode immediately before ``ldr pc, =boot_task_main``. ZC3201's ``Utl_UStr*``
    # pointer guard (``FUN_08008a04``) admits only ``[0x08000000, 0x08200000]``
    # (2 MiB, vs MT's 4 MiB), and the discovery scan builds its ``"B:"`` root path on
    # the stack; at the MT top (0x08400000) SP ~0x083f… fails the guard, the copy is
    # skipped, the root stays garbage and no ``.gme`` is enumerated. At the real
    # 0x08200000 the stack-resident root passes the guard, discovery enumerates
    # ``B:/EXAMPLE.GME`` into ``studylist.lst``, and a product-OID tap mounts + reaches
    # the play path — with the high-heap objects (0x081d8058, 0x081d9ad8, 0x081dd000,
    # all < 0x08200000) intact, exactly as on hardware. (Leg 21 saw perturbation only
    # because the B: partition was unmountable then — see nand_image_zc3201.)
    svc_stack_top=0x0820_0000,
    irq_stack_top=0x0800_8000,  # authentic reset-handler value (in the resident low region)
    intc_timer_ack_decouple=True,  # ZC3201 timer ISR acks top-level before 2nd-level bit
    soc_chip_id=0x3332_3931,  # "1923" — the ZC3201 SoC chip-ID (FS version gate)
    nand_small_page=True,     # Samsung K9F5608: 512-B page + 16-B OOB, 32 pages/block
    nand_page_size=512,
    nand_spare_surface_strobe=0x0800_30D8,  # nandboot readspare spare-surface leaf
    # MtdLib device-geometry struct at dev=0x08007d94, decoded from the K9F5608
    # flash_ic row (update.upd[0x200]) the way the producer's descriptor builder
    # 0x080056d4 does (docs/zc3201-producer-addresses.md §10): u32 fields
    # +0=custom(1) +4=flag(0x10000000) +8=chips*planes(2) +0xc=planes(2)
    # +0x10=planeblocks(1024) +0x14=pages/block(32) +0x18=1 +0x1c=page bytes(512).
    # dev+8*dev+0x10 = 2048 total blocks; dev+0x14/dev+0x1c drive the mount.
    nand_dev_geometry=(
        0x0800_7D94,
        struct.pack("<8I", 1, 0x1000_0000, 2, 2, 1024, 32, 1, 512),
    ),
    nand_read_id=0xBDA5_75EC,  # Samsung K9F5608 (bytes EC 75 A5 BD, 512-byte page)
    # nandboot NAND-geometry descriptor 0x08006fa0: byte +1 = 512-byte sub-page
    # transfers per page read. The image ships the large-page default 4; the
    # 512-byte-page K9F5608 reads 1 (byte +0 = 2 is already correct in the image
    # and is kept). See nandboot_geom_seed.
    nandboot_geom_seed=(0x0800_6FA0, bytes([2, 1, 0, 0])),
    # nandboot system-bin shift globals 0x080075a2..a4: log2(512)=9, log2(32)=5,
    # plane factor dev+0xc=2. The nandboot boot-file loader needs these to walk a
    # system file's block map; from-entry they read 0. See nandboot_shift_seed.
    nandboot_shift_seed=(0x0800_75A2, bytes([9, 5, 2])),
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
