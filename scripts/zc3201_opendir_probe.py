#!/usr/bin/env python3
"""Wall-2 probe: with the stack lowered so the discovery scan builds a correct
"B:/" root, trace File_Open / File_OpenId / the isdir gate to see exactly why
opendir("B:/") resolves to a non-directory (attr != 0x10)."""
from __future__ import annotations

import dataclasses
import sys

from tt_emu.boot import build_zc3201_machine
from tt_emu.loader import load_upd
from tt_emu.machine import MachineConfig
from tt_emu.firmware_profile import ZC3201
from unicorn.arm_const import (
    UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3,
    UC_ARM_REG_SP, UC_ARM_REG_LR,
)

FILE_OPEN = 0x080A8E04
FILE_OPENID = 0x080A8C74
ISDIR = 0x080A8A8C          # FUN_080a8a8c: dir check
ROOT_DIRENT = 0x080B370C    # FUN_080b370c: create root dirent
OPENDIR = 0x080AB860


def wstr(m, addr, n=32):
    if not addr:
        return "<null>"
    b = m.read_bytes(addr, n * 2)
    out = []
    for i in range(0, len(b), 2):
        c = b[i] | (b[i + 1] << 8)
        if c == 0:
            break
        out.append(chr(c) if 32 <= c < 127 else f"<{c:x}>")
    return "".join(out)


def main() -> int:
    fw = load_upd(sys.argv[1])
    stack = int(sys.argv[2], 0) if len(sys.argv) > 2 else 0x081D0000
    gme = open("/home/jojo/tiptoi/firmware-re/tools/ttemu/content/B/example.gme", "rb").read()
    prof = dataclasses.replace(ZC3201, svc_stack_top=stack)
    m = build_zc3201_machine(fw, MachineConfig(instructions_per_tick=20_000),
                             profile=prof, b_files={"example.gme": gme})
    R = lambda r: m.uc.reg_read(r)
    state = {"in_open": 0, "opendir_seen": 0}

    def on_opendir(mm):
        p = R(UC_ARM_REG_R0)
        s = wstr(mm, p)
        if "B" in s and state["opendir_seen"] < 6:
            state["opendir_seen"] += 1
            state["trace"] = True
            print(f"\n[{mm.clock}] opendir('{s}') sp={R(UC_ARM_REG_SP):#x}")

    def on_file_open(mm):
        if not state.get("trace"):
            return
        r0, r1, r2, r3 = R(UC_ARM_REG_R0), R(UC_ARM_REG_R1), R(UC_ARM_REG_R2), R(UC_ARM_REG_R3)
        # mode object at r1: +0x10 = wide string ptr, +0xc = flags
        wptr = mm.read_u32(r1 + 0x10) if r1 else 0
        flags = mm.read_u32(r1 + 0xc) if r1 else 0
        print(f"  File_Open(path={r0:#x} mode_obj={r1:#x} r2={r2:#x} r3={r3:#x}) "
              f"wstr='{wstr(mm, wptr)}' flags={flags:#x}")
        state["fo_lr"] = R(UC_ARM_REG_LR)

    def on_file_openid(mm):
        if not state.get("trace"):
            return
        print(f"    File_OpenId(id={R(UC_ARM_REG_R0):#x} cluster={R(UC_ARM_REG_R1):#x})")

    def on_root_dirent(mm):
        if not state.get("trace"):
            return
        print(f"    FUN_080b370c(id={R(UC_ARM_REG_R0):#x} cluster={R(UC_ARM_REG_R1):#x}) "
              f"[creates root dirent]")

    def on_isdir(mm):
        if not state.get("trace"):
            return
        fh = R(UC_ARM_REG_R0)
        magic = mm.read_u32(fh + 0x1c) if fh else 0
        dirent = mm.read_u32(fh + 0x20) if fh else 0
        b3b = mm.read_u8(dirent + 0x3b) if dirent else -1
        attr = mm.read_u32(dirent + 0x3c) if dirent else -1
        print(f"    isdir(fh={fh:#x}) magic={magic:#x} dirent={dirent:#x} "
              f"+0x3b={b3b} +0x3c(attr)={attr:#x}")

    m.on_code(OPENDIR, on_opendir)
    m.on_code(FILE_OPEN, on_file_open)
    m.on_code(FILE_OPENID, on_file_openid)
    m.on_code(ROOT_DIRENT, on_root_dirent)
    m.on_code(ISDIR, on_isdir)

    print(f"--- boot with stack {stack:#x} ---")
    m.run(40_000_000)
    print(f"\ndone; opendir B seen={state['opendir_seen']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
