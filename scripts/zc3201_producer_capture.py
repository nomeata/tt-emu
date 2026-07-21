#!/usr/bin/env python3
"""Drive the ZC3201 producer's format+erase+write protocol and capture the NAND.

Builds on scripts/zc3201_producer_probe.py (which cracked cmd 2 -> cmd 3 -> cmd 5
transc_format). This leg (Leg 8, see docs/zc3201-producer-addresses.md §11)
recovers the REAL command semantics and the on-media write seam:

  * cmd 4 ``transc_erase`` (worker 0x080015f8): arg0 -> {u32 start, u32 end}
    (an 8-byte struct memcpy'd from the pointer, NOT two ring words). It fires the
    gNand erase leaf **desc+0x38 = 0x08005d44** as ``leaf(dev, 0, block)`` across
    the whole chip (2043 blocks for [0,2048)). PROVEN here — the full-chip erase IS
    captured into the WritableNand.
  * cmd 7 ``transc_data`` (worker 0x08001aa8 -> 0x0800849c): arg0 -> a **0x18-byte**
    descriptor {u32 dataLen; u32 ?; char name[16]} memcpy'd into 0x08020a9c. It
    LOOKS UP ``name`` in the producer's file-records table (global 0x08020888,
    record count at 0x0802088c, 0x24-byte records at 0x08020898; the search is the
    strcmp 0x08007b40 driven by 0x08007bc8) and streams the matching file's bytes
    to NAND through the **medium object 0x08027060** (method table built by cmd 5;
    write via [0x08027060+8]). The writes reach the gNand write leaf desc+0x2c
    (0x08005b14) — but ONLY once the named file's records+data are present in the
    table. With an empty table cmd 7 finds nothing and writes nothing (0 leaves).
  * cmd 10 = ``transc_update_self`` (not an iterate/format).

REMAINING WALL: the file-records table (0x08020888) + file data must be UPLOADED by
another host command before transc_data can commit it. Reversing that upload
command (and the medium write path 0x08027060) is the next step — see §11.

Run: .venv/bin/python scripts/zc3201_producer_capture.py
"""
import struct
import sys

from unicorn import (
    UC_ARCH_ARM, UC_HOOK_CODE, UC_HOOK_MEM_FETCH_UNMAPPED, UC_HOOK_MEM_READ,
    UC_HOOK_MEM_READ_UNMAPPED, UC_HOOK_MEM_WRITE, UC_HOOK_MEM_WRITE_UNMAPPED,
    UC_MEM_FETCH, UC_MEM_FETCH_PROT, UC_MEM_FETCH_UNMAPPED, UC_MODE_ARM,
    UC_MODE_LITTLE_ENDIAN, Uc, UcError,
)
from unicorn.arm_const import (
    UC_ARM_REG_CPSR, UC_ARM_REG_LR, UC_ARM_REG_PC, UC_ARM_REG_R0, UC_ARM_REG_R1,
    UC_ARM_REG_R2, UC_ARM_REG_R3, UC_ARM_REG_SP,
)

PROD = "/home/jojo/tiptoi/tt-firmware-reveng/ZC3201/data/producer.bin"
UPD = "/home/jojo/tiptoi/tt-firmware-reveng/ZC3201/data/update.upd"

BASE = 0x08000000
USB_LOOP = 0x080034A4
DISPATCH = 0x08003664
SVC_SP = 0x0802B000
PRINTF = 0x080003DC
MALLOC_W = 0x080004D4
FREE_W = 0x080004E0
GEOM_PTR = 0x0801569C
DEV_OBJ = 0x08024B50
NFC_READID_REG = 0x0404A150
SOC_CHIPID_REG = 0x04000000

# small-page geometry (K9F5608)
PAGE = 512
SPARE = 16
PAGES_BLK = 32
NBLOCKS = 2048
BLK = PAGE * PAGES_BLK

# NAND method-vtable leaves (offset in device desc -> leaf addr).
LEAVES = {
    0x28: 0x08005C1C, 0x2C: 0x08005B14, 0x34: 0x08005BE0, 0x38: 0x08005D44,
    0x3C: 0x08005DB0, 0x40: 0x08005A04, 0x44: 0x08005AF8, 0x30: 0x08005CD8,
}
LEAF_NAME = {v: ("desc+%#x" % k) for k, v in LEAVES.items()}

CHIP_ID = 0xBDA575EC
SOC_CHIP_ID = 0x33323931


class WritableNand:
    def __init__(self):
        self.data = {}
        self.spare = {}
        self.log = []

    def _blk(self, b):
        d = self.data.get(b)
        if d is None:
            d = bytearray(b"\xff" * BLK)
            self.data[b] = d
        return d

    def erase(self, b):
        self.data[b] = bytearray(b"\xff" * BLK)
        for pg in range(PAGES_BLK):
            self.spare.pop((b, pg), None)
        self.log.append(("erase", b, -1, 0))

    def program(self, b, pg, data, tag):
        d = self._blk(b)
        data = (bytes(data) + b"\xff" * PAGE)[:PAGE]
        d[pg * PAGE:pg * PAGE + PAGE] = data
        if tag:
            self.spare[(b, pg)] = bytes(tag)
        self.log.append(("prog", b, pg, len(tag) if tag else 0))


def main():
    prod = open(PROD, "rb").read()
    upd = open(UPD, "rb").read()
    nand = WritableNand()
    uc = Uc(UC_ARCH_ARM, UC_MODE_ARM | UC_MODE_LITTLE_ENDIAN)
    uc.mem_map(BASE, 0x0040_0000)
    uc.mem_map(0x0900_0000, 0x0800_0000)
    uc.mem_write(BASE, prod)
    uc.mem_map(0x0400_0000, 0x0020_0000)

    leaf_log = []  # (leafaddr, r0,r1,r2,r3, sp0,sp1,sp2,sp3, LR)

    def rd(a, n=4):
        return int.from_bytes(uc.mem_read(a, n), "little")

    def hook_mmio_r(uc, acc, addr, sz, val, ud):
        b0 = addr & ~3
        if addr == NFC_READID_REG:
            uc.mem_write(b0, struct.pack("<I", CHIP_ID)); return True
        if b0 == SOC_CHIPID_REG:
            uc.mem_write(b0, struct.pack("<I", SOC_CHIP_ID)); return True
        if (0x0407_0000 <= addr < 0x0407_2000 or addr == 0x0401_0010
                or 0x0404_A000 <= addr < 0x0404_C000
                or 0x0405_B000 <= addr < 0x0405_C000):
            uc.mem_write(b0, struct.pack("<I", 0xFFFFFFFF)); return True
        uc.mem_write(b0, struct.pack("<I", 0)); return True

    uc.hook_add(UC_HOOK_MEM_READ, hook_mmio_r, None, 0x0400_0000, 0x0420_0000)
    uc.hook_add(UC_HOOK_MEM_WRITE, lambda *a: True, None, 0x0400_0000, 0x0420_0000)

    def on_unmapped(uc, acc, addr, sz, val, ud):
        if acc in (UC_MEM_FETCH, UC_MEM_FETCH_UNMAPPED, UC_MEM_FETCH_PROT):
            print("  [FETCH-FAULT] addr=%#x PC=%#x LR=%#x" %
                  (addr, uc.reg_read(UC_ARM_REG_PC), uc.reg_read(UC_ARM_REG_LR)))
            return False
        try:
            uc.mem_map(addr & ~0xFFFF, 0x10000); return True
        except UcError:
            return False

    uc.hook_add(UC_HOOK_MEM_READ_UNMAPPED | UC_HOOK_MEM_WRITE_UNMAPPED
                | UC_HOOK_MEM_FETCH_UNMAPPED, on_unmapped)

    def ret(v):
        uc.reg_write(UC_ARM_REG_R0, v & 0xFFFFFFFF)
        uc.reg_write(UC_ARM_REG_PC, uc.reg_read(UC_ARM_REG_LR) & ~1)

    heap = [0x0900_0000]

    def do_malloc(uc, a, s, ud):
        n = (uc.reg_read(UC_ARM_REG_R0) + 0x1F) & ~0x1F
        p = heap[0]; heap[0] += n
        uc.mem_write(p, b"\x00" * n); ret(p)

    def read_cstr(p, mx=256):
        out = b""
        while len(out) < mx:
            c = uc.mem_read(p + len(out), 1)[0]
            if c == 0:
                break
            out += bytes([c])
        return out.decode("latin1", "replace")

    def do_printf(uc, a, s, ud):
        import re
        fmt = read_cstr(uc.reg_read(UC_ARM_REG_R0))
        args = [uc.reg_read(r) for r in (UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3)]
        args += [rd(uc.reg_read(UC_ARM_REG_SP) + i * 4) for i in range(6)]
        it = iter(args)

        def sub(m):
            v = next(it); c = m.group(0)[-1]
            if c in "xX": return hex(v)
            if c in "du": return str(v)
            if c == "s": return read_cstr(v)
            if c == "c": return chr(v & 0xFF)
            return m.group(0)

        try:
            out = re.sub(r"%[0-9.\-+ lh#]*[xXdusc%]", sub, fmt)
        except Exception:
            out = fmt
        print("  [pr]", out.rstrip()); ret(0)

    uc.hook_add(UC_HOOK_CODE, do_malloc, None, MALLOC_W, MALLOC_W)
    uc.hook_add(UC_HOOK_CODE, lambda *a: ret(0), None, FREE_W, FREE_W)
    uc.hook_add(UC_HOOK_CODE, do_printf, None, PRINTF, PRINTF)

    # --- leaf capture: log convention + write into WritableNand ---
    def mk_leaf(addr, off):
        def cb(uc, a, sz, ud):
            r = [uc.reg_read(x) for x in (UC_ARM_REG_R0, UC_ARM_REG_R1,
                                          UC_ARM_REG_R2, UC_ARM_REG_R3)]
            sp = uc.reg_read(UC_ARM_REG_SP)
            st = [rd(sp + i * 4) for i in range(4)]
            lr = uc.reg_read(UC_ARM_REG_LR)
            leaf_log.append((off, r, st, lr))
            # Capture into the WritableNand. erase leaf desc+0x38: r2 = block.
            # (write leaf desc+0x2c calling convention validated once a data write
            # fires — see module docstring / §11; args logged above meanwhile.)
            if off == 0x38:
                if r[2] < NBLOCKS:
                    nand.erase(r[2])
        return cb

    for off, ad in LEAVES.items():
        uc.hook_add(UC_HOOK_CODE, mk_leaf(ad, off), None, ad, ad)

    uc.reg_write(UC_ARM_REG_CPSR, 0x13 | 0xC0)
    uc.reg_write(UC_ARM_REG_SP, SVC_SP)
    print("== startup ==")
    try:
        uc.emu_start(BASE, USB_LOOP, count=30_000_000)
    except UcError as e:
        print("STARTUP FAULT %s PC=%#x" % (e, uc.reg_read(UC_ARM_REG_PC)))
    print("reached loop=%s" % (uc.reg_read(UC_ARM_REG_PC) == USB_LOOP))

    SENTINEL = 0x0838_0000
    uc.mem_write(SENTINEL, struct.pack("<I", 0xFFFFFFFF))
    PKT = 0x0805_0000
    ARGBUF = 0x0805_2000

    def call_cmd(cmd, arg0_ptr=0, arg1=0, budget=120_000_000, label=""):
        pkt = struct.pack("<IIHH", cmd, arg0_ptr, arg1 & 0xFFFF, 0)
        uc.mem_write(PKT, pkt.ljust(64, b"\x00"))
        uc.reg_write(UC_ARM_REG_R0, PKT)
        uc.reg_write(UC_ARM_REG_R1, 0)
        uc.reg_write(UC_ARM_REG_SP, SVC_SP)
        uc.reg_write(UC_ARM_REG_LR, SENTINEL)
        uc.reg_write(UC_ARM_REG_CPSR, 0x13 | 0xC0)
        n0 = len(leaf_log)
        try:
            uc.emu_start(DISPATCH, SENTINEL, count=budget)
            pc = uc.reg_read(UC_ARM_REG_PC)
            print("[cmd %d %s] -> %s r0=%#x  leaves+%d" %
                  (cmd, label, "RET" if pc == SENTINEL else "PC=%#x" % pc,
                   uc.reg_read(UC_ARM_REG_R0), len(leaf_log) - n0))
        except UcError as e:
            print("[cmd %d %s] FAULT %s PC=%#x" %
                  (cmd, label, e, uc.reg_read(UC_ARM_REG_PC)))

    blob = bytearray(upd[0x200:0x240].ljust(287, b"\x00"))
    assert struct.unpack_from("<I", blob, 0)[0] == CHIP_ID

    call_cmd(2, label="get_chip_id")
    # cmd 3 arg0 -> chip-param blob
    uc.mem_write(ARGBUF, bytes(blob))
    call_cmd(3, arg0_ptr=ARGBUF, label="set_chip_param")
    dev = rd(DEV_OBJ)
    print("  dev=%#x geom=%#x" % (dev, rd(GEOM_PTR)))
    call_cmd(5, label="format", budget=200_000_000)
    print("  after cmd5: leaf calls so far=%d" % len(leaf_log))

    # cmd 4 transc_erase: arg0 -> {u32 start, u32 end}
    ERASE_ARGS = 0x0805_3000
    uc.mem_write(ERASE_ARGS, struct.pack("<II", 0, NBLOCKS))
    call_cmd(4, arg0_ptr=ERASE_ARGS, label="erase[0,2048]", budget=400_000_000)

    # cmd 7 transc_data: arg0 -> 0x18-byte {u32 dataLen; u32 ?; char name[16]}
    DATA_ARGS = 0x0805_3100
    uc.mem_write(DATA_ARGS,
                 struct.pack("<II", 0x100, 0) + b"PROG".ljust(16, b"\x00"))
    call_cmd(7, arg0_ptr=DATA_ARGS, label="data:PROG", budget=400_000_000)

    print("\n== leaf-call convention (first 40) ==")
    for off, r, st, lr in leaf_log[:40]:
        print("  desc+%#x  r0=%#x r1=%#x r2=%#x r3=%#x  sp=[%#x %#x %#x %#x] LR=%#x"
              % (off, r[0], r[1], r[2], r[3], st[0], st[1], st[2], st[3], lr))
    from collections import Counter
    c = Counter(off for off, *_ in leaf_log)
    print("\n== leaf histogram ==", dict(sorted(c.items())))
    print("== WritableNand: %d blocks erased, %d pages programmed ==" %
          (sum(1 for x in nand.log if x[0] == "erase"),
           sum(1 for x in nand.log if x[0] == "prog")))
    # File-records table state (transc_data reads this; empty until an upload cmd).
    print("== file-records table @0x08020888: count@0x0802088c=%d ==" %
          rd(0x0802088C))
    return 0


if __name__ == "__main__":
    sys.exit(main())
