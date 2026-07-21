#!/usr/bin/env python3
"""Trace ZC3201 game discovery + mount: does the scan write studylist.lst with
example.gme, and does a product tap reach the mount + GME play?"""
from __future__ import annotations
import sys

from tt_emu.boot import build_zc3201_machine
from tt_emu.loader import load_upd
from tt_emu.machine import MachineConfig
from tt_emu.firmware_profile import ZC3201
from unicorn.arm_const import UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_LR

S = ZC3201.symbols
LAND = {
    0x080A178C: "discovery_scan_setup",
    0x080A1F9C: "lst_open_or_create",
    0x080A2224: "scan_gate",
    0x080A1F38: "scan_writer",
    0x0804DC38: "study_main",
    0x080297DC: "gme_mount_check_product",
    0x080299A0: "akoid_open_check",
    0x08029BD4: "product_switch_replay",
    S["gme_parse_header"]: "gme_parse_header",
    S["gme_oid_to_playscript"]: "gme_oid_to_playscript",
    S["gme_exec_command"]: "gme_exec_command",
    S["voice_load_and_play"]: "voice_load_and_play",
    S["game_play_oid_voice"]: "game_play_oid_voice",
}
FS_OPEN = S["fs_open"]


def anystr(m, addr, n=64):
    b = m.read_bytes(addr, n)
    if len(b) >= 2 and b[1] == 0 and 32 <= b[0] < 127:
        out = []
        for i in range(0, n, 2):
            c = b[i] | (b[i + 1] << 8)
            if c == 0:
                break
            out.append(chr(c) if 32 <= c < 127 else f"<{c:x}>")
        return "".join(out)
    out = []
    for c in b:
        if c == 0:
            break
        out.append(chr(c) if 32 <= c < 127 else ".")
    return "".join(out)


def main() -> int:
    fw = load_upd(sys.argv[1])
    gme = open("/home/jojo/tiptoi/firmware-re/tools/ttemu/content/B/example.gme", "rb").read()
    m = build_zc3201_machine(fw, MachineConfig(instructions_per_tick=20_000),
                             b_files={"example.gme": gme})
    counts: dict[int, int] = {}

    def mk(pc, name):
        def cb(mm):
            counts[pc] = counts.get(pc, 0) + 1
            if counts[pc] <= 3:
                extra = ""
                if pc == 0x080A178C:
                    extra = f" list_id={mm.uc.reg_read(UC_ARM_REG_R1)}"
                if pc == 0x080297DC:
                    extra = f" mode={mm.uc.reg_read(UC_ARM_REG_R0)}"
                if pc == 0x0804DC38:
                    extra = f" event={mm.uc.reg_read(UC_ARM_REG_R0):#x}"
                print(f"[{mm.clock:>12}] {name}{extra}")
        return cb
    for pc, name in LAND.items():
        m.on_code(pc, mk(pc, name))

    def on_fs_open(mm):
        p = anystr(mm, mm.uc.reg_read(UC_ARM_REG_R0))
        counts.setdefault("opens", []).append(p)
        if ".gme" in p.lower() or "studylist" in p.lower():
            print(f"[{mm.clock:>12}] fs_open '{p}'")
    m.on_code(FS_OPEN, on_fs_open)

    print("--- boot to book idle ---")
    m.run(40_000_000)
    for lbl, oid in [("product-1", 42), ("product-2", 42), ("content", 8065)]:
        print(f"--- tap {lbl} oid={oid} ---")
        m.oid.hold(oid)
        m.run(30_000_000)
        m.oid.lift()
        m.run(50_000_000)

    print("\n=== counts ===")
    for pc, name in LAND.items():
        if pc in counts:
            print(f"  {name:28s} {counts[pc]}")
    print(f"  scan_setup ran, opens total={len(counts.get('opens', []))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
