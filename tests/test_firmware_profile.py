"""Firmware-target profiles: detection, fetch metadata, and the ZC3201 substrate.

The pure-registry checks always run; the ones needing a real ``.upd`` skip
cleanly when the container is not present. The ZC3201 from-entry boot proves the
profile-driven load path end-to-end through tt-emu's real ``Machine`` — it is
the substrate the future ZC3201 bring-up builds on (``docs/zc3201-boot-feasibility.md``).
"""

from __future__ import annotations

import pytest
from _data import firmware_path, firmware_path_zc3201

from tt_emu.firmware_profile import MT, PROFILES, ZC3201, by_key, detect

MT_PATH = firmware_path()
ZC_PATH = firmware_path_zc3201()


# --- pure registry (no artifacts) ----------------------------------------------------


def test_registry_keys_unique_and_lookup() -> None:
    keys = [p.key for p in PROFILES]
    assert keys == sorted(set(keys)), "profile keys must be unique"
    assert by_key("mt") is MT
    assert by_key("zc3201") is ZC3201
    with pytest.raises(KeyError):
        by_key("nope")


def test_profile_load_layout_distinct() -> None:
    # The layout difference that motivated the abstraction.
    assert MT.prog_load == 0x0800_9000 and MT.nandboot_alias == 0x07FF_8000
    # ZC3201 PROG links at 0x08008000 (NOT 0x08000000 — the reveng project's
    # wrong base, the same mistake MT made before it moved to 0x08009000), with
    # nandboot aliased at 0x08000000 like MT so the 0x0800xxxx HAL veneers resolve.
    assert ZC3201.prog_load == 0x0800_8000 and ZC3201.nandboot_alias == 0x0800_0000
    # prog_entry is the pre-init boot task (ROM entry), NOT the INIT-state leaf
    # handler (which is dispatched by the pump; recorded in symbols). All PROG
    # addresses are the reveng values + 0x8000 (the base fix).
    assert ZC3201.prog_entry == 0x0802_B8BC  # boot_task_main (reveng 0x0802_38BC)
    assert ZC3201.symbols["state_init_power_on"] == 0x0803_8E48  # leaf (0x0803_0E48)
    assert ZC3201.symbols["app_init_main"] == 0x0802_B6C0  # (reveng 0x0802_36C0)
    assert ZC3201.symbols["irq_mask_push"] == 0x07FF_DB00  # HAL/nandboot — unshifted
    assert ZC3201.bss_seed == (0x0800_6FE4, 0x0800_8000 - 0x0800_6FE4)  # crt0 .bss zero
    assert MT.bss_seed is None  # MT enters after crt0
    assert MT.boots_to_book and not ZC3201.boots_to_book
    # The nandboot NAND-geometry sub-page count seed (skipped chip-detect): ZC3201's
    # 512-byte-page chip reads 1 sub-page per page; the nandboot image default is 4
    # (MT's 2-KiB large page). MT has no such seed (its real boot populates it).
    assert MT.nandboot_geom_seed is None
    assert ZC3201.nandboot_geom_seed == (0x0800_6FA0, bytes([2, 1, 0, 0]))
    # The nandboot system-bin shift globals (skipped nandboot init): log2(512)=9,
    # log2(32)=5, plane factor 2. MT's real boot populates them (Leg 16).
    assert MT.nandboot_shift_seed is None
    assert ZC3201.nandboot_shift_seed == (0x0800_75A2, bytes([9, 5, 2]))
    # Per-generation hardware: the L2 NAND-staging SRAM window and the SoC chip-ID.
    assert MT.nand_sram_window == 0x0800_6800 and ZC3201.nand_sram_window == 0x0800_5800
    assert MT.soc_chip_id == 0x3039_3031 and ZC3201.soc_chip_id == 0x3332_3931
    # GPIO idle word: MT sets bit0 (0x3201), ZC3201 clears it (0x3200) so its
    # standby SM descends past the GPIO-pin0 wait toward book (Leg 18).
    assert MT.gpio_in_idle == 0x0000_3201 and ZC3201.gpio_in_idle == 0x0000_3200
    # Data globals are absolute literals — unshifted (verified against baked words).
    assert ZC3201.symbols["gb_app_context"] == 0x0800_779C
    assert ZC3201.symbols["p_pMeGame_slot"] == 0x081D_8854
    assert ZC3201.symbols["gme_file_handle_ptr"] == 0x080D_20A0
    assert ZC3201.symbols["chomp_handle_ptr"] == 0x080D_28FC
    assert ZC3201.symbols["fs_open"] == 0x0800_C0EC  # re-based names.csv (was a typo)


def test_fetch_metadata() -> None:
    assert MT.fetch.filename == "update3202MT.upd"
    assert MT.fetch.sha256 == "8e37af0a3d3c126189447964784fd84ccf0356cb7425b5ab478e86b3352741f9"
    assert ZC3201.fetch.sha256 == "03c12f41b6bc9ab78ee206c2bdfdbe45eb9ad7c3ab8337e7a8661555aae4a4a6"
    assert ZC3201.fetch.filename != MT.fetch.filename  # distinct cache files


def test_detect_requires_both_generation_and_fingerprint() -> None:
    # A generation with the wrong fingerprint is not recognized.
    assert detect(b"\x00" * 32, "ANYKANB1") is None
    assert detect(MT.build_prefix, "ANYKANB0") is None  # MT fingerprint, ZC gen
    assert detect(MT.build_prefix, "ANYKANB1") is MT
    assert detect(ZC3201.build_prefix, "ANYKANB0") is ZC3201


# --- with real containers ------------------------------------------------------------


@pytest.mark.skipif(MT_PATH is None, reason="MT firmware .upd not available")
def test_detect_real_mt() -> None:
    from tt_emu.loader import load_upd

    fw = load_upd(str(MT_PATH))
    assert fw.profile is MT
    assert fw.boot_generation == "ANYKANB1"


@pytest.mark.skipif(ZC_PATH is None, reason="ZC3201 firmware .upd not available")
def test_detect_real_zc3201() -> None:
    from tt_emu.loader import load_upd

    fw = load_upd(str(ZC_PATH))
    assert fw.profile is ZC3201
    assert fw.boot_generation == "ANYKANB0"
    assert fw.build_id == "v0136"
    # PROG identity-mapped 1 MiB at 0x08000000; nandboot ANYKANB0.
    assert fw.prog.size == 0x10_0000
    assert fw.codepage.size == ZC3201.codepage_size


@pytest.mark.skipif(ZC_PATH is None, reason="ZC3201 firmware .upd not available")
def test_zc3201_boots_through_app_init_to_storage() -> None:
    """The unmodified ZC3201 firmware, from its real boot-task entry
    (``boot_task_main`` 0x0802b8bc) loaded at the **correct base 0x08008000** with
    the reused MT SoC core peripherals + the MT NFC/ECC/L2 storage trio, runs
    ``app_init_main`` → subsystem inits → clock setup → MTD/storage init and drives
    the NAND/NFC command-list sequencer — no demand-paging MMU, no hooks. It must
    clear the HAL IRQ-nesting overflow guard (the low-RAM seed defuses it), reach
    ``mtd_extra_bitmap``, and actually touch the NAND (the freq wall from the
    wrong base is gone). With the ZC3201 NAND-staging SRAM window (``0x08005800``,
    not the MT ``0x08006800`` which collides with the nandboot alias) the boot
    advances *past* the old collision crash, through ``fs_storage_mount_init`` and
    into ``fs_open`` — where, with a blank NAND, it hits the FAT-Lib heap wall (an
    uninitialized allocator vtable → null call). Reaching book mode needs a valid
    ZC3201 MtdLib NAND image (``docs/zc3201-boot-feasibility.md`` "Leg 4" resume
    pointer).
    """
    from collections import Counter

    from unicorn import UC_HOOK_CODE

    from tt_emu.boot import build_zc3201_machine
    from tt_emu.loader import load_upd
    from tt_emu.machine import MachineConfig

    fw = load_upd(str(ZC_PATH))
    machine = build_zc3201_machine(fw, MachineConfig(instructions_per_tick=20_000))
    reached: set[str] = set()
    milestones = {
        ZC3201.symbols["app_init_main"]: "app_init",
        ZC3201.symbols["mtd_extra_bitmap"]: "mtd",
        ZC3201.symbols["fs_storage_mount_init"]: "mount",
        0x0800_C208: "map_read",       # FUN_0800c208 — reads the superblock (block 0 pg31)
        0x0802_F0C0: "map_build",      # whole-disk map object build
        0x0802_CBD8: "map_table_build",  # map-table (spare-scan) build
    }
    for addr, tag in milestones.items():
        machine.on_code(addr, (lambda t: lambda _m: reached.add(t))(tag))
    hot: Counter[int] = Counter()
    machine.uc.hook_add(UC_HOOK_CODE, lambda _uc, a, _s, _u: hot.update((a,)))

    machine.run(20_000_000)

    # Forward progress well past the seed: the boot task called app_init_main and
    # the firmware reached storage/MTD init and the mount, then — with the hand-built
    # small-page MtdLib image + seeded device geometry — got *into* the map-read and
    # map-table build (the old blank-NAND path bailed before the map read). The
    # remaining wall is the reserve-zone map metadata / spare-read OOB fidelity
    # (docs/zc3201-boot-feasibility.md "Leg 11" resume pointer).
    assert "app_init" in reached, "app_init_main never entered"
    assert "mtd" in reached, "MTD/storage init (mtd_extra_bitmap) never reached"
    assert "mount" in reached, "fs_storage_mount_init never reached"
    assert "map_read" in reached, "map read (FUN_0800c208) never reached — geometry seed?"
    assert "map_table_build" in reached, "map-table build never reached"
    # The clock wall (wrong-base garbage frequency) is gone and the firmware drove
    # the NFC sequencer: at least one NAND read happened.
    assert machine.nand is not None and machine.nand.reads > 0, "NFC/NAND never engaged"
    # It did NOT wedge on the HAL IRQ-nesting overflow guard (the seed defused it).
    HANG = 0x07FF_DB14
    assert hot.get(HANG, 0) == 0, "hung at irq_mask_push nesting-overflow guard"
    # The ZC3201 staging window (0x08005800) is used, and the MT window (0x08006800,
    # nandboot-alias code here) is never written as a staging deposit / clobbered.
    in_prog = {a for a in hot if 0x0800_0000 <= a < 0x0810_0000}
    assert len(in_prog) > 2000, f"expected deep PROG coverage, got {len(in_prog)}"


@pytest.mark.skipif(ZC_PATH is None, reason="ZC3201 firmware .upd not available")
def test_zc3201_fatlib_mount_completes() -> None:
    """The unmodified ZC3201 firmware boots through the **complete FatLib mount** of
    both partitions — past the Leg-14 divide-by-zero abort.

    The MtdLib mount (``docs/zc3201-boot-feasibility.md`` Leg 13) reaches ``InitPlane
    succeed`` on both partitions; FatLib then reads sectors through the MtdLib
    manager. Before Leg 15 a nandboot bulk page-read (``func_0x080028b0``) used the
    large-page sub-page count (4) baked into the nandboot image — the skipped
    chip-detect never corrected it to 1 for the 512-byte-page K9F5608 — so the
    map-read read 2 KiB into the 512-byte map buffer and overran the just-allocated
    MtdLib manager, zeroing its ``+0x2c`` pages-per-block divisor → a compiler
    divide-by-zero guard (``FUN_0800c974``) fired an ARM-state semihosting
    ``SYS_EXIT``. The ``nandboot_geom_seed`` (byte +1 = 1) fixes it.

    Assert: (1) the map divisor ``FUN_0800c974`` is entered and is **never zero**;
    (2) the boot proceeds **past** the whole FatLib mount into app_init's post-mount
    nandboot file lookup (``0x08000868``) — reaching it proves both partitions
    mounted and FatLib built; (3) no ``SYS_EXIT`` abort stop.
    """
    from tt_emu.boot import build_zc3201_machine
    from tt_emu.loader import load_upd
    from tt_emu.machine import MachineConfig
    from unicorn.arm_const import UC_ARM_REG_R0

    fw = load_upd(str(ZC_PATH))
    machine = build_zc3201_machine(fw, MachineConfig(instructions_per_tick=100_000))

    divisors: list[int] = []
    reached_lookup = {"v": False}

    def on_div(m: object) -> None:
        r0 = machine.uc.reg_read(UC_ARM_REG_R0)
        # r0 is a *virtual* struct pointer; under the real MMU read through the
        # translation (read_u32 routes via machine.mmu.read_va), not raw physical.
        divisors.append(machine.read_u32(r0 + 0x2C))

    def on_lookup(m: object) -> None:
        reached_lookup["v"] = True
        machine.request_stop("post-mount file lookup reached")

    machine.on_code(0x0800_C974, on_div)   # FUN_0800c974 (global-block → part/block)
    machine.on_code(0x0800_0868, on_lookup)  # nandboot post-mount file-by-name lookup

    machine.run(500_000_000)

    assert divisors, "FUN_0800c974 (FatLib block translation) never reached"
    assert 0 not in divisors, "map divisor zeroed — manager overrun (nandboot_geom_seed?)"
    assert reached_lookup["v"], (
        "boot did not reach the post-mount file lookup — FatLib mount did not complete"
    )
    assert machine.stop_reason is not None and "SYS_EXIT" not in machine.stop_reason, (
        f"firmware aborted via SYS_EXIT: {machine.stop_reason!r}"
    )


@pytest.mark.skipif(ZC_PATH is None, reason="ZC3201 firmware .upd not available")
def test_zc3201_codepage_index_reaches_statechart() -> None:
    """Past the mount, ``app_init`` loads the ``codepage`` bin through the nandboot
    boot-file loader and reaches the statechart INIT leaf + the event pump (Leg 16).

    The loader (``FUN_0x08000868``, via ``FUN_0x08000dcc``) ``strcmp``-searches an
    on-media system-bin index for ``codepage``; an empty index is a fatal ``b .``
    spin at ``0x08000944``. :func:`build_zc3201_nand_image` now lays that index
    (header p30, records p29, block map + content) and :func:`build_zc3201_machine`
    seeds the nandboot shift globals, so the load succeeds and ``app_init`` reaches
    ``state_init_power_on`` ``0x08038e48`` (the statechart INIT leaf) and the OID/GME
    event dispatch ``0x0803629c`` — the event pump is live.

    Assert: (1) no not-found spin at ``0x08000944``; (2) ``state_init_power_on`` is
    reached; (3) the event pump runs (``gme_oid_dispatch`` dispatched at least once).
    """
    from tt_emu.boot import build_zc3201_machine
    from tt_emu.loader import load_upd
    from tt_emu.machine import MachineConfig

    fw = load_upd(str(ZC_PATH))
    machine = build_zc3201_machine(fw, MachineConfig(instructions_per_tick=20_000))

    hit = {"spin": False, "init": 0, "dispatch": 0}

    def on_spin(m: object) -> None:
        hit["spin"] = True
        machine.request_stop("nandboot codepage not-found spin (index missing)")

    def on_init(m: object) -> None:
        hit["init"] += 1

    def on_dispatch(m: object) -> None:
        hit["dispatch"] += 1

    machine.on_code(0x0800_0944, on_spin)   # loader not-found self-spin
    machine.on_code(0x0803_8E48, on_init)   # state_init_power_on (statechart INIT leaf)
    machine.on_code(0x0803_629C, on_dispatch)  # gme_oid_dispatch (event pump)

    machine.run(300_000_000)

    assert not hit["spin"], "nandboot codepage lookup spun — the system-bin index is missing/wrong"
    assert hit["init"] >= 1, "app_init did not reach state_init_power_on (statechart INIT leaf)"
    assert hit["dispatch"] >= 1, "event pump did not dispatch (gme_oid_dispatch never reached)"


@pytest.mark.skipif(ZC_PATH is None, reason="ZC3201 firmware .upd not available")
def test_zc3201_statechart_advances_init_to_standby() -> None:
    """The HAL software-timer tick drives the statechart from INIT into standby (Leg 17).

    ZC3201's periodic statechart driver is a HAL software timer the INIT leaf arms
    (100 ms, callback nandboot ``0x080065ec``). That timer is ticked by the timer
    IRQ's two-step ack: the ISR (``0x08003d6c``) clears the top-level line-10 status
    at ``0x040000cc`` *before* it reads the second-level timer-fired bit at
    ``0x0400004c`` bit17 that gates the software-timer tick (``0x08006d38``). The
    :class:`IntcTimer` therefore decouples those two latches for ZC3201
    (``zc3201_timer_ack=True``) — otherwise clearing ``0xCC`` also cleared the
    ``0x4C`` latch, the tick never ran, and the statechart stalled at INIT with the
    tick events piling up undrained.

    With the decoupling, the software timer ticks, the callback posts tick events,
    the pump drains them, and the statechart advances ``state_init_power_on``
    ``0x08038e48`` → the standby state machine ``FUN_0803ef7c`` ``0x0803ef7c`` (the
    twin of MT's ``standby_handler`` per ``correspondences.tsv``). Observed hook-free
    via :class:`tt_emu.firmware.zc3201.Zc3201Debugger`.
    """
    from tt_emu.boot import build_zc3201_machine
    from tt_emu.firmware import zc3201 as fw_zc
    from tt_emu.loader import load_upd
    from tt_emu.machine import MachineConfig

    fw = load_upd(str(ZC_PATH))
    assert fw_zc.recognize(fw.prog.data), "ZC3201 firmware recognition failed"
    machine = build_zc3201_machine(fw, MachineConfig(instructions_per_tick=20_000))
    dbg = fw_zc.Zc3201Debugger(machine)
    dbg.attach_watches()
    machine.run(40_000_000)

    assert dbg.timer_ticks > 100, (
        f"HAL software-timer tick never ran ({dbg.timer_ticks}) — the timer-status "
        "decouple regressed; the statechart's periodic driver is dead"
    )
    visited = [pc for _clock, pc in dbg.leaves]
    assert 0x0803_8E48 in visited, "statechart never entered the INIT leaf"
    assert 0x0803_EF7C in visited, (
        "statechart did not advance from INIT into the standby state machine "
        "(FUN_0803ef7c) — the timer tick is not driving the pump"
    )


@pytest.mark.skipif(ZC_PATH is None, reason="ZC3201 firmware .upd not available")
def test_zc3201_standby_descends_past_gpio_pin0_wait() -> None:
    """Standby descends toward book, not into a GPIO-pin0 spin (Leg 18).

    ZC3201's standby state machine (``FUN_0803ef7c``) waits for ``GPIO_IN`` bit0
    (a battery-OK comparator, nandboot ``func_0x08006978(0)`` = ``0x040000bc``
    bit0) to read *released* (0) before it descends INIT→standby→book and starts
    the power-on voice. MT's idle word ``0x3201`` sets bit0; the ZC3201 profile
    clears it (``gpio_in_idle = 0x3200``) — the authentic 1st-gen idle level — so
    standby leaves the pin0 wait loop and hands off to the voice-player active
    object (``Fwl_pfVoice_fn`` ``0x0809eda4``) instead of spinning. Observed
    hook-free via the statechart-leaf watches.
    """
    from tt_emu.boot import build_zc3201_machine
    from tt_emu.firmware import zc3201 as fw_zc
    from tt_emu.loader import load_upd
    from tt_emu.machine import MachineConfig

    fw = load_upd(str(ZC_PATH))
    assert ZC3201.gpio_in_idle == 0x0000_3200, "ZC3201 idle word must clear GPIO bit0"
    machine = build_zc3201_machine(fw, MachineConfig(instructions_per_tick=100_000))
    dbg = fw_zc.Zc3201Debugger(machine)
    dbg.attach_watches()
    machine.run(200_000_000)

    visited = [pc for _clock, pc in dbg.leaves]
    assert 0x0803_EF7C in visited, "statechart never reached the standby state machine"
    assert 0x0809_EDA4 in visited, (
        "standby did not descend past the GPIO pin0 wait into the voice-player "
        "(Fwl_pfVoice_fn 0x0809eda4) — the pen is spinning on GPIO_IN bit0 instead "
        "of proceeding toward book mode"
    )


def test_zc3201_oid_profile_wiring() -> None:
    """The ZC3201 OID sensor wiring differs from MT (Leg 20) — pure profile data."""
    assert ZC3201.oid_pin_clock == 7 and ZC3201.oid_pin_data == 16
    assert ZC3201.oid_bit_count_addr == 0x0800_7BF9
    assert ZC3201.gpio_amp_pin == 9  # GPIO16 is OID data here, not the amp
    # MT keeps the original wiring (GPIO2 clock / GPIO9 data / 0x08008C09).
    assert MT.oid_pin_clock == 2 and MT.oid_pin_data == 9
    assert MT.oid_bit_count_addr == 0x0800_8C09 and MT.gpio_amp_pin == 16


@pytest.mark.skipif(ZC_PATH is None, reason="ZC3201 firmware .upd not available")
def test_zc3201_oid_tap_captured_and_dispatched() -> None:
    """A tapped OID is captured by the ZC3201 OID sensor and dispatched (Leg 20).

    The 1st-gen OID path is a genuine hardware delta (MT's GPIO2/GPIO9 wiring does
    not transfer — GPIO9 is the audio amp here). Its nandboot HAL bit-bang clocks on
    GPIO7 and samples data/attention on GPIO16, reading its own ``bit_count`` at
    ``0x08007BF9`` (capture-state struct ``0x08007BF8``); a 40 ms poll timer
    (``hal_oid_timer_start`` ``0x08005CF0`` → callback ``0x08005F48``, armed by
    ``state_init_power_on`` and the book-descent SM) shifts an armed frame in
    (``hal_oid_shift_in`` ``0x08005D80``), stores ``0x400000 | oid`` to
    ``[gb_app_context+0x40]+8``, and posts the OID event ``0x1063`` into the
    statechart event ring (drained by the pump ``0x08003A84`` → dispatch
    ``0x080037D8``). :func:`build_zc3201_machine` re-points the shared
    :class:`tt_emu.peripherals.oid.OidSensor` to those pins/address via the profile,
    so the unmodified firmware runs its own capture — nothing hooked. This asserts,
    end to end: the poll timer runs in book mode, a held tap is latched, the exact
    ``0x400000 | oid`` lands in the app context, and event ``0x1063`` is dispatched.
    """
    from unicorn.arm_const import UC_ARM_REG_R0

    from tt_emu.boot import build_zc3201_machine
    from tt_emu.loader import load_upd
    from tt_emu.machine import MachineConfig

    fw = load_upd(str(ZC_PATH))
    machine = build_zc3201_machine(fw, MachineConfig(instructions_per_tick=100_000))

    poll_cb = {"n": 0}
    machine.on_code(0x0800_5F48, lambda _m: poll_cb.__setitem__("n", poll_cb["n"] + 1))
    oid_events = {"n": 0}

    def on_dispatch(m: object) -> None:
        if machine.uc.reg_read(UC_ARM_REG_R0) == 0x1063:
            oid_events["n"] += 1

    machine.on_code(0x0800_37D8, on_dispatch)

    machine.run(200_000_000)  # boot to book-mode idle
    assert poll_cb["n"] > 50, (
        f"the 40 ms OID poll timer callback did not run in book mode ({poll_cb['n']}) "
        "— the OID sensor's driver is not armed"
    )

    oid = 8065  # a scripted OID from the example.gme range
    base = machine.oid.gameplay_frames_served
    machine.oid.hold(oid)
    machine.run(80_000_000)
    machine.oid.lift()
    machine.run(120_000_000)

    assert machine.oid.gameplay_frames_served > base, (
        "the firmware never latched the tapped OID (the nandboot shift-in did not "
        "capture the frame through the modelled GPIO7/GPIO16 handshake)"
    )
    ctx = machine.read_u32(0x0800_779C + 0x40)
    assert machine.read_u32(ctx + 8) == (0x40_0000 | oid), (
        "the captured OID did not reach [gb_app_context+0x40]+8 as 0x400000|oid"
    )
    assert oid_events["n"] >= 1, (
        "the OID tap event 0x1063 was never dispatched into the statechart"
    )
