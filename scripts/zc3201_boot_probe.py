#!/usr/bin/env python3
"""ZC3201 (1st-gen tiptoi) boot-feasibility probe.

tt-emu currently models the 2nd-gen "MT" pen (ZC3202N / AK1050) and boots its real
firmware authentically (see ``docs/memory-map-and-boot.md``). This script is a probe for
extending that to the **1st-generation ZC3201** firmware — see
``docs/zc3201-boot-feasibility.md`` for the write-up.

It loads the real ZC3201 PROG + nandboot with tt-emu's own ANYKA106 loader, maps a plain
identity memory space (ZC3201's PROG lives at 0x08000000, 1 MiB — unlike the MT's
0x08009000), swallows MMIO, and runs from the ``state_init_power_on`` entry, reporting how
far the firmware executes before it needs state the (skipped) early-boot stages would have
produced. It does NOT hook or patch firmware behaviour — it only observes.

Usage:
    scripts/zc3201_boot_probe.py path/to/update.upd
    # the ZC3201 update.upd: https://cdn.ravensburger.de/db/firmware/update%20encrypt%20normal%20freq.upd
    #                        sha256 03c12f41b6bc9ab78ee206c2bdfdbe45eb9ad7c3ab8337e7a8661555aae4a4a6
"""
from __future__ import annotations

import sys

from unicorn import (
    UC_ARCH_ARM,
    UC_HOOK_CODE,
    UC_HOOK_MEM_FETCH_UNMAPPED,
    UC_HOOK_MEM_READ,
    UC_HOOK_MEM_READ_UNMAPPED,
    UC_HOOK_MEM_WRITE,
    UC_HOOK_MEM_WRITE_UNMAPPED,
    UC_MODE_ARM,
    UC_MODE_LITTLE_ENDIAN,
    Uc,
    UcError,
)
from unicorn.arm_const import UC_ARM_REG_LR, UC_ARM_REG_PC, UC_ARM_REG_SP, UC_CPU_ARM_926

from tt_emu.loader import load_upd

#: ZC3201 firmware addresses (from the tt-firmware-reveng ZC3201 analysis / FINDINGS).
PROG_LOAD = 0x08000000  # identity-mapped, 1 MiB (the MT loads at 0x08009000 and needs an MMU)
NANDBOOT_LOAD = 0x07FF8000
ENTRY = 0x08030E48  # state_init_power_on
LR_SENTINEL = 0x00002000


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    fw = load_upd(sys.argv[1])
    print(f"loaded {fw.path.name}: build {fw.build_id}, boot gen {fw.boot_generation}, "
          f"PROG {fw.prog.size:#x}, nandboot {fw.nandboot.size:#x}")
    if fw.boot_generation != "ANYKANB0":
        print(f"warning: expected ANYKANB0 (ZC3201), got {fw.boot_generation}")

    uc = Uc(UC_ARCH_ARM, UC_MODE_ARM | UC_MODE_LITTLE_ENDIAN)
    uc.ctl_set_cpu_model(UC_CPU_ARM_926)
    for base, size in [
        (0x00000000, 0x10000),   # low / mask-ROM area (zero stub)
        (0x07FF0000, 0x10000),   # resident HAL / boot-SRAM window
        (0x08000000, 0x400000),  # main RAM + PROG (identity)
        (0x08400000, 0x40000),   # stack headroom
        (0x04000000, 0x200000),  # MMIO window
    ]:
        uc.mem_map(base, size)
    uc.mem_write(PROG_LOAD, fw.prog.data)
    uc.mem_write(NANDBOOT_LOAD, fw.nandboot.data)
    # Observe only: swallow MMIO (reads 0), and refuse to auto-map faults so a real
    # unmapped access is reported rather than papered over.
    uc.hook_add(UC_HOOK_MEM_READ | UC_HOOK_MEM_WRITE, lambda *a: True, None, 0x04000000, 0x04200000)

    pcs: list[int] = []
    uc.hook_add(UC_HOOK_CODE, lambda uc, addr, sz, ud: pcs.append(addr))
    wedge: list = []
    uc.hook_add(
        UC_HOOK_MEM_READ_UNMAPPED | UC_HOOK_MEM_WRITE_UNMAPPED | UC_HOOK_MEM_FETCH_UNMAPPED,
        lambda uc, acc, addr, sz, val, ud: (wedge.append((acc, addr, uc.reg_read(UC_ARM_REG_PC))), False)[1],
    )

    uc.reg_write(UC_ARM_REG_SP, 0x08420000)
    uc.reg_write(UC_ARM_REG_LR, LR_SENTINEL)
    try:
        uc.emu_start(ENTRY, LR_SENTINEL, count=2_000_000)
        outcome = f"returned to LR sentinel after {len(pcs)} instructions"
    except UcError as e:
        outcome = f"UcError {e} at PC={uc.reg_read(UC_ARM_REG_PC):#010x} after {len(pcs)} instructions"

    inprog = [p for p in pcs if PROG_LOAD <= p < PROG_LOAD + 0x100000]
    print(f"entry {ENTRY:#010x}: {outcome}")
    if wedge:
        acc, addr, pc = wedge[0]
        print(f"first unmapped access: acc={acc} addr={addr:#010x} pc={pc:#010x}")
    else:
        print("no unmapped access — identity-mapped PROG runs without a demand-paging MMU")
    if inprog:
        print(f"PROG execution span: {min(inprog):#010x}..{max(inprog):#010x} "
              f"({len(set(inprog))} distinct addresses)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
