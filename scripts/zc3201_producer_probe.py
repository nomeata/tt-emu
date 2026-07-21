#!/usr/bin/env python3
"""Dynamic bring-up + NAND-seam trace of the ZC3201 producer.bin.

Uses the addresses from docs/zc3201-producer-addresses.md (single ring dispatcher,
indirect NAND). Goal: (1) confirm startup reaches the USB loop; (2) drive cmd 5
(init) then cmd 7 (format) via the dispatcher; (3) find the NAND register-I/O
sites by logging PCs that touch the 0x0400xxxx MMIO / 0x04010000 DMA region.

Findings (2026-07-21, run against ZC3201/data/producer.bin):
  * The producer BOOTS under this harness: startup reaches the USB loop cleanly
    ("asic freq: 60000000", "malloc init", "gb_RAMBuffer=...", "Enter event
    loop"). printf/malloc/free hooks at the doc addresses work.
  * The NFC controller register band is **0x04070000** (NOT MT's 0x0404a000):
    the producer busy-polls **0x04070200** (ready bit) at HAL fn PC ~0x08006744,
    and reads **0x0407033c** at PC ~0x08006784. Making 0x04070000..0x04072000
    read "ready" (0xFFFFFFFF) removes the 20M-iteration spin.
  * BLOCKER: the gNand init (fn 0x08003084, error print at 0x08003030 →
    "init error gNand" @0x08013ec0) still fails chip-detect *early* (before any
    read-ID), so cmd 5 returns 0 and cmd 7 (format worker 0x0800849c) does
    nothing (dataLen 0). Next step: model the 0x04070000 NAND controller (and/or
    seed the gNand/geometry global 0x0801569c) enough to pass detect, then the
    format worker will iterate the chip and the NAND writes can be captured to a
    WritableNand for tt_emu.nand_provision's ZC3201 variant.
"""
import collections
import struct
import sys

from unicorn import *
from unicorn.arm_const import *

PROD = "/home/jojo/tiptoi/tt-firmware-reveng/ZC3201/data/producer.bin"
UPD = "/home/jojo/tiptoi/tt-firmware-reveng/ZC3201/data/update.upd"

BASE = 0x08000000
ENTRY = 0x08000000
USB_LOOP = 0x080034A4
DISPATCH = 0x08003664
SVC_SP = 0x0802B000
PRINTF = 0x080003DC
MALLOC_W = 0x080004D4
FREE_W = 0x080004E0
GEOM_PTR = 0x0801569C  # NAND geometry global (ptr)

prod = open(PROD, "rb").read()
uc = Uc(UC_ARCH_ARM, UC_MODE_ARM | UC_MODE_LITTLE_ENDIAN)
uc.mem_map(BASE, 0x00400000)          # image + bss + stacks + ring
uc.mem_map(0x09000000, 0x08000000)    # heap
uc.mem_write(BASE, prod)

# MMIO
uc.mem_map(0x04000000, 0x00200000)
mmio_pc = collections.Counter()
mmio_addrs = collections.Counter()


TRACE_NFC = [False]


def hook_mmio_r(uc, acc, addr, sz, val, ud):
    pc = uc.reg_read(UC_ARM_REG_PC)
    mmio_pc[pc] += 1
    mmio_addrs[addr & ~3] += 1
    if TRACE_NFC[0] and 0x04070000 <= addr < 0x04072000:
        print("    [NFC rd] %#x  PC=%#x" % (addr, pc))
    b0 = addr & ~3
    # NFC/DMA status "ready/done" so polls fall through. ZC3201's producer NFC
    # band is 0x04070000 (dynamic-trace finding: it spins polling 0x04070200).
    if (0x0404A000 <= addr < 0x0404C000 or 0x0405B000 <= addr < 0x0405C000
            or 0x04070000 <= addr < 0x04072000 or addr == 0x04010010):
        uc.mem_write(b0, struct.pack("<I", 0xFFFFFFFF))
        return True
    uc.mem_write(b0, struct.pack("<I", 0))
    return True


def hook_mmio_w(uc, acc, addr, sz, val, ud):
    mmio_pc[uc.reg_read(UC_ARM_REG_PC)] += 1
    mmio_addrs[addr & ~3] += 1
    return True


uc.hook_add(UC_HOOK_MEM_READ, hook_mmio_r, None, 0x04000000, 0x04200000)
uc.hook_add(UC_HOOK_MEM_WRITE, hook_mmio_w, None, 0x04000000, 0x04200000)


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


uc.hook_add(UC_HOOK_MEM_READ_UNMAPPED | UC_HOOK_MEM_WRITE_UNMAPPED | UC_HOOK_MEM_FETCH_UNMAPPED,
            on_unmapped)


def rd(a, n=4):
    return int.from_bytes(uc.mem_read(a, n), "little")


def ret(v):
    uc.reg_write(UC_ARM_REG_R0, v & 0xFFFFFFFF)
    uc.reg_write(UC_ARM_REG_PC, uc.reg_read(UC_ARM_REG_LR) & ~1)


heap = [0x09000000]


def do_malloc(uc, a, s, ud):
    n = (uc.reg_read(UC_ARM_REG_R0) + 0x1F) & ~0x1F
    p = heap[0]
    heap[0] += n
    uc.mem_write(p, b"\x00" * n)
    ret(p)


def do_free(uc, a, s, ud):
    ret(0)


def read_cstr(p, mx=256):
    out = b""
    while len(out) < mx:
        c = uc.mem_read(p + len(out), 1)[0]
        if c == 0:
            break
        out += bytes([c])
    return out.decode("latin1", "replace")


prlog = []


def do_printf(uc, a, s, ud):
    fmt = read_cstr(uc.reg_read(UC_ARM_REG_R0))
    args = [uc.reg_read(r) for r in (UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3)]
    try:
        out = fmt
        # crude %x/%d substitution
        import re
        it = iter(args + [rd(uc.reg_read(UC_ARM_REG_SP) + i * 4) for i in range(6)])
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
        out = re.sub(r"%[0-9.\-+ lh#]*[xXdusc%]", sub, fmt)
    except Exception:
        out = fmt
    prlog.append(out)
    print("  [pr]", out.rstrip())
    ret(0)


uc.hook_add(UC_HOOK_CODE, do_malloc, None, MALLOC_W, MALLOC_W)
uc.hook_add(UC_HOOK_CODE, do_free, None, FREE_W, FREE_W)
uc.hook_add(UC_HOOK_CODE, do_printf, None, PRINTF, PRINTF)

uc.reg_write(UC_ARM_REG_CPSR, 0x13 | 0xC0)
uc.reg_write(UC_ARM_REG_SP, SVC_SP)

print("== startup: %#x -> USB loop %#x ==" % (ENTRY, USB_LOOP))
try:
    uc.emu_start(ENTRY, USB_LOOP, count=30_000_000)
    pc = uc.reg_read(UC_ARM_REG_PC)
    print("startup stop PC=%#x reached_loop=%s" % (pc, pc == USB_LOOP))
except UcError as e:
    print("STARTUP FAULT %s PC=%#x LR=%#x" % (e, uc.reg_read(UC_ARM_REG_PC), uc.reg_read(UC_ARM_REG_LR)))

print("geom ptr @%#x -> %#x" % (GEOM_PTR, rd(GEOM_PTR)))
g = rd(GEOM_PTR)
if g:
    print("  geom[+0x14/+0x18/+0x1c] = %#x %#x %#x" % (rd(g + 0x14), rd(g + 0x18), rd(g + 0x1c)))

SENTINEL = 0x08380000
uc.mem_write(SENTINEL, struct.pack("<I", 0xFFFFFFFF))
PKT = 0x08050000


def call_cmd(cmd, arg0=0, arg1=0, budget=20_000_000, label=""):
    pkt = struct.pack("<IIHH", cmd, arg0, arg1 & 0xFFFF, 0)
    uc.mem_write(PKT, pkt + b"\x00" * 8)
    uc.reg_write(UC_ARM_REG_R0, PKT)
    uc.reg_write(UC_ARM_REG_SP, SVC_SP)
    uc.reg_write(UC_ARM_REG_LR, SENTINEL)
    uc.reg_write(UC_ARM_REG_CPSR, 0x13 | 0xC0)
    m0 = sum(mmio_addrs.values())
    try:
        uc.emu_start(DISPATCH, SENTINEL, count=budget)
        pc = uc.reg_read(UC_ARM_REG_PC)
        print("[cmd %d %s] -> %s r0=%#x  (mmio +%d)" %
              (cmd, label, "RET" if pc == SENTINEL else "PC=%#x" % pc,
               uc.reg_read(UC_ARM_REG_R0), sum(mmio_addrs.values()) - m0))
    except UcError as e:
        print("[cmd %d %s] FAULT %s PC=%#x LR=%#x" %
              (cmd, label, e, uc.reg_read(UC_ARM_REG_PC), uc.reg_read(UC_ARM_REG_LR)))


print("\n== cmd 5 (init) ==")
TRACE_NFC[0] = True
call_cmd(5, label="init")
TRACE_NFC[0] = False
print("\n== cmd 7 (format) ==")
call_cmd(7, budget=40_000_000, label="format")

print("\n== MMIO access sites (top PCs) ==")
for pc, c in mmio_pc.most_common(20):
    print("  PC=%#010x : %d" % (pc, c))
print("== MMIO addresses (top) ==")
for a, c in mmio_addrs.most_common(15):
    print("  %#010x : %d" % (a, c))
