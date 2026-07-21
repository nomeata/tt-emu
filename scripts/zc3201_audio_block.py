#!/usr/bin/env python3
"""Instrument the ZC3201 voice-play block: does the chomp file open, does
voice_play_sample reach the done-setter, what does the AO poll, where does PC park?"""
from __future__ import annotations
import sys
from collections import Counter

from tt_emu.boot import build_zc3201_machine
from tt_emu.loader import load_upd
from tt_emu.machine import MachineConfig
from tt_emu.firmware_profile import ZC3201
from unicorn.arm_const import UC_ARM_REG_R0, UC_ARM_REG_LR, UC_ARM_REG_PC

S = ZC3201.symbols
PLAY_CHOMP = 0x0809F374
VOICE_PLAY = 0x0809F068
DONE_SETTER = 0x0809EFA4       # sets +1 |= 4
GETTER = 0x0809F57C
FS_OPEN = S["fs_open"]
VOICE_ERR = 0x0809F0A0  # inside voice_play_sample error path (approx) — skip
AO_LEAF = 0x0809EDA4
FUN_08008a04 = 0x08008A04


def main() -> int:
    fw = load_upd(sys.argv[1])
    budget = int(sys.argv[2]) if len(sys.argv) > 2 else 40_000_000
    m = build_zc3201_machine(fw, MachineConfig(instructions_per_tick=20_000))

    ev = []
    counts = Counter()

    def log(msg, mm):
        if len(ev) < 200:
            ev.append(f"[{mm.clock:>11}] {msg}")

    def on_play_chomp(mm):
        counts["play_chomp"] += 1
        if counts["play_chomp"] <= 3:
            log(f"play_chomp_voice arg={mm.uc.reg_read(UC_ARM_REG_R0):#x}", mm)

    def on_voice_play(mm):
        counts["voice_play"] += 1
        if counts["voice_play"] <= 3:
            r0 = mm.uc.reg_read(UC_ARM_REG_R0)
            log(f"voice_play_sample handle={r0:#x}", mm)

    def on_fs_open(mm):
        counts["fs_open"] += 1
        # r0 = path pointer
        r0 = mm.uc.reg_read(UC_ARM_REG_R0)
        try:
            path = mm.read_bytes(r0, 40).split(b"\x00")[0].decode("latin1")
        except Exception:
            path = "?"
        lr = mm.uc.reg_read(UC_ARM_REG_LR)
        if counts["fs_open"] <= 20:
            log(f"fs_open('{path}') from lr={lr:#x}", mm)

    def on_done_setter(mm):
        counts["done_setter"] += 1
        if counts["done_setter"] <= 5:
            log("FUN_0809efa4 done-setter entered", mm)

    def on_getter(mm):
        counts["getter"] += 1

    def on_ao_leaf(mm):
        counts["ao_leaf"] += 1

    m.on_code(PLAY_CHOMP, on_play_chomp)
    m.on_code(VOICE_PLAY, on_voice_play)
    m.on_code(FS_OPEN, on_fs_open)
    m.on_code(DONE_SETTER, on_done_setter)
    m.on_code(GETTER, on_getter)
    m.on_code(AO_LEAF, on_ao_leaf)

    # sample PC parking in the last stretch
    tail_pcs = Counter()
    def sampler(mm):
        if mm.clock > budget - 2_000_000:
            tail_pcs[mm.uc.reg_read(UC_ARM_REG_PC)] += 1
    # too expensive as code hook on all; instead sample via a periodic ping
    res = m.run(budget)

    print(f"stop reason={res.reason} pc={res.pc:#x} insns={res.instructions}")
    for e in ev:
        print(e)
    print("--- counts ---")
    for k, v in counts.items():
        print(f"  {k}: {v}")

    # read done flag
    dat = 0x0809ECEC
    try:
        ptr = int.from_bytes(m.read_bytes(dat, 4), "little")
        flag = m.read_bytes(ptr + 1, 1)[0]
        print(f"DAT_0809ecec -> {ptr:#x}, flag byte +1 = {flag:#x} (done bit2={bool(flag&4)})")
    except Exception as e:
        print("flag read err", e)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
