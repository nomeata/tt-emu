#!/usr/bin/env python3
"""Log every fs_open path (UTF-16 or ASCII) across a ZC3201 boot to book mode,
optionally taps a product OID, and reports the codepage-type flag.

Oracle for the codepage-fix: with the codepage block-map correctly loaded the
firmware's UTF-16 path converter yields non-empty ASCII paths (e.g.
``A:/VOIMG/Chomp_Voice.bin``), and book-mode game discovery opens ``*.gme``.
"""
from __future__ import annotations
import sys
from pathlib import Path

from tt_emu.boot import build_zc3201_machine
from tt_emu.loader import load_upd
from tt_emu.machine import MachineConfig
from tt_emu.firmware_profile import ZC3201
from unicorn.arm_const import UC_ARM_REG_R0, UC_ARM_REG_LR

FS_OPEN = ZC3201.symbols["fs_open"]
CP_FLAG = 0x08007C4C


def anystr(m, addr, n=96):
    """Decode a path that may be UTF-16LE or ASCII."""
    b = m.read_bytes(addr, n)
    # Heuristic: UTF-16 if byte[1]==0 and byte[0] printable
    if len(b) >= 2 and b[1] == 0 and 32 <= b[0] < 127:
        out = []
        for i in range(0, n, 2):
            c = b[i] | (b[i + 1] << 8)
            if c == 0:
                break
            out.append(chr(c) if 32 <= c < 127 else f"<{c:x}>")
        return "u16:" + "".join(out)
    out = []
    for c in b:
        if c == 0:
            break
        out.append(chr(c) if 32 <= c < 127 else f"<{c:x}>")
    return "asc:" + "".join(out)


def main() -> int:
    upd = sys.argv[1] if len(sys.argv) > 1 else "/home/jojo/tiptoi/update.upd"
    budget = int(sys.argv[2]) if len(sys.argv) > 2 else 60_000_000
    fw = load_upd(upd)
    gme = Path("/home/jojo/tiptoi/firmware-re/tools/ttemu/content/B/example.gme").read_bytes()
    m = build_zc3201_machine(
        fw, MachineConfig(instructions_per_tick=20_000),
        b_files={"example.gme": gme},
    )
    opens: list[str] = []

    def on_fs_open(mm):
        r0 = mm.uc.reg_read(UC_ARM_REG_R0)
        lr = mm.uc.reg_read(UC_ARM_REG_LR)
        p = anystr(mm, r0)
        opens.append(p)
        print(f"[{mm.clock}] fs_open {p} (lr={lr:#x})")

    m.on_code(FS_OPEN, on_fs_open)
    res = m.run(budget)
    flag = m.read_bytes(CP_FLAG, 1)[0]
    print(f"--- stop {res.reason} pc={res.pc:#x} insns={res.instructions}")
    print(f"codepage-type flag *0x08007c4c = {flag}")
    gmes = [p for p in opens if ".gme" in p.lower()]
    empt = [p for p in opens if p in ("u16:", "asc:")]
    print(f"total fs_open={len(opens)}  .gme opens={len(gmes)}  empty paths={len(empt)}")
    for p in gmes:
        print("  GME:", p)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
