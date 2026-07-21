#!/usr/bin/env python3
"""Trace the ZC3201 OID event: which events post, which state handler dispatches."""
from __future__ import annotations
import sys
from unicorn.arm_const import UC_ARM_REG_R0
from tt_emu.boot import build_zc3201_machine
from tt_emu.loader import load_upd
from tt_emu.machine import MachineConfig
from tt_emu.firmware_profile import ZC3201

AO_PTR = 0x08009708
RING_POST = 0x08003C44
SM_DISPATCH = 0x080096A8
DECODE23 = 0x08005EEC


def main() -> int:
    fw = load_upd(sys.argv[1])
    gme = open("/home/jojo/tiptoi/firmware-re/tools/ttemu/content/B/example.gme", "rb").read()
    m = build_zc3201_machine(fw, MachineConfig(instructions_per_tick=20_000),
                             b_files={"example.gme": gme})
    trace = {"on": False}
    posts: list = []
    handlers: dict[int, int] = {}

    def on_post(mm):
        ev = mm.uc.reg_read(UC_ARM_REG_R0)
        if trace["on"]:
            posts.append((mm.clock, ev))
            if len(posts) < 60:
                print(f"[{mm.clock:>12}] POST event={ev:#x}")
    def on_dispatch(mm):
        try:
            ao = mm.read_u32(AO_PTR)
            if 0x08000000 <= ao < 0x08440000:
                h = mm.read_u32(ao + 0xC)
                handlers[h] = handlers.get(h, 0) + 1
        except Exception:
            pass
    def on_decode(mm):
        if trace["on"]:
            oid_stored = None
            try:
                ctx = mm.read_u32(0x0800779C + 0x40)
                oid_stored = mm.read_u32(ctx + 8)
            except Exception:
                pass
            print(f"[{mm.clock:>12}] DECODE23 (captured -> ctx+8 later)")

    m.on_code(RING_POST, on_post)
    m.on_code(SM_DISPATCH, on_dispatch)
    m.on_code(DECODE23, on_decode)

    m.run(40_000_000)
    print("=== after boot, active state handlers seen (obj+0xc) ===")
    for h, c in sorted(handlers.items(), key=lambda x: -x[1])[:12]:
        print(f"   handler {h:#x}: {c}")
    handlers.clear()

    for oid, lbl in [(42, "product"), (8065, "content")]:
        print(f"\n########## tap({oid}) {lbl} ##########")
        trace["on"] = True
        base = m.oid.gameplay_frames_served
        m.oid.hold(oid)
        m.run(30_000_000)
        m.oid.lift()
        m.run(40_000_000)
        trace["on"] = False
        print(f"  captured={m.oid.gameplay_frames_served-base} "
              f"ctx+8={_ctx_oid(m):#x}")
        print("  state handlers dispatched during/after tap:")
        for h, c in sorted(handlers.items(), key=lambda x: -x[1])[:12]:
            print(f"     handler {h:#x}: {c}")
        handlers.clear()
    return 0


def _ctx_oid(m):
    try:
        ctx = m.read_u32(0x0800779C + 0x40)
        return m.read_u32(ctx + 8)
    except Exception:
        return -1


if __name__ == "__main__":
    raise SystemExit(main())
