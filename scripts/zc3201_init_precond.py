#!/usr/bin/env python3
"""Instrument the ZC3201 INIT precondition wall (Leg 17).

state_init_power_on (0x08038e48) allocates FUN_0802b350(4) and checks it with
FUN_08008a04 (ptr in [0x08000000,0x08200000]). If it fails, INIT bails, mode
stays 8, standby powers off. Find what the allocator returns and why.
"""
from __future__ import annotations

import sys

from unicorn.arm_const import UC_ARM_REG_R0, UC_ARM_REG_LR, UC_ARM_REG_PC

from tt_emu.boot import build_zc3201_machine
from tt_emu.loader import load_upd
from tt_emu.machine import MachineConfig

STATE_INIT = 0x08038E48
ALLOC = 0x0802B350          # FUN_0802b350(size)
ALLOC_CORE = 0x0802B0D0     # FUN_0802b0d0 real allocator
PRECOND = 0x08008A04        # FUN_08008a04(ptr) range check
STANDBY = 0x0803EF7C
MODE8SET = 0x0803E384       # sets mode +0x1b = 8

# DAT pointer literals (addresses holding pointers)
DAT_08039090 = 0x08039090   # app/game context obj
DAT_08039094 = 0x08039094
DAT_0803e44c = 0x0803E44C
DAT_0802ab5c = 0x0802AB5C   # movable-mem manager descriptor ptr


def main() -> int:
    fw = load_upd(sys.argv[1])
    budget = int(sys.argv[2]) if len(sys.argv) > 2 else 40_000_000
    m = build_zc3201_machine(fw, MachineConfig(instructions_per_tick=20_000))

    st = {"init": 0, "alloc_calls": 0, "precond": [], "standby": 0, "mode8": 0,
          "last_alloc": None}

    def rd(addr):
        try:
            return int.from_bytes(m.read_bytes(addr, 4), "little")
        except Exception:
            return None

    st["in_init"] = False

    def on_init(mm):
        st["init"] += 1
        st["in_init"] = True
        if st["init"] <= 4:
            mgr = rd(DAT_0802ab5c)
            hdr = rd(mgr) if mgr else 0
            ctx = rd(DAT_08039090)
            mode = m.read_bytes(ctx + 0x1b, 1)[0] if ctx else None
            print(f"[init #{st['init']} @{mm.clock}] mgr_ptr={mgr:#x} "
                  f"hdr={hdr:#x} segcount={(hdr>>22) if hdr else '?'} "
                  f"ctx={ctx:#x} mode+0x1b={mode}")

    def on_alloc(mm):
        st["alloc_calls"] += 1

    def on_precond(mm):
        r0 = mm.uc.reg_read(UC_ARM_REG_R0)
        lr = mm.uc.reg_read(UC_ARM_REG_LR)
        ok = (r0 + 0xF8000000) & 0xFFFFFFFF < 0x200001
        st["last_alloc"] = r0
        # Only the call site inside state_init (LR lands right after its bl)
        if 0x08038E48 <= lr <= 0x08038F80:
            print(f"[precond IN state_init @{mm.clock}] ptr=r0={r0:#x} lr={lr:#x} in_range={ok}")

    def on_standby(mm):
        st["standby"] += 1
        if st["standby"] == 1:
            ctx = rd(DAT_0803e44c)
            mode = m.read_bytes(ctx + 0x1b, 1)[0] if ctx else None
            print(f"[STANDBY entry @{mm.clock}] ctx={ctx:#x} mode+0x1b={mode}")

    def on_mode8(mm):
        st["mode8"] += 1
        print(f"[MODE8SET @{mm.clock}] FUN_0803e384 sets mode=8 (call #{st['mode8']}) lr={mm.uc.reg_read(UC_ARM_REG_LR):#x}")

    m.on_code(STATE_INIT, on_init)
    m.on_code(ALLOC, on_alloc)
    m.on_code(PRECOND, on_precond)
    m.on_code(STANDBY, on_standby)
    m.on_code(MODE8SET, on_mode8)

    res = m.run(budget)
    print(f"\nstop reason={res.reason} pc={res.pc:#x} insns={res.instructions}")
    print(f"init hits={st['init']} alloc_calls={st['alloc_calls']} "
          f"standby_hits={st['standby']} mode8_sets={st['mode8']}")
    mgr = rd(DAT_0802ab5c)
    print(f"final mgr_ptr={mgr:#x} hdr={rd(mgr) if mgr else None}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
