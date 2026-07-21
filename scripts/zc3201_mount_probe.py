#!/usr/bin/env python3
"""ZC3201 mount probe: does the hand-built small-page NAND image mount A:/B:?

Boots the unmodified 1st-gen firmware via :func:`tt_emu.boot.build_zc3201_machine`
(now serving the hand-built ``build_zc3201_nand_image`` through the small-page
``NfcController``) and instruments the mount path so we can see exactly how far the
firmware's own ``fs_storage_mount_init`` gets and where it diverges.

Usage: zc3201_mount_probe.py <update.upd> [game.gme] [max_insns]
"""
from __future__ import annotations

import logging
import sys
from collections import Counter

from unicorn import UC_HOOK_CODE
from unicorn.arm_const import UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R3, UC_ARM_REG_R4

from tt_emu.boot import build_zc3201_machine
from tt_emu.loader import load_upd
from tt_emu.machine import MachineConfig

# Runtime addresses (PROG base 0x08008000) from the ZC3201 decomp / RE.
MOUNT = {
    0x0802B6C0: "app_init_main",
    0x0802D4A8: "pre_mount_bitmap_alloc",
    0x0802D0E0: "fs_storage_mount_init",
    0x0802CCF0: "superblock_partition_parse",
    0x0800C208: "map_read(last_page)",
    0x0802F0C0: "whole_disk_map_build",
    0x0802CBD8: "map_table_build",
    0x0802EDB8: "spare_tag_scan",
    0x08030224: "readspare_leaf",
    0x0803345C: "nand_disk_mount",
    0x0800C0EC: "fs_open",
    0x0802AA8C: "mtd_extra_bitmap",
}
# Infinite-loop "give up" PCs inside fs_storage_mount_init (return 0xffffffff / hang).
HANG_PCS = {0x0802D280: "mount_fail(LAB_0802d280)"}


def main() -> int:
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    fw = load_upd(sys.argv[1])
    gme = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2].endswith(".gme") else None
    budget = int(sys.argv[-1]) if sys.argv[-1].isdigit() else 60_000_000

    b_files = {gme.split("/")[-1]: open(gme, "rb").read()} if gme else None
    m = build_zc3201_machine(fw, MachineConfig(), b_files=b_files)

    counts: Counter[str] = Counter()
    hot: Counter[int] = Counter()
    log_lines: list[str] = []

    def hook(name: str):
        def cb(mm):
            counts.update((name,))
            uc = mm.uc
            if counts[name] <= 3:  # log the first few calls with key args
                if name == "map_read(last_page)":
                    log_lines.append(f"  map_read: row={uc.reg_read(UC_ARM_REG_R3)} "
                                     f"buf={uc.reg_read(UC_ARM_REG_R0):#x}")
                elif name == "superblock_partition_parse":
                    log_lines.append(f"  sb_parse(id={uc.reg_read(UC_ARM_REG_R0)})")
                elif name == "whole_disk_map_build":
                    log_lines.append(f"  map_build: reserve={uc.reg_read(UC_ARM_REG_R1)} "
                                     f"span={uc.reg_read(UC_ARM_REG_R0)}")
                elif name == "readspare_leaf":
                    log_lines.append(f"  readspare(blk={uc.reg_read(UC_ARM_REG_R0)},"
                                     f"pg={uc.reg_read(UC_ARM_REG_R1)})")
        return cb

    for addr, name in MOUNT.items():
        m.on_code(addr, hook(name))
    for addr, name in HANG_PCS.items():
        m.on_code(addr, hook(name))
    m.uc.hook_add(UC_HOOK_CODE, lambda _uc, a, _s, _u: hot.update((a,)))

    print(f"entry {0x0802B8BC:#010x} budget {budget}  gme={gme}")
    res = m.run(budget)
    prog = [a for a in hot if 0x08000000 <= a < 0x08100000]
    print(f"stop: {res.reason}  clock={res.instructions}  pc={res.pc:#010x}")
    if prog:
        print(f"PROG distinct={len(prog)} span={min(prog):#010x}..{max(prog):#010x}")
    print("mount milestones:")
    for name in list(MOUNT.values()) + list(HANG_PCS.values()):
        if counts.get(name):
            print(f"  {name}: {counts[name]}")
    for line in log_lines[:24]:
        print(line)
    print("hottest PCs:", [(hex(a), n) for a, n in hot.most_common(6)])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
