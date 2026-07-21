#!/usr/bin/env python3
"""Instrument FUN_08030108 (bad-block check, dev+0x40) + func_0x080006fc scan."""
from __future__ import annotations
import sys
from unicorn import UC_HOOK_CODE
from unicorn.arm_const import (UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2,
                               UC_ARM_REG_R3, UC_ARM_REG_LR, UC_ARM_REG_PC)
from tt_emu.boot import build_zc3201_machine
from tt_emu.loader import load_upd
from tt_emu.machine import MachineConfig

fw = load_upd(sys.argv[1])
m = build_zc3201_machine(fw, MachineConfig())
uc = m.uc

STATE_PTR = 0x081d97d8
log = []
calls = {"108": 0, "6fc": 0, "28b0": 0}
MAX = 12

def rd(a, n=4):
    return int.from_bytes(uc.mem_read(a, n), "little")

def h108(mm):
    calls["108"] += 1
    if calls["108"] <= MAX:
        r0, r1, r2 = uc.reg_read(UC_ARM_REG_R0), uc.reg_read(UC_ARM_REG_R1), uc.reg_read(UC_ARM_REG_R2)
        state = rd(STATE_PTR, 1)
        bmp = rd(STATE_PTR + 4)
        log.append(f"[108 #{calls['108']}] dev={r0:#x} blk={r1} pg={r2} state@{STATE_PTR:#x}={state} bmp={bmp:#x}")

def h108_ret(tag):
    def cb(mm):
        if calls["108"] <= MAX:
            log.append(f"    -> {tag} (r0={uc.reg_read(UC_ARM_REG_R0)})")
    return cb

def h6fc(mm):
    calls["6fc"] += 1
    if calls["6fc"] <= MAX:
        log.append(f"  [6fc #{calls['6fc']}] r0={uc.reg_read(UC_ARM_REG_R0):#x} r1={uc.reg_read(UC_ARM_REG_R1):#x} r2={uc.reg_read(UC_ARM_REG_R2)} lr={uc.reg_read(UC_ARM_REG_LR):#x}")

def h28b0(mm):
    calls["28b0"] += 1
    if calls["6fc"] <= 3 and calls["28b0"] <= 40:
        log.append(f"    [28b0 #{calls['28b0']}] r0={uc.reg_read(UC_ARM_REG_R0):#x} r1={uc.reg_read(UC_ARM_REG_R1):#x} lr={uc.reg_read(UC_ARM_REG_LR):#x}")

def h28b0_ret(mm):
    # at 0x080028b0 return: we hook the caller-side is hard; instead hook a known ret path
    pass

m.on_code(0x08030108, h108)
m.on_code(0x080301a8, h108_ret("BAD/return1"))
m.on_code(0x080301b0, h108_ret("GOOD/return0"))
m.on_code(0x080006fc, h6fc)
m.on_code(0x080028b0, h28b0)

m.run(20_000_000)
print("\n".join(log[:120]))
print("calls:", calls)
