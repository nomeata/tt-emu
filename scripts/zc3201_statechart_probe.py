#!/usr/bin/env python3
"""Observe the ZC3201 statechart live: the AO object, its current-state leaf
handler pointer, event dispatch, and where the pump parks.

ZC3201's QHsm active object pointer is the RAM global at ``0x08009708``; the
current-state leaf handler is the function pointer at ``obj+0xc`` (dispatched by
``sm_dispatch_hierarchy`` 0x080096a8, which fetches an event via ``obj+8`` and
calls ``*(obj+0xc)``). This probe polls that leaf pointer and reports the
sequence of statechart leaves the unmodified firmware visits, plus a PC
histogram of where it spends time after INIT.

Usage: zc3201_statechart_probe.py <update.upd> [budget]
"""
from __future__ import annotations

import collections
import sys

from tt_emu.boot import build_zc3201_machine
from tt_emu.loader import load_upd
from tt_emu.machine import MachineConfig

AO_PTR_GLOBAL = 0x08009708  # RAM global -> AO object
LEAF_OFF = 0xC              # obj+0xc = current-state handler function pointer

# Known statechart leaf handlers (runtime addresses, ZC3201/out/decomp names)
LEAF_NAMES = {
    0x08038E48: "state_init_power_on",
    0x0803E454: "state_stdb_setrefresh",
    0x0803EF7C: "state_stdb_standby(SM)",   # FUN_08036f7c reveng + 0x8000
    0x0803629C: "mtd/gme_oid_dispatch?",
}

DISPATCH = 0x080096A8      # sm_dispatch_hierarchy
INIT_LEAF = 0x08038E48
STANDBY_SM = 0x0803EF7C


def main() -> int:
    fw = load_upd(sys.argv[1])
    budget = int(sys.argv[2]) if len(sys.argv) > 2 else 300_000_000
    m = build_zc3201_machine(fw, MachineConfig(instructions_per_tick=20_000))

    state = {"init_at": None, "leaves": [], "last_leaf": None,
             "dispatches": 0, "init_hits": 0}

    def read_leaf() -> int | None:
        try:
            ao = int.from_bytes(m.read_bytes(AO_PTR_GLOBAL, 4), "little")
            if not (0x08000000 <= ao < 0x08440000):
                return None
            leaf = int.from_bytes(m.read_bytes(ao + LEAF_OFF, 4), "little")
            return leaf
        except Exception:
            return None

    def on_dispatch(mm):
        state["dispatches"] += 1
        leaf = read_leaf()
        if leaf != state["last_leaf"]:
            state["last_leaf"] = leaf
            name = LEAF_NAMES.get(leaf, f"{leaf:#x}" if leaf else "None")
            state["leaves"].append((mm.clock, name))

    def on_init(mm):
        state["init_hits"] += 1
        if state["init_at"] is None:
            state["init_at"] = mm.clock

    m.on_code(DISPATCH, on_dispatch)
    m.on_code(INIT_LEAF, on_init)

    # PC histogram of the last stretch (sampled via a coarse block hook is too
    # slow; instead read the terminal PC and rely on the leaf log).
    res = m.run(budget)

    print(f"stop reason={res.reason} pc={res.pc:#x} insns={res.instructions}")
    print(f"AO ptr @0x08009708 = {read_leaf.__self__ if False else ''}")
    ao = int.from_bytes(m.read_bytes(AO_PTR_GLOBAL, 4), "little")
    print(f"AO object = {ao:#x}")
    print(f"sm_dispatch_hierarchy calls: {state['dispatches']}")
    print(f"state_init_power_on hits: {state['init_hits']} (first @ {state['init_at']})")
    print(f"--- statechart leaf sequence (via obj+0xc at each dispatch) ---")
    for clock, name in state["leaves"]:
        print(f"  [{clock:>12}] leaf = {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
