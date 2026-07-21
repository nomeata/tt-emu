#!/usr/bin/env python3
"""Instrument codepage_recover (0x08025b7c) + FUN_08025b04 (page read) to see which
codepage offsets the path converters request and whether the reads succeed."""
from __future__ import annotations
import sys
from pathlib import Path

from tt_emu.boot import build_zc3201_machine
from tt_emu.loader import load_upd
from tt_emu.machine import MachineConfig
from tt_emu.firmware_profile import ZC3201
from unicorn.arm_const import UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_LR

FS_OPEN = ZC3201.symbols["fs_open"]
CP_RECOVER = 0x08025B7C
CP_B04 = 0x08025B04


def main() -> int:
    upd = sys.argv[1] if len(sys.argv) > 1 else "/home/jojo/tiptoi/update.upd"
    budget = int(sys.argv[2]) if len(sys.argv) > 2 else 80_000_000
    fw = load_upd(upd)
    gme = Path("/home/jojo/tiptoi/firmware-re/tools/ttemu/content/B/example.gme").read_bytes()
    m = build_zc3201_machine(fw, MachineConfig(instructions_per_tick=20_000),
                             b_files={"example.gme": gme})
    st = {"recover": [], "reads": [], "active": False}

    def on_recover(mm):
        off = mm.uc.reg_read(UC_ARM_REG_R0)
        st["recover"].append((mm.clock, off))
        if st["active"]:
            blk = off >> 14
            print(f"  [{mm.clock}] codepage_recover off={off:#x} block={blk}")

    def on_b04(mm):
        origin = mm.uc.reg_read(UC_ARM_REG_R0)
        page = mm.uc.reg_read(UC_ARM_REG_R1)
        if st["active"]:
            print(f"  [{mm.clock}]   FUN_08025b04 origin={origin} page_in_blk={page} "
                  f"-> abspage={32*origin+page}")

    def on_fs_open(mm):
        r0 = mm.uc.reg_read(UC_ARM_REG_R0)
        lr = mm.uc.reg_read(UC_ARM_REG_LR)
        b = mm.read_bytes(r0, 4)
        empty = (b[0] == 0)
        # Turn on detailed tracing shortly before known empty-path sites
        print(f"[{mm.clock}] fs_open lr={lr:#x} first4={b.hex()} {'EMPTY' if empty else ''}")

    m.on_code(CP_RECOVER, on_recover)
    m.on_code(CP_B04, on_b04)
    m.on_code(FS_OPEN, on_fs_open)

    # Enable detailed recover tracing for a window around the chomp conversion
    def enable_trace(mm):
        st["active"] = True

    def disable_trace(mm):
        st["active"] = False

    # play_chomp_voice entry / exit bracket
    m.on_code(0x0809F374, enable_trace)
    m.on_code(0x0809F2C0, disable_trace)

    res = m.run(budget)
    print(f"--- stop {res.reason} pc={res.pc:#x} insns={res.instructions}")
    offs = [o for _, o in st["recover"]]
    if offs:
        print(f"codepage_recover calls={len(offs)} max_off={max(offs):#x} "
              f"max_block={max(offs)>>14}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
