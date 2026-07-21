#!/usr/bin/env python3
"""Dynamic bring-up + NAND-seam trace of the ZC3201 producer.bin.

Uses the addresses from docs/zc3201-producer-addresses.md (single ring dispatcher,
indirect NAND). This probe drives the **real factory command protocol** and shows
how far the producer formats a NAND under a Python harness.

STATUS (this leg): the MtdLib-init wall is SOLVED. cmd 2 -> cmd 3 -> cmd 5
``transc_format`` runs to completion (``RET r0=1``) and MtdLib initialises the NAND
partition with the correct 512-byte-page geometry::

    MtdLib - NandPart:Bits=0x10000000,FstPl=0,StartB=0,BCnt=2048,PCnt=2,
             LPCnt=2,BPerP=1024,PgPerB=32,BytPSec=512

The three ingredients that crack it (see docs/zc3201-producer-addresses.md §10):

  1. **The cmd-3 chip-param blob IS the ``.upd``'s own flash_ic descriptor**
     (``update.upd[0x200:0x240]``), NOT a hand-built struct. It is the Samsung
     **K9F5608** row: chip-ID ``EC 75 A5 BD``, page 512, spare 16, 32 pages/block,
     2048 blocks, planeblocks 1024, col-cycles 1, row-cycles 2, custom 1. The
     descriptor builder ``0x080056d4`` decodes it (r4=blob, r5=desc):
       desc[+0]=blob[0x13] (custom) ; desc[+4]=LE32(blob[0x14:0x18]) (flag) ;
       desc[+0x1c]=LE16(blob[4:6]) (page size 512) ;
       desc[+0x10]=LE16(blob[0xc:0xe]) (planeblocks 1024) ;
       desc[+0xc]=LE16(blob[8:0xa]) / desc[+0x10] (planes=2048/1024=2) ;
       desc[+0x14]=LE16(blob[6:8]) (pages/block 32) ;
       desc[+0x18]=LE16(blob[4:6]) / desc[+0x1c] (=1).
     The critical branch is ``blob[0xf]==1`` (columnaddrcycle==1) selecting the
     **small-page** path; the earlier placeholder blob had ``blob[0xf]=0`` and took
     the large-page path, yielding the scrambled ``page size=1, planes=64`` geometry.
  2. **The physical chip-ID ``0xBDA575EC``** returned on NFC ``0x0404a150`` (the
     dword the builder assembles from bytes EC 75 A5 BD; must equal ``blob[0:4]``).
  3. **The SoC chip-ID ``0x33323931`` ("1923") at ``0x04000000``.** This is the
     real MtdLib wall: ``mtd_set_pool`` ``0x08012a88`` copies the pool method table
     to the global ``0x08027090``, then calls a SoC-signature check ``0x08012a0c``
     (a jump table of ``ldr *0x04000000; cmp "1923"`` variants). On **failure** it
     runs an error-cleanup ``memset(pool, 0, 0x18)`` that zeroes the method table,
     so the next ``ldr pc,[sb]`` (``0x08012718``, sb=pool) fetch-faults at PC=0. The
     placeholder harness returned 0 at 0x04000000 -> check failed -> pool wiped ->
     the PC=0x10000 fault docs/§9 "Leg 6" hit. Returning the SoC chip-ID passes the
     check, the pool survives, and transc_format completes.

REMAINING WALL (next leg): ``transc_format`` (cmd 5, worker 0x08001660) only
*initialises* the MtdLib partition — it calls **none** of the NAND write/erase
leaves (verified: the 8 static leaves 0x08005b14/... fire 0 times during cmd 5).
The actual full-chip erase + FS-metadata + FAT write is driven by **other** commands
(``transc_erase`` cmd 4 worker 0x080015f8; the full-chip iterate worker 0x0800849c
reached by cmd 7/10; the block/boot writers 0x0800cf14/0x08009cb4 via cmd 11) — but
these need the **host protocol's real packet arguments** (cmd 4's "erase start/end"
came through as 0/0 with a naive ``pkt[4..7]``/``pkt[8..9]`` mapping, so the arg
layout is a small struct, not the two ring words). Reconstructing that packet
sequence (the Windows host tool's driver) is the next step: then hook the static
leaves to a WritableNand (ZC3201 ring-dispatch variant of tt_emu.nand_provision),
capture the writes, convert to a NandImage with 512B-page + 16B-spare fidelity, and
reach fs_storage_mount_init 0x0802d0e0 mounting A:/B:.
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
GEOM_PTR = 0x0801569C        # NAND geometry global (set by cmd 3)
DEV_OBJ = 0x08024B50         # gNand device descriptor pointer (set by cmd 3)
NFC_READID_REG = 0x0404A150  # producer reads the chip-ID dword here
SOC_CHIPID_REG = 0x04000000  # SysCon REG_CHIP_ID — MtdLib checks this == "1923"

# NAND method-vtable leaves (from the cmd-3 descriptor builder 0x080056d4) — the
# seam a capture harness hooks to a WritableNand (read/write/erase/readspare).
NAND_LEAVES = {
    "l28": 0x08005C1C, "l2c": 0x08005B14, "l34_rdspare": 0x08005BE0,
    "l38": 0x08005D44, "l3c": 0x08005DB0, "l40": 0x08005A04,
    "l44": 0x08005AF8, "l30": 0x08005CD8,
}

# The pen's real NAND chip-ID (Samsung K9F5608), returned on the NFC read-ID
# register; equals blob[0:4] = bytes EC 75 A5 BD assembled little-endian.
CHIP_ID = 0xBDA575EC
# The ZC3201 SoC chip-ID ("1923") the MtdLib pool-init SoC-signature check reads.
SOC_CHIP_ID = 0x33323931


def main() -> int:
    prod = open(PROD, "rb").read()
    upd = open(UPD, "rb").read()
    uc = Uc(UC_ARCH_ARM, UC_MODE_ARM | UC_MODE_LITTLE_ENDIAN)
    uc.mem_map(BASE, 0x0040_0000)
    uc.mem_map(0x0900_0000, 0x0800_0000)
    uc.mem_write(BASE, prod)
    uc.mem_map(0x0400_0000, 0x0020_0000)

    leaf_counts: dict[str, int] = {}

    def rd(a, n=4):
        return int.from_bytes(uc.mem_read(a, n), "little")

    def hook_mmio_r(uc, acc, addr, sz, val, ud):
        b0 = addr & ~3
        if addr == NFC_READID_REG:
            uc.mem_write(b0, struct.pack("<I", CHIP_ID))
            return True
        if b0 == SOC_CHIPID_REG:
            uc.mem_write(b0, struct.pack("<I", SOC_CHIP_ID))
            return True
        if (0x0407_0000 <= addr < 0x0407_2000 or addr == 0x0401_0010
                or 0x0404_A000 <= addr < 0x0404_C000
                or 0x0405_B000 <= addr < 0x0405_C000):
            uc.mem_write(b0, struct.pack("<I", 0xFFFFFFFF))  # ready / status
            return True
        uc.mem_write(b0, struct.pack("<I", 0))
        return True

    uc.hook_add(UC_HOOK_MEM_READ, hook_mmio_r, None, 0x0400_0000, 0x0420_0000)
    uc.hook_add(UC_HOOK_MEM_WRITE, lambda *a: True, None, 0x0400_0000, 0x0420_0000)

    def on_unmapped(uc, acc, addr, sz, val, ud):
        if acc in (UC_MEM_FETCH, UC_MEM_FETCH_UNMAPPED, UC_MEM_FETCH_PROT):
            print("  [FETCH-FAULT] addr=%#x PC=%#x LR=%#x" %
                  (addr, uc.reg_read(UC_ARM_REG_PC), uc.reg_read(UC_ARM_REG_LR)))
            return False
        try:
            uc.mem_map(addr & ~0xFFFF, 0x10000)
            return True
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
        p = heap[0]
        heap[0] += n
        uc.mem_write(p, b"\x00" * n)
        ret(p)

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
            v = next(it)
            c = m.group(0)[-1]
            if c in "xX":
                return hex(v)
            if c in "du":
                return str(v)
            if c == "s":
                return read_cstr(v)
            if c == "c":
                return chr(v & 0xFF)
            return m.group(0)

        try:
            out = re.sub(r"%[0-9.\-+ lh#]*[xXdusc%]", sub, fmt)
        except Exception:
            out = fmt
        print("  [pr]", out.rstrip())
        ret(0)

    uc.hook_add(UC_HOOK_CODE, do_malloc, None, MALLOC_W, MALLOC_W)
    uc.hook_add(UC_HOOK_CODE, lambda *a: ret(0), None, FREE_W, FREE_W)
    uc.hook_add(UC_HOOK_CODE, do_printf, None, PRINTF, PRINTF)
    uc.hook_add(UC_HOOK_CODE, lambda *a: print("  [*** init error gNand ***]"),
                None, 0x08003030, 0x08003030)

    # Count NAND-leaf hits (the WritableNand capture seam for the next leg).
    def mk_leaf(name):
        def cb(uc, a, sz, ud):
            leaf_counts[name] = leaf_counts.get(name, 0) + 1
        return cb

    for nm, ad in NAND_LEAVES.items():
        uc.hook_add(UC_HOOK_CODE, mk_leaf(nm), None, ad, ad)

    uc.reg_write(UC_ARM_REG_CPSR, 0x13 | 0xC0)
    uc.reg_write(UC_ARM_REG_SP, SVC_SP)
    print("== startup -> USB loop %#x ==" % USB_LOOP)
    try:
        uc.emu_start(BASE, USB_LOOP, count=30_000_000)
    except UcError as e:
        print("STARTUP FAULT %s PC=%#x" % (e, uc.reg_read(UC_ARM_REG_PC)))
    print("reached loop=%s" % (uc.reg_read(UC_ARM_REG_PC) == USB_LOOP))

    SENTINEL = 0x0838_0000
    uc.mem_write(SENTINEL, struct.pack("<I", 0xFFFFFFFF))
    PKT = 0x0805_0000
    DATA = 0x0805_2000

    def call_cmd(cmd, blob=b"", arg1=0, budget=80_000_000):
        # ring packet [cmd:4][arg0:4][arg1:2][pad]; dispatcher forwards r0=arg0
        # (a POINTER to the data buffer), r1=arg1.
        uc.mem_write(DATA, blob.ljust(512, b"\x00"))
        pkt = struct.pack("<IIHH", cmd, DATA if blob else 0, arg1 & 0xFFFF, 0)
        uc.mem_write(PKT, pkt.ljust(64, b"\x00"))
        uc.reg_write(UC_ARM_REG_R0, PKT)
        uc.reg_write(UC_ARM_REG_R1, 0)
        uc.reg_write(UC_ARM_REG_SP, SVC_SP)
        uc.reg_write(UC_ARM_REG_LR, SENTINEL)
        uc.reg_write(UC_ARM_REG_CPSR, 0x13 | 0xC0)
        try:
            uc.emu_start(DISPATCH, SENTINEL, count=budget)
            pc = uc.reg_read(UC_ARM_REG_PC)
            print("[cmd %d] -> %s r0=%#x" %
                  (cmd, "RET" if pc == SENTINEL else "PC=%#x" % pc,
                   uc.reg_read(UC_ARM_REG_R0)))
        except UcError as e:
            print("[cmd %d] FAULT %s PC=%#x" % (cmd, e, uc.reg_read(UC_ARM_REG_PC)))

    # --- feed the REAL flash_ic descriptor (from .upd @0x200) as the cmd-3 blob ---
    # The Samsung K9F5608 row: bytes EC 75 A5 BD 00 02 20 00 ... (see module docs).
    blob = bytearray(upd[0x200:0x240].ljust(287, b"\x00"))
    assert struct.unpack_from("<I", blob, 0)[0] == CHIP_ID, "flash_ic chip-ID mismatch"

    print("\n== cmd 2 (transc_get_chip_id) ==")
    call_cmd(2)
    print("\n== cmd 3 (transc_set_chip_param) ==")
    call_cmd(3, bytes(blob))
    dev, geom = rd(DEV_OBJ), rd(GEOM_PTR)
    print("  *0x08024b50(dev)=%#x geom 0x0801569c=%#x" % (dev, geom))
    if dev:
        print("  desc[+0]=%#x [+4]=%#x [+8]=%#x [+0xc]=%#x [+0x10]=%#x "
              "[+0x14]=%#x [+0x18]=%#x [+0x1c]=%#x" %
              (rd(dev), rd(dev + 4), rd(dev + 8), rd(dev + 0xC), rd(dev + 0x10),
               rd(dev + 0x14), rd(dev + 0x18), rd(dev + 0x1C)))
        print("  vtable +0x28=%#x +0x2c=%#x +0x34=%#x +0x38=%#x +0x3c=%#x "
              "+0x40=%#x +0x44=%#x" %
              (rd(dev + 0x28), rd(dev + 0x2C), rd(dev + 0x34), rd(dev + 0x38),
               rd(dev + 0x3C), rd(dev + 0x40), rd(dev + 0x44)))
    print("\n== cmd 5 (transc_format) ==")
    call_cmd(5, budget=120_000_000)
    print("  NAND leaf hits during format:", leaf_counts or "{} (init only, no writes)")
    print("\nfinal geom=%#x dev=%#x" % (rd(GEOM_PTR), rd(DEV_OBJ)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
