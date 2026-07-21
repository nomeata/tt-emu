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
    assert ZC3201.prog_entry == 0x0803_0E48  # state_init_power_on
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
def test_zc3201_from_entry_runs_init_leaf() -> None:
    """The unmodified ZC3201 firmware runs from ``state_init_power_on`` and
    returns cleanly — the proven forward progress (131 distinct PCs, no unmapped
    access), reproduced through tt-emu's real ``Machine`` via the profile-driven
    load path. Reaching book mode from here needs the not-yet-existing boot RE.
    """
    from unicorn import UC_HOOK_CODE

    from tt_emu.boot import build_bare_machine
    from tt_emu.loader import load_upd
    from tt_emu.machine import MachineConfig

    fw = load_upd(str(ZC_PATH))
    machine = build_bare_machine(fw, MachineConfig(instructions_per_tick=20_000))
    pcs: list[int] = []
    machine.uc.hook_add(UC_HOOK_CODE, lambda uc, addr, size, ud: pcs.append(addr))
    result = machine.run(2_000_000)

    assert result.reason == "returned to entry sentinel"
    in_prog = [p for p in pcs if 0x0800_0000 <= p < 0x0810_0000]
    assert in_prog, "PROG never executed"
    assert min(in_prog) >= 0x0800_0000 and max(in_prog) < 0x0810_0000
    # No demand-paging MMU needed: the identity image runs without an unmapped
    # access (the run stops at the sentinel, not on a fault).
    assert len(set(pcs)) < 500  # the init leaf is short (~131 distinct PCs)
