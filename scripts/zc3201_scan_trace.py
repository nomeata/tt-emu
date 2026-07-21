#!/usr/bin/env python3
"""Trace the ZC3201 map-table scan: which (row,col) does readspare read, and
what 4-byte tag does the classifier see there?

Monkeypatches NfcController._data_read_smallpage to record every read's
(row, col, data[0:4], oob[0:4]) and correlates with spare_tag_scan entries.
"""
from __future__ import annotations

import sys
from collections import Counter

from tt_emu.boot import build_zc3201_machine
from tt_emu.loader import load_upd
from tt_emu.machine import MachineConfig
from tt_emu.peripherals import nand as nandmod


def main() -> int:
    fw = load_upd(sys.argv[1])
    budget = int(sys.argv[2]) if len(sys.argv) > 2 else 60_000_000
    m = build_zc3201_machine(fw, MachineConfig())

    nfc = None
    for p in m._peripherals:
        if isinstance(p, nandmod.NfcController):
            nfc = p
            break
    assert nfc is not None

    reads: list[tuple[int, int, bytes]] = []
    orig = nfc._data_read_smallpage

    def traced():
        row, col = nfc._row, nfc._col
        orig()
        # window[0:4] is what readspare returns as the tag
        data0 = m.read_bytes(nfc.sram_window, 4)
        reads.append((row, col, bytes(data0)))

    nfc._data_read_smallpage = traced

    # stop shortly after entering the hang loop
    hang = {"n": 0}
    def hang_cb(mm):
        hang["n"] += 1
        if hang["n"] > 5:
            mm.uc.emu_stop()
    m.on_code(0x0802D208, hang_cb)

    res = m.run(budget)
    print(f"stop {res.reason} pc={res.pc:#x} reads={len(reads)}")

    # Which rows were read, and the tag values seen
    rowcnt = Counter(r for r, c, d in reads)
    print(f"distinct rows read: {len(rowcnt)}")
    print("first 40 reads (row, col, data[0:4] LE):")
    for row, col, d in reads[:40]:
        val = int.from_bytes(d, "little")
        blk, pg = divmod(row, 32)
        print(f"  row={row:5d} (blk{blk} pg{pg}) col={col:4d} data0={val:#010x}")
    print("most common rows:", [(r, n) for r, n in rowcnt.most_common(12)])
    # unique (row,col) pairs
    pairs = Counter((r, c) for r, c, d in reads)
    print("distinct (row,col):", len(pairs))
    print("sample (row,col) low rows:",
          sorted(set((r, c) for r, c, d in reads if r < 2048))[:20])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
