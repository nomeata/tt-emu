#!/usr/bin/env python3
"""Test: with GPIO_IN pin0 released (=0), does standby descend past the wait loop
toward book mode? Watch the statechart leaves + GME/book landmark PCs, and inject
an OID tap once book-ish PCs appear.
"""
from __future__ import annotations

import sys

from tt_emu.boot import build_zc3201_machine
from tt_emu.loader import load_upd
from tt_emu.machine import MachineConfig
from tt_emu.firmware_profile import ZC3201

S = ZC3201.symbols
LANDMARKS = {
    0x08038E48: "state_init_power_on",
    0x0803EF7C: "standby_SM",
    0x0803E454: "standby_setrefresh",
    S["gme_parse_header"]: "gme_parse_header",
    S["gme_oid_to_playscript"]: "gme_oid_to_playscript",
    S["gme_exec_command"]: "gme_exec_command",
    S["gme_oid_tap_handler"]: "gme_oid_tap_handler",
    S["game_play_oid_voice"]: "game_play_oid_voice",
    S["play_chomp_voice"]: "play_chomp_voice",
    S["fs_open"]: "fs_open",
    0x0803629C: "FUN_0803629c",
}


def main() -> int:
    fw = load_upd(sys.argv[1])
    budget = int(sys.argv[2]) if len(sys.argv) > 2 else 80_000_000
    pin0 = 0 if len(sys.argv) <= 3 else int(sys.argv[3])
    m = build_zc3201_machine(fw, MachineConfig(instructions_per_tick=20_000))

    # Find the gpio peripheral and release pin0 (battery-OK comparator idle=released).
    gpio = None
    for p in m._peripherals:
        if type(p).__name__ == "GpioBlock":
            gpio = p
            break
    if gpio is not None and pin0 in (0, 1):
        gpio.set_input(0, pin0)
        print(f"forced GPIO_IN pin0 = {pin0}")

    counts: dict[int, int] = {}
    firsts: dict[int, int] = {}

    def mk(pc):
        def cb(mm):
            counts[pc] = counts.get(pc, 0) + 1
            if pc not in firsts:
                firsts[pc] = mm.clock
                print(f"[{mm.clock:>12}] FIRST {LANDMARKS[pc]} ({pc:#x})")
        return cb

    for pc in LANDMARKS:
        m.on_code(pc, mk(pc))

    audio = None
    for p in m._peripherals:
        if type(p).__name__ == "AudioDma":
            audio = p
            break

    res = m.run(budget)
    print(f"\nstop reason={res.reason} pc={res.pc:#x} insns={res.instructions}")
    if audio is not None:
        print(f"AUDIO: dac_submits={audio.dac_submits} completions={audio.completions} "
              f"flush={audio.flush_submits} unresolved={audio.unresolved_submits} "
              f"captured_chunks={len(audio.capture.chunks) if hasattr(audio.capture,'chunks') else '?'}")
    print("landmark hit counts:")
    for pc, name in LANDMARKS.items():
        if pc in counts:
            print(f"  {name:28s} {counts[pc]:>8d}  first@{firsts[pc]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
