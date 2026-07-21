#!/usr/bin/env python3
"""Log distinct MMIO writes/reads on the audio bands during the ZC3201 voice play
(after play_chomp_voice) to locate the DAC DMA submit registers + completion poll.
"""
from __future__ import annotations

import sys
from collections import Counter

from unicorn import UC_HOOK_MEM_WRITE, UC_HOOK_MEM_READ

from tt_emu.boot import build_zc3201_machine
from tt_emu.loader import load_upd
from tt_emu.machine import MachineConfig

PLAY_CHOMP = 0x0809F374


def main() -> int:
    fw = load_upd(sys.argv[1])
    budget = int(sys.argv[2]) if len(sys.argv) > 2 else 60_000_000
    m = build_zc3201_machine(fw, MachineConfig(instructions_per_tick=20_000))

    active = {"on": False}
    writes: Counter = Counter()
    reads: Counter = Counter()
    wval: dict[int, int] = {}

    def in_band(a):
        return (0x04000000 <= a < 0x04100000) or (0x08080000 <= a < 0x08090000)

    def on_w(uc, access, addr, size, value, ud):
        if active["on"] and in_band(addr):
            writes[addr] += 1
            wval[addr] = value

    def on_r(uc, access, addr, size, value, ud):
        if active["on"] and in_band(addr):
            reads[addr] += 1

    m.uc.hook_add(UC_HOOK_MEM_WRITE, on_w, begin=0x04000000, end=0x040FFFFF)
    m.uc.hook_add(UC_HOOK_MEM_WRITE, on_w, begin=0x08080000, end=0x0808FFFF)
    m.uc.hook_add(UC_HOOK_MEM_READ, on_r, begin=0x04000000, end=0x040FFFFF)
    m.uc.hook_add(UC_HOOK_MEM_READ, on_r, begin=0x08080000, end=0x0808FFFF)

    def on_chomp(mm):
        active["on"] = True

    m.on_code(PLAY_CHOMP, on_chomp)
    res = m.run(budget)
    print(f"stop {res.reason} pc={res.pc:#x} insns={res.instructions} active={active['on']}")
    print("--- WRITES (addr: count  lastval) ---")
    for addr, cnt in sorted(writes.items()):
        print(f"  {addr:#010x}: {cnt:>7d}  last={wval[addr]:#010x}")
    print("--- READS (addr: count) top 20 ---")
    for addr, cnt in sorted(reads.items(), key=lambda kv: -kv[1])[:20]:
        print(f"  {addr:#010x}: {cnt:>7d}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
