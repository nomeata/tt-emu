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
    assert ZC3201.prog_load == 0x0800_0000 and ZC3201.nandboot_alias is None
    # prog_entry is the pre-init boot task (ROM entry), NOT the INIT-state leaf
    # handler 0x08030e48 (which is dispatched by the pump; recorded in symbols).
    assert ZC3201.prog_entry == 0x0802_38BC  # boot_task_main
    assert ZC3201.symbols["state_init_power_on"] == 0x0803_0E48  # the leaf handler
    assert ZC3201.symbols["app_init_main"] == 0x0802_36C0
    assert ZC3201.bss_seed == (0x0800_7000, 0x2000)  # crt0-equivalent .bss zero
    assert MT.bss_seed is None  # MT enters after crt0
    assert MT.boots_to_book and not ZC3201.boots_to_book


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
    (``boot_task_main`` 0x080238bc) with the reused MT SoC core peripherals + the
    crt0 ``.bss`` seed, runs ``app_init_main`` → subsystem inits → MTD/storage
    init — no demand-paging MMU, no hooks. It must clear the HAL IRQ-nesting
    overflow guard (which the crt0 seed defuses) and reach ``mtd_extra_bitmap``.
    Reaching book mode from here needs the ZC3201 NAND/NFC controller + image
    (``docs/zc3201-boot-feasibility.md`` step 3).
    """
    from collections import Counter

    from unicorn import UC_HOOK_CODE

    from tt_emu.boot import build_zc3201_machine
    from tt_emu.loader import load_upd
    from tt_emu.machine import MachineConfig

    fw = load_upd(str(ZC_PATH))
    machine = build_zc3201_machine(fw, MachineConfig(instructions_per_tick=20_000))
    reached: set[str] = set()
    machine.on_code(ZC3201.symbols["app_init_main"], lambda _m: reached.add("app_init"))
    machine.on_code(ZC3201.symbols["mtd_extra_bitmap"], lambda _m: reached.add("mtd"))
    hot: Counter[int] = Counter()
    machine.uc.hook_add(UC_HOOK_CODE, lambda _uc, a, _s, _u: hot.update((a,)))

    result = machine.run(15_000_000)

    # Forward progress well past the seed: the boot task called app_init_main and
    # the firmware reached storage/MTD init.
    assert "app_init" in reached, "app_init_main never entered"
    assert "mtd" in reached, "MTD/storage init (mtd_extra_bitmap) never reached"
    # It did NOT wedge on the HAL IRQ-nesting overflow guard (the seed defused it).
    HANG = 0x07FF_DB14
    assert hot.get(HANG, 0) == 0, "hung at irq_mask_push nesting-overflow guard"
    assert result.pc >> 24 == 0x08 or result.pc >> 20 == 0x07F  # in PROG or HAL, running
    in_prog = {a for a in hot if 0x0800_0000 <= a < 0x0810_0000}
    assert len(in_prog) > 400, f"expected deep PROG coverage, got {len(in_prog)}"
