#!/usr/bin/env python3
"""Resolve ldr rX,[pc,#imm] literal-pool words in producer.bin."""
import sys, struct
from capstone import Cs, CS_ARCH_ARM, CS_MODE_ARM, CS_MODE_LITTLE_ENDIAN
PROD = "/home/jojo/tiptoi/tt-firmware-reveng/ZC3201/data/producer.bin"
BASE = 0x08000000
data = open(PROD, "rb").read()
def w(a): return struct.unpack_from("<I", data, a - BASE)[0]
start = int(sys.argv[1], 0); n = int(sys.argv[2], 0) if len(sys.argv)>2 else 60
md = Cs(CS_ARCH_ARM, CS_MODE_ARM | CS_MODE_LITTLE_ENDIAN)
for insn in md.disasm(data[start-BASE:start-BASE+n*4], start):
    extra = ""
    if insn.mnemonic.startswith("ldr") and "[pc, #" in insn.op_str:
        imm = int(insn.op_str.split("#")[1].rstrip("]"), 0)
        lit = insn.address + 8 + imm
        extra = "  ; [%#x] = %#x" % (lit, w(lit))
    print("0x%08x  %-8s %s%s" % (insn.address, insn.mnemonic, insn.op_str, extra))
