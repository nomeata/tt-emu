#!/usr/bin/env python3
"""ZC3201 full OID→GME-play probe: boot to book, tap product (mount), tap content.

Puts example.gme (product 42, scripted OIDs 8065-8067) on B: and drives the
authentic flow through the OID sensor. Traces the OID HAL, the book/standby
statechart dispatch, and the GME interpreter / voice-play sinks.
"""
from __future__ import annotations

import sys

from tt_emu.boot import build_zc3201_machine
from tt_emu.loader import load_upd
from tt_emu.machine import MachineConfig
from tt_emu.firmware_profile import ZC3201

S = ZC3201.symbols
LAND = {
    0x0804C164: "book_game_entry",
    0x08005F48: "oid_timer_cb",
    0x08005EEC: "oid_decode23",
    0x08005D80: "oid_shift_in",
    0x0803B710: "book_dispatch",
    0x0803DEAC: "standby_dispatch",
    0x0805CBD4: "game_reading_sm_dispatch",
    S["game_play_oid_voice"]: "game_play_oid_voice",
    S["gme_parse_header"]: "gme_parse_header",
    S["gme_oid_to_playscript"]: "gme_oid_to_playscript",
    S["gme_exec_command"]: "gme_exec_command",
    S["voice_load_and_play"]: "voice_load_and_play",
    S["voice_play_sample"]: "voice_play_sample",
    S["fs_open"]: "fs_open",
}


def main() -> int:
    fw = load_upd(sys.argv[1])
    gme = open("/home/jojo/tiptoi/firmware-re/tools/ttemu/content/B/example.gme", "rb").read()
    m = build_zc3201_machine(
        fw, MachineConfig(instructions_per_tick=20_000),
        b_files={"example.gme": gme},
    )
    counts: dict[int, int] = {}
    firsts: dict[int, int] = {}

    def mk(pc):
        def cb(mm):
            counts[pc] = counts.get(pc, 0) + 1
            if pc not in firsts:
                firsts[pc] = mm.clock
                print(f"[{mm.clock:>12}] FIRST {LAND[pc]} ({pc:#x})")
        return cb
    for pc in LAND:
        m.on_code(pc, mk(pc))

    def tap(oid, latch=30_000_000, settle=40_000_000, label=""):
        print(f"--- tap({oid}) {label} ---")
        base = m.oid.gameplay_frames_served
        m.oid.hold(oid)
        m.run(latch)
        m.oid.lift()
        m.run(settle)
        print(f"    gameplay_frames_served={m.oid.gameplay_frames_served} (was {base})")

    print("--- boot to book idle ---")
    r = m.run(40_000_000)
    print(f"boot stop={r.reason} pc={r.pc:#x} book_entry={counts.get(0x0804C164,0)}")

    tap(42, label="product/mount")
    tap(8065, label="content")

    print(f"\noid.taps_served={m.oid.taps_served} gameplay={m.oid.gameplay_frames_served}")
    print("landmark hit counts:")
    for pc, name in LAND.items():
        if pc in counts:
            print(f"  {name:28s} {counts[pc]:>8d}  first@{firsts.get(pc)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
