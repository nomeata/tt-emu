#!/usr/bin/env python3
"""DIAGNOSTIC: force FUN_08030108 (bad-block check) to return 0=good, observe
whether the readspare runs, the map-table build succeeds, and the mount proceeds.
This is a throwaway probe patch to validate the lever before the authentic fix."""
from __future__ import annotations
import sys
from collections import Counter
from unicorn.arm_const import (UC_ARM_REG_R0, UC_ARM_REG_LR, UC_ARM_REG_PC,
                               UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3)
from tt_emu.boot import build_zc3201_machine
from tt_emu.loader import load_upd
from tt_emu.machine import MachineConfig

fw = load_upd(sys.argv[1])
gme = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2].endswith(".gme") else None
b_files = {gme.split("/")[-1]: open(gme, "rb").read()} if gme else None
m = build_zc3201_machine(fw, MachineConfig(), b_files=b_files)
uc = m.uc

counts = Counter()
rsp = []

def force_good(mm):
    counts["108"] += 1
    uc.reg_write(UC_ARM_REG_R0, 0)
    uc.reg_write(UC_ARM_REG_PC, uc.reg_read(UC_ARM_REG_LR))

def readspare(mm):
    counts["readspare"] += 1
    if len(rsp) < 30:
        rsp.append((uc.reg_read(UC_ARM_REG_R1), uc.reg_read(UC_ARM_REG_R2), uc.reg_read(UC_ARM_REG_R3)))

NAMES = {
    0x0802CBD8: "map_table_build",
    0x0802EDB8: "spare_tag_scan",
    0x08030224: "readspare_leaf",
    0x0803345C: "nand_disk_mount",
    0x0800C0EC: "fs_open",
    0x0802C09C: "fat_mount_A",
    0x0802C35C: "fat_mount_B",
    0x0802D208: "HANG_mount_fail",
}
def mk(n):
    def cb(mm):
        counts[n] += 1
    return cb

m.on_code(0x08030108, force_good)
m.on_code(0x08030224, readspare)
for a, n in NAMES.items():
    m.on_code(a, mk(n))

res = m.run(80_000_000)
print(f"stop: {res.reason} clock={res.instructions} pc={res.pc:#010x}")
for k in ["108","readspare","map_table_build","spare_tag_scan","readspare_leaf",
          "nand_disk_mount","fat_mount_A","fat_mount_B","fs_open","HANG_mount_fail"]:
    if counts.get(k):
        print(f"  {k}: {counts[k]}")
print("first readspare (blk,pg,col):", rsp[:12])
