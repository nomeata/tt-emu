#!/usr/bin/env python3
"""Dump the ZC3201 map-table scan structure: partition bounds + block range."""
from __future__ import annotations
import sys
from collections import Counter
from unicorn.arm_const import (UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2,
                               UC_ARM_REG_R3)
from tt_emu.boot import build_zc3201_machine
from tt_emu.loader import load_upd
from tt_emu.machine import MachineConfig


def u32(m, a):
    return int.from_bytes(m.read_bytes(a, 4), "little")
def u16(m, a):
    return int.from_bytes(m.read_bytes(a, 2), "little")


def main() -> int:
    fw = load_upd(sys.argv[1])
    budget = int(sys.argv[2]) if len(sys.argv) > 2 else 40_000_000
    m = build_zc3201_machine(fw, MachineConfig())
    log: list[str] = []
    rs_blocks: Counter[tuple[int, int]] = Counter()

    def ea54(mm):
        r0 = mm.uc.reg_read(UC_ARM_REG_R0)
        if r0 and len(log) < 40:
            log.append(f"ea54 param_1={r0:#x} nparts=*+8={u32(mm,r0+8) if r0>0x1000 else '?'} "
                       f"+0x14={u32(mm,r0+0x14)} +0x18={u32(mm,r0+0x18)} +0x1c={u32(mm,r0+0x1c)}")
    def e5e0(mm):
        uc = mm.uc
        mapo = uc.reg_read(UC_ARM_REG_R0); part = uc.reg_read(UC_ARM_REG_R1)
        parr = u32(mm, mapo + 0x28)  # param_1[10]
        if 0x08000000 <= parr < 0x08400000:
            rec = parr + part * 0x18
            log.append(f"e5e0 part={part} rec={rec:#x} hi(+0x14)={u16(mm,rec+0x14)} "
                       f"lo(+0x16)={u16(mm,rec+0x16)} cnt(+4)={u16(mm,rec+4)} +6={u16(mm,rec+6)}")
    def rs(mm):
        uc = mm.uc
        rs_blocks.update([(uc.reg_read(UC_ARM_REG_R1), uc.reg_read(UC_ARM_REG_R2))])

    m.on_code(0x0802EA54, ea54)
    m.on_code(0x0802E5E0, e5e0)
    m.on_code(0x08030224, rs)

    stop = {"n": 0}
    def hang(mm):
        stop["n"] += 1
        if stop["n"] > 2:
            mm.uc.emu_stop()
    m.on_code(0x0802D208, hang)

    res = m.run(budget)
    print(f"stop {res.reason} pc={res.pc:#x}")
    for ln in log[:40]:
        print(" ", ln)
    print("readspare (part, block) counts, most common:",
          [((p, b), n) for (p, b), n in rs_blocks.most_common(15)])
    print("distinct blocks scanned:", sorted(set(b for (p, b) in rs_blocks))[:40])
    print("max block:", max((b for (p, b) in rs_blocks), default=None))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
