#!/usr/bin/env python3
"""Capture the ZC3201 MtdLib mount's own printf diagnostics.

The MtdLib/FatLib services vtable is the global at ``0x081d9ad8``; slot ``+0x10``
is the ``logf`` used by the mount/format code (``nandmtd.c`` messages
``MtdLib15/40/41/43/47/49/58/59``, ``InitPlane``, ``NandPart``, …). We resolve
its target (``0x08008a48`` on this build) and format each message — the best
oracle for how the map-table build classifies each block.

Usage: zc3201_mtdlog.py <update.upd> [budget]
"""
from __future__ import annotations

import sys

from unicorn.arm_const import (UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2,
                               UC_ARM_REG_R3, UC_ARM_REG_SP)

from tt_emu.boot import build_zc3201_machine
from tt_emu.loader import load_upd
from tt_emu.machine import MachineConfig

PROG = "/home/jojo/tiptoi/tt-firmware-reveng/ZC3201/data/PROG.bin"
PBASE = 0x08008000
_pd = open(PROG, "rb").read() if __name__ == "__main__" else b""

#: The MtdLib services vtable global, and the logf slot (+0x10). Resolved from
#: the vtable at runtime; this is the value on the pinned build.
SERVICES_VTABLE = 0x081D9AD8
LOGF_SLOT = 0x10


def cstr(m, a: int) -> str:
    if PBASE <= a < PBASE + len(_pd):
        o = a - PBASE
        e = _pd.find(b"\0", o)
        return _pd[o:e].decode("latin1", "replace")
    out = b""
    for i in range(160):
        c = m.read_bytes(a + i, 1)
        if c == b"\0":
            break
        out += c
    return out.decode("latin1", "replace")


def main() -> int:
    fw = load_upd(sys.argv[1])
    budget = int(sys.argv[2]) if len(sys.argv) > 2 else 150_000_000
    m = build_zc3201_machine(fw, MachineConfig())

    lines: list[str] = []
    installed = {"pc": None}

    def logcb(mm):
        uc = mm.uc
        fmt = cstr(mm, uc.reg_read(UC_ARM_REG_R0))
        sp = uc.reg_read(UC_ARM_REG_SP)
        args = [uc.reg_read(UC_ARM_REG_R1), uc.reg_read(UC_ARM_REG_R2),
                uc.reg_read(UC_ARM_REG_R3)]
        args += [int.from_bytes(mm.read_bytes(sp + i * 4, 4), "little") for i in range(6)]
        n = fmt.count("%")
        try:
            out = fmt.replace("%d", "{}").replace("%x", "{:x}").replace("%p", "{:x}")
            out = out.format(*args[:n]) if n else fmt
        except Exception:
            out = f"{fmt!r} {[hex(x) for x in args[:n]]}"
        lines.append(out.rstrip("\r\n"))

    def install(mm):
        if installed["pc"] is not None:
            return
        vt = int.from_bytes(mm.read_bytes(SERVICES_VTABLE + LOGF_SLOT, 4), "little")
        if 0x08000000 <= vt < 0x08100000:
            installed["pc"] = vt
            m.on_code(vt, logcb)

    # The vtable slot is filled by mtd_helper_eb24 *inside* fs_storage_mount_init,
    # so resolve it at the map-table build (after setup), not at the mount entry.
    m.on_code(0x0802CBD8, install)
    m.on_code(0x0802EA54, install)

    stop = {"n": 0}
    def hang(mm):
        stop["n"] += 1
        if stop["n"] > 2:
            mm.uc.emu_stop()
    m.on_code(0x0802D208, hang)

    res = m.run(budget)
    print(f"stop {res.reason} pc={res.pc:#x} logf={installed['pc'] and hex(installed['pc'])}")
    print(f"--- {len(lines)} MtdLib log lines (last 80) ---")
    for ln in lines[-80:]:
        print(ln)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
