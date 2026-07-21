#!/usr/bin/env python3
"""Disassembler / literal-resolver / immediate-finder for the ZC3201 nandboot blob.

nandboot.bin loads at 0x07ff8000 (runtime) and is aliased at 0x08000000.
Addresses in the reveng/lab notes use the 0x0800xxxx alias view for HAL veneers
and the 0x07ffxxxx view for timer callbacks; both map to the same blob byte:
  offset = addr & 0xffff  (for 0x07ff8000)  OR  addr - 0x08000000 (for alias)

Usage:
  zc_nb.py dis  <addr> [n]     disassemble n insns from addr (alias-base 0x08000000)
  zc_nb.py lit  <addr> [n]     disassemble + resolve ldr [pc,#imm] literals
  zc_nb.py imm  <value>        find all 32-bit words == value (report both views)
  zc_nb.py refs <addr>         find words that equal addr or addr in either view
"""
import sys, struct
from capstone import Cs, CS_ARCH_ARM, CS_MODE_ARM, CS_MODE_LITTLE_ENDIAN

NB = "/home/jojo/tiptoi/tt-firmware-reveng/ZC3201/data/nandboot.bin"
ALIAS = 0x08000000       # nandboot alias base (PROG HAL veneer view)
HAL = 0x07ff8000         # nandboot load base (timer-cb view)
data = open(NB, "rb").read()
SIZE = len(data)

def to_off(addr):
    if ALIAS <= addr < ALIAS + SIZE: return addr - ALIAS
    if HAL <= addr < HAL + SIZE: return addr - HAL
    raise ValueError("addr %#x not in nandboot (size %#x)" % (addr, SIZE))

def w(off): return struct.unpack_from("<I", data, off)[0]

md = Cs(CS_ARCH_ARM, CS_MODE_ARM | CS_MODE_LITTLE_ENDIAN)

def dis(addr, n, resolve=False):
    off = to_off(addr)
    for insn in md.disasm(data[off:off + n*4], addr):
        extra = ""
        if resolve and insn.mnemonic.startswith("ldr") and "[pc, #" in insn.op_str:
            imm = int(insn.op_str.split("#")[1].rstrip("]"), 0)
            lit = insn.address + 8 + imm
            try:
                extra = "  ; [%#x]=%#010x" % (lit, w(to_off(lit)))
            except ValueError:
                extra = "  ; [%#x]=?" % lit
        print("0x%08x  %-8s %s%s" % (insn.address, insn.mnemonic, insn.op_str, extra))

def find_imm(val):
    for off in range(0, SIZE-3):
        if struct.unpack_from("<I", data, off)[0] == val:
            print("off %#06x  alias %#010x  hal %#010x" % (off, ALIAS+off, HAL+off))

def main():
    cmd = sys.argv[1]
    if cmd == "dis":
        dis(int(sys.argv[2],0), int(sys.argv[3],0) if len(sys.argv)>3 else 40)
    elif cmd == "lit":
        dis(int(sys.argv[2],0), int(sys.argv[3],0) if len(sys.argv)>3 else 60, resolve=True)
    elif cmd == "imm":
        find_imm(int(sys.argv[2],0))
    elif cmd == "refs":
        a = int(sys.argv[2],0)
        cands = {a}
        if ALIAS <= a < ALIAS+SIZE: cands.add(a - ALIAS + HAL)
        if HAL <= a < HAL+SIZE: cands.add(a - HAL + ALIAS)
        for off in range(0, SIZE-3):
            v = struct.unpack_from("<I", data, off)[0]
            if v in cands:
                print("off %#06x (alias %#010x) -> %#010x" % (off, ALIAS+off, v))

if __name__ == "__main__":
    main()
