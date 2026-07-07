"""End-to-end test: the emulator runs an embedded **main-binary** GME.

A main-binary GME (``tests/data/main_binary/minimal_mb.gme``) carries a native
ARM blob at header offset 0xA8. Unlike a play-script GME (``test_session.py``),
the firmware here *loads and executes* that blob: on mounting the product the
statechart descends book(13) → state 69 (``gme_separate_binary``) → state 67,
whose handler (``gme_oid_dispatch_alt``) calls ``gme_launch_binary_build_sysapi``
@0x080AA934, which reads the blob into the load region **0x08132000**, builds
the ~90-entry ``system_api`` struct, and jumps into it with ``r0 = &system_api``
(``docs/firmware-2n-mt.md`` §8).

The firmware runs **unmodified** — no hooks; the test only attaches read-only
PC watchpoints (via ``run_session(on_prepared=...)``) and reads the word trail
the blob leaves at 0x08141F00, proving purely by observation that:

* the firmware reached the main-binary loader/launcher and the load address,
* the blob's own code ran (``MARK[0] == 0xDEADBEEF``),
* a ``system_api`` call reached the firmware and returned (``MARK[2]/[3]``),
* the audio ``play_sound`` slot was invoked from inside the blob.

Skipped unless the firmware ``.upd`` is present.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tt_emu.boot import BootedMachine
from tt_emu.machine import Machine
from tt_emu.runner import gme_product_code, run_session

UPD_PATH = Path("/home/jojo/tiptoi/update3202MT.upd")
GME_PATH = Path(__file__).parent / "data" / "main_binary" / "minimal_mb.gme"

# --- Firmware addresses on the load/launch path (firmware-2n-mt.md §8) --------------
READ_MAIN_BINARY = 0x080A_ADD8  # gme_read_main_binary_table (reads hdr@0xA8)
LAUNCH_SYSAPI = 0x080A_A934  # gme_launch_binary_build_sysapi (loads blob + jumps)
BINARY_LOAD_ADDR = 0x0813_2000  # gme_alloc_binary_region region base — the blob entry
IS_AUDIO_PLAYING = 0x0800_B024  # system_api +0x0c
PLAY_MEDIA = 0x080A_B7B4  # system_api +0x2c (play_sound)

#: The blob's word trail (main.c), inside the loader's reserved 64 KiB region.
MARK_ADDR = 0x0814_1F00
STATE_SEPARATE_BINARY = 69

pytestmark = pytest.mark.skipif(
    not UPD_PATH.exists(), reason="firmware .upd not present"
)


def test_main_binary_gme_loads_and_executes() -> None:
    gme = GME_PATH.read_bytes()
    product = gme_product_code(gme)

    hits: dict[int, int] = {}
    marks: dict[int, list[int]] = {}

    def watch(addr: int):
        def hook(_m: Machine) -> None:
            hits[addr] = hits.get(addr, 0) + 1

        return hook

    def on_entry_stop(m: Machine) -> None:
        # First hit = blob entered; on the second the first main() has fully run
        # (all four markers written), so snapshot them and stop — keeping the
        # test fast (~60 M instructions) while the game would otherwise re-launch
        # the blob every event-pump cycle forever.
        hits[BINARY_LOAD_ADDR] = hits.get(BINARY_LOAD_ADDR, 0) + 1
        if hits[BINARY_LOAD_ADDR] >= 2:
            marks["v"] = [m.read_u32(MARK_ADDR + 4 * i) for i in range(4)]
            m.request_stop("embedded main binary executed")

    def on_prepared(booted: BootedMachine) -> None:
        for addr in (READ_MAIN_BINARY, LAUNCH_SYSAPI, IS_AUDIO_PLAYING, PLAY_MEDIA):
            booted.machine.on_code(addr, watch(addr))
        booted.machine.on_code(BINARY_LOAD_ADDR, on_entry_stop)

    report = run_session(
        str(UPD_PATH),
        [product],  # a single product tap; the blob then launches autonomously
        flag_resume=True,
        max_instructions=200_000_000,
        b_files={GME_PATH.name: gme},
        on_prepared=on_prepared,
    )

    # The product mounted and the statechart reached the main-binary state.
    assert report.mounted_product == product, report.format_log()
    leaves = [state for _, state in report.state_chain]
    assert STATE_SEPARATE_BINARY in leaves, leaves

    # The firmware walked the whole load/launch path and jumped into the blob.
    assert hits.get(READ_MAIN_BINARY, 0) >= 1, "loader never read hdr@0xA8"
    assert hits.get(LAUNCH_SYSAPI, 0) >= 1, "launcher never built the system_api"
    assert hits.get(BINARY_LOAD_ADDR, 0) >= 1, "PC never reached the load address"

    # The blob's own code ran and made system_api calls into the firmware.
    assert "v" in marks, "the embedded main() never completed a full pass"
    m0, m1, m2, m3 = marks["v"]
    assert m0 == 0xDEADBEEF, f"entry marker {m0:#x}"  # blob code executed
    assert m1 in (0, 1), f"is_audio_playing() return {m1:#x}"  # sysapi returned
    assert m2 == 0x5A5A0001, f"post-call marker {m2:#x}"  # returned from the call
    assert m3 == 0x5A5A0002, f"post-play marker {m3:#x}"  # returned from play_sound

    # play_sound (the audio slot) was invoked from inside the blob.
    assert hits.get(PLAY_MEDIA, 0) >= 1, "the blob never called play_sound"
    assert hits.get(IS_AUDIO_PLAYING, 0) >= 1, "the blob never called is_audio_playing"
