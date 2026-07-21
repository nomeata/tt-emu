#!/usr/bin/env python3
"""Dynamic bring-up + NAND-seam trace of the ZC3201 producer.bin.

Uses the addresses from docs/zc3201-producer-addresses.md (single ring dispatcher,
indirect NAND). This probe drives the **real factory command protocol** and shows
how far the producer formats a NAND under a Python harness.

KEY FINDING (this leg — supersedes the earlier "gNand init fails chip-detect"):
the producer's format (cmd 5 ``transc_format``) is not a standalone command — it
requires the geometry to have been loaded first by the host protocol sequence:

  * **cmd 2** ``transc_get_chip_id`` (worker 0x08001260): resets the NAND (``rst
    nand 0..3``), selects each CE, issues READ-ID over the NFC and reports the
    chip-ID dword to the host.
  * **cmd 3** ``transc_set_chip_param`` (worker 0x08001430): the host sends a
    287-byte chip-param blob (``arg0`` in the ring packet = a POINTER to it). The
    worker memcpy's it into the static struct 0x0801f751, then the descriptor
    builder ``0x080056d4`` re-reads the physical chip-ID over the NFC, matches it
    against the blob's expected ID, and — when they match ("find chip=1") — builds
    the gNand device descriptor ``*0x08024b50`` (0x54 bytes) and sets the geometry
    global 0x0801569c. The descriptor's NAND method vtable is filled with the
    **static** leaves: read/write/erase/readspare at
    +0x28=0x08005c1c +0x2c=0x08005b14 +0x34=0x08005be0 +0x38=0x08005d44
    +0x3c=0x08005db0 +0x40=0x08005a04 +0x44=0x08005af8, +0x30=0x08005cd8.
    (These are the NAND leaves docs/§8 said would need dynamic tracing — they are
    pinned statically here.)
  * **cmd 5** ``transc_format`` (worker 0x08001660): now that ``*0x08024b50`` is
    a real device, the gNand init 0x08002eb4 no longer prints "init error gNand".

The descriptor geometry is decoded from the blob (r4 = 0x0801f751 = arg0) as:

    desc[+4]   = LE32(blob[0x14:0x18])                    # total block count
    desc[+0x14]= LE16(blob[6:8])                          # page size (bytes)
    desc[+0x10]= LE16(blob[0xc:0xe])                      # plane/sector size
    desc[+0xc] = LE16(blob[8:0xa]) / desc[+0x10]          # pages-per-block group
    desc[+0x1c]= LE16(blob[4:6])
    desc[+0x18]= LE16(blob[4:6]) / desc[+0x1c]  (=1)
    desc[+0]   = blob[0x13]
    desc[+8]   = n_chips * desc[+0xc]
    blob[0xf]  = a flag; if ==1 a different (bad-block?) path 0x08001590 runs.

The chip-ID must be returned on the NFC read-ID register **0x0404a150** (the same
register the MT producer uses; the ZC3201 NAND HAL uses BOTH the 0x0404a000 band
— status/ready at +0x158 bit31, data at +0x150 — AND the 0x04070000 band). The
blob's [0:3] expected-ID must equal the returned chip-ID for "find chip=1".

REMAINING WALL: with a valid descriptor, cmd 5's gNand init 0x08002eb4 gets past
the alloc, but the medium/FatLib build (via 0x0800cd78 / 0x08012a88) then calls a
still-null method pointer -> fetch fault at PC=0x10000. Two coupled next steps:
(1) the EXACT geometry sub-field encoding needs the real host flash_ic.ini values
(the pen's true NAND chip-ID + page/block bytes) so the medium's method table
initialises fully; (2) once the medium builds, cmd 5's format worker 0x0800849c
iterates the chip and its NAND writes (through the 0x08005b14/... leaves, hooked
here) can be captured to a WritableNand.
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

# NAND method-vtable leaves (from the cmd-3 descriptor builder 0x080056d4) — the
# seam a capture harness hooks to a WritableNand (n_read/n_write/n_erase/...).
NAND_LEAVES = {
    "read?": 0x08005C1C, "write?": 0x08005B14, "readspare": 0x08005BE0,
    "m0x38": 0x08005D44, "m0x3c": 0x08005DB0, "m0x40": 0x08005A04,
    "m0x44": 0x08005AF8, "m0x30": 0x08005CD8,
}

# The pen's NAND chip-ID (returned on NFC read-ID). This value must match the
# blob's expected-ID [0:3]. Placeholder Hynix id (AD DC 10 95) pending the real
# ZC3201 flash_ic.ini value — see module docstring.
CHIP_ID = 0x9510DCAD


def main() -> int:
    prod = open(PROD, "rb").read()
    uc = Uc(UC_ARCH_ARM, UC_MODE_ARM | UC_MODE_LITTLE_ENDIAN)
    uc.mem_map(BASE, 0x0040_0000)
    uc.mem_map(0x0900_0000, 0x0800_0000)
    uc.mem_write(BASE, prod)
    uc.mem_map(0x0400_0000, 0x0020_0000)

    def rd(a, n=4):
        return int.from_bytes(uc.mem_read(a, n), "little")

    def hook_mmio_r(uc, acc, addr, sz, val, ud):
        b0 = addr & ~3
        if addr == NFC_READID_REG:
            uc.mem_write(b0, struct.pack("<I", CHIP_ID))
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

    def call_cmd(cmd, blob=b"", arg1=0, budget=40_000_000):
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

    # --- build the 287-byte chip-param blob (see module docstring decode) -------
    blob = bytearray(287)
    struct.pack_into("<I", blob, 0, CHIP_ID)   # [0:4] expected chip-ID (must match)
    struct.pack_into("<H", blob, 4, 1)         # [4:6]  -> desc+0x1c ; desc+0x18=1
    struct.pack_into("<H", blob, 6, 2048)      # [6:8]  page size    -> desc+0x14
    struct.pack_into("<H", blob, 8, 0x8000)    # [8:0xa] block bytes -> /planesize
    struct.pack_into("<H", blob, 0xC, 512)     # [0xc:0xe] plane sz  -> desc+0x10
    blob[0xF] = 0                              # flag (!=1)
    struct.pack_into("<I", blob, 0x14, 4096)   # [0x14:0x18] block count -> desc+4

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
    call_cmd(5, budget=80_000_000)
    print("\nfinal geom=%#x dev=%#x" % (rd(GEOM_PTR), rd(DEV_OBJ)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
