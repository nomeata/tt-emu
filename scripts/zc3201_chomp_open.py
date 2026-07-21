#!/usr/bin/env python3
"""Trace the chomp-voice fs_open: wide path + return value; does voice_play_sample run?"""
from __future__ import annotations
import sys
from tt_emu.boot import build_zc3201_machine
from tt_emu.loader import load_upd
from tt_emu.machine import MachineConfig
from tt_emu.firmware_profile import ZC3201
from unicorn.arm_const import UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_LR

S = ZC3201.symbols
FS_OPEN = S["fs_open"]
PLAY_CHOMP = 0x0809F374
VOICE_PLAY = 0x0809F068
CHOMP_LR = 0x0809F2BC  # the fs_open call site inside play_chomp_voice

def wstr(m, addr, n=64):
    b = m.read_bytes(addr, n)
    out = []
    for i in range(0, n, 2):
        c = b[i] | (b[i+1] << 8)
        if c == 0:
            break
        out.append(chr(c) if 32 <= c < 127 else f"<{c:x}>")
    return "".join(out)

def main() -> int:
    fw = load_upd(sys.argv[1])
    budget = int(sys.argv[2]) if len(sys.argv) > 2 else 30_000_000
    m = build_zc3201_machine(fw, MachineConfig(instructions_per_tick=20_000))
    st = {"chomp_seen": False, "pending_ret": None}

    def on_play_chomp(mm):
        print(f"[{mm.clock}] play_chomp_voice(arg={mm.uc.reg_read(UC_ARM_REG_R0):#x})")
        st["chomp_seen"] = True

    def on_fs_open(mm):
        r0 = mm.uc.reg_read(UC_ARM_REG_R0)
        lr = mm.uc.reg_read(UC_ARM_REG_LR)
        if st["chomp_seen"] and lr == CHOMP_LR:
            print(f"[{mm.clock}] CHOMP fs_open path='{wstr(mm, r0)}' (r0={r0:#x}) lr={lr:#x}")
            st["pending_ret"] = lr

    def on_ret(mm):
        if st["pending_ret"] is not None:
            print(f"[{mm.clock}] CHOMP fs_open RETURNED r0={mm.uc.reg_read(UC_ARM_REG_R0):#x}")
            st["pending_ret"] = None

    def on_voice_play(mm):
        print(f"[{mm.clock}] *** voice_play_sample ENTERED "
              f"handle={mm.uc.reg_read(UC_ARM_REG_R0):#x} off={mm.uc.reg_read(UC_ARM_REG_R1):#x} "
              f"size={mm.uc.reg_read(UC_ARM_REG_R2):#x}")

    m.on_code(PLAY_CHOMP, on_play_chomp)
    m.on_code(FS_OPEN, on_fs_open)
    m.on_code(CHOMP_LR, on_ret)
    m.on_code(VOICE_PLAY, on_voice_play)
    res = m.run(budget)
    print(f"stop {res.reason} pc={res.pc:#x} insns={res.instructions}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
