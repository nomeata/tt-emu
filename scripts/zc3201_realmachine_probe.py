#!/usr/bin/env python3
"""ZC3201 real-machine boot probe.

Runs the 1st-gen firmware from its real boot-task entry through tt-emu's REAL
Machine with the reused 2N-MT SoC core peripherals (SysCon/IntcTimer/GpioBlock/
BatteryAdc) at the identical 0x040000xx addresses, identity-mapped (no MMU), via
:func:`tt_emu.boot.build_zc3201_machine`. Reports how far the unmodified firmware
runs (PROG PC span / distinct PCs / milestones) and the hottest loops.

Step 2/3 of docs/zc3201-boot-feasibility.md. Hook-free — only observes.

Usage:
    zc3201_realmachine_probe.py <update.upd> [max_insns]
"""
from __future__ import annotations

import logging
import sys
from collections import Counter

from unicorn import UC_HOOK_CODE

from tt_emu.boot import build_zc3201_machine
from tt_emu.firmware_profile import ZC3201
from tt_emu.loader import load_upd
from tt_emu.machine import MachineConfig

#: Boot-path milestones (firmware-absolute) to report if reached.
MILESTONES = {
    0x080236C0: "app_init_main",
    0x08022A8C: "mtd_extra_bitmap",
    0x080250E0: "fs_storage_mount_init",
    0x0802B45C: "nand_disk_mount",
    0x080040EC: "fs_open",
    0x08001544: "event_post",
    0x080016A8: "sm_dispatch_hierarchy",
    0x08030E48: "state_init_power_on(leaf)",
    0x08036454: "state_stdb_standby",
}


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    fw = load_upd(sys.argv[1])
    budget = int(sys.argv[2]) if len(sys.argv) > 2 else 40_000_000

    m = build_zc3201_machine(fw, MachineConfig())
    counts: Counter[str] = Counter()
    hot: Counter[int] = Counter()
    for addr, name in MILESTONES.items():
        m.on_code(addr, (lambda nm: lambda _mm: counts.update((nm,)))(name))
    m.uc.hook_add(UC_HOOK_CODE, lambda _uc, a, _s, _u: hot.update((a,)))

    print(f"entry {ZC3201.prog_entry:#010x} (boot_task_main), budget {budget}")
    res = m.run(budget)
    prog = [a for a in hot if 0x08000000 <= a < 0x08100000]
    print(f"stop: {res.reason}  clock={res.instructions}  pc={res.pc:#010x}")
    if prog:
        print(f"PROG distinct={len(prog)} span={min(prog):#010x}..{max(prog):#010x}")
    print("milestones reached:")
    for name in MILESTONES.values():
        if counts.get(name):
            print(f"  {name}: {counts[name]}")
    print("hottest PCs:", [(hex(a), n) for a, n in hot.most_common(6)])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
