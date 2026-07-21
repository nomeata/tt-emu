#!/usr/bin/env python3
"""Pin the OOB byte offset the MtdLib map tag lives at.

Places a distinctive 16-byte ramp OOB on the pages the scan reads, wires the
GPIO-3 spare surface, and logs what 4-byte value the readspare classifier
(FUN_0802edb8) actually receives (r0 at nandboot 0x08002d04 `str r0,[r7]`).
"""
from __future__ import annotations
import sys
from collections import Counter
from unicorn.arm_const import UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R7

from tt_emu.boot import build_zc3201_machine
from tt_emu.loader import load_upd
from tt_emu.machine import MachineConfig
from tt_emu.peripherals import nand as nandmod


def main() -> int:
    fw = load_upd(sys.argv[1])
    budget = int(sys.argv[2]) if len(sys.argv) > 2 else 30_000_000
    m = build_zc3201_machine(fw, MachineConfig())

    nfc = next(p for p in m._peripherals if isinstance(p, nandmod.NfcController))
    # Overwrite every existing tag with a ramp 0xB0..0xBF so we can read the offset
    ramp = bytes(range(0xB0, 0xC0))
    for row in list(nfc.flash._tags):
        nfc.flash.set_tag(row, ramp)
    # also set a ramp on the rows the scan reads (page0 of blocks 0..300)
    for blk in range(0, 300):
        nfc.flash.set_tag(blk * 32, ramp)

    seen: Counter[int] = Counter()
    def tag_read(mm):
        r0 = mm.uc.reg_read(UC_ARM_REG_R0)
        seen.update((r0 & 0xFFFFFFFF,))
    m.on_code(0x08002D04, tag_read)  # str r0,[r7] : r0 = tag word

    hang = {"n": 0}
    def hang_cb(mm):
        hang["n"] += 1
        if hang["n"] > 3:
            mm.uc.emu_stop()
    m.on_code(0x0802D208, hang_cb)

    res = m.run(budget)
    print(f"stop {res.reason} pc={res.pc:#x}")
    print("tag words seen at post-strobe read:", [(hex(v), n) for v, n in seen.most_common(10)])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
