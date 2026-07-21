#!/usr/bin/env python3
"""Trace the ZC3201 mount's heap: malloc/free around the 0x80fa400 region.

The services vtable (0x081d9ad8) slot 0 = malloc, slot 1 = free, slot 4 = logf.
We resolve them at runtime, hook them, and log every alloc/free, flagging any
that touch the region [0x80fa380, 0x80fa480].

Leg 15 conclusion: the allocator is a plain UPWARD bump allocator handing out
CONTIGUOUS, non-overlapping blocks (whole-disk-map malloc(0x60)=0x80fa3c0, then
MtdLib manager malloc(0x28c)=0x80fa420). The Leg-14 "heap collision" was NOT an
allocator bug — it was a nandboot bulk page-read overrunning the 512-byte
map-read buffer (0x80fa000) by reading the large-page sub-page count (4) instead
of the 512-byte-page count (1); fixed by FirmwareProfile.nandboot_geom_seed. This
probe is the diagnostic that established the allocator is sound.
"""
from __future__ import annotations

import sys

from unicorn.arm_const import (UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_LR,
                               UC_ARM_REG_PC)

from tt_emu.boot import build_zc3201_machine
from tt_emu.loader import load_upd
from tt_emu.machine import MachineConfig

VTABLE = 0x081D9AD8
LO, HI = 0x80FA380, 0x80FA480  # collision watch region


def main() -> int:
    fw = load_upd(sys.argv[1])
    budget = int(sys.argv[2]) if len(sys.argv) > 2 else 150_000_000
    m = build_zc3201_machine(fw, MachineConfig())

    state = {"malloc": None, "free": None, "installed": False, "seq": 0}
    events: list[str] = []
    pend = {}  # lr -> (seq, size)

    def on_malloc(mm):
        uc = mm.uc
        size = uc.reg_read(UC_ARM_REG_R0)
        lr = uc.reg_read(UC_ARM_REG_LR)
        state["seq"] += 1
        pend.setdefault(lr, []).append((state["seq"], size))
        # install a return hook at lr
        if lr not in ret_hooked:
            ret_hooked.add(lr)
            mm.on_code(lr, on_ret)

    def on_ret(mm):
        uc = mm.uc
        lr = uc.reg_read(UC_ARM_REG_PC)
        if lr not in pend or not pend[lr]:
            return
        seq, size = pend[lr].pop(0)
        ptr = uc.reg_read(UC_ARM_REG_R0)
        flag = ""
        if ptr and (LO <= ptr < HI or LO <= ptr + size < HI or (ptr <= LO and ptr + size >= HI)):
            flag = "  <<< COLLISION REGION"
        events.append(f"#{seq:4d} malloc(0x{size:x}) = 0x{ptr:08x} (ret->0x{lr:08x}){flag}")

    def on_free(mm):
        uc = mm.uc
        ptr = uc.reg_read(UC_ARM_REG_R0)
        lr = uc.reg_read(UC_ARM_REG_LR)
        state["seq"] += 1
        flag = "  <<< COLLISION REGION" if ptr and LO <= ptr < HI else ""
        events.append(f"#{state['seq']:4d} free(0x{ptr:08x}) (ret->0x{lr:08x}){flag}")

    ret_hooked = set()

    def install(mm):
        if state["installed"]:
            return
        slots = [int.from_bytes(mm.read_bytes(VTABLE + 4 * i, 4), "little") for i in range(6)]
        malloc, free = slots[0], slots[1]
        if not (0x08000000 <= malloc < 0x08100000):
            return
        state["installed"] = True
        state["malloc"], state["free"] = malloc, free
        print("vtable slots:", [hex(s) for s in slots])
        mm.on_code(malloc, on_malloc)
        mm.on_code(free, on_free)

    # stop at the divide-by-zero divisor function so we see the last allocs
    def at_div(mm):
        mm.uc.emu_stop()
    m.on_code(0x0800C974, at_div)

    def on_chunk(mm):
        install(mm)

    res = m.run(budget, on_chunk=on_chunk)
    print(f"stop {res.reason} pc={res.pc:#x} malloc={state['malloc'] and hex(state['malloc'])} free={state['free'] and hex(state['free'])}")
    print(f"--- {len(events)} heap events (all) ---")
    for e in events:
        print(e)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
