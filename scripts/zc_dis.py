#!/usr/bin/env python3
"""Quick capstone disassembler for the ZC3201 producer.bin (base 0x08000000)."""
import sys
from capstone import Cs, CS_ARCH_ARM, CS_MODE_ARM, CS_MODE_LITTLE_ENDIAN

PROD = "/home/jojo/tiptoi/tt-firmware-reveng/ZC3201/data/producer.bin"
BASE = 0x08000000

def main():
    data = open(PROD, "rb").read()
    start = int(sys.argv[1], 0)
    n = int(sys.argv[2], 0) if len(sys.argv) > 2 else 40
    md = Cs(CS_ARCH_ARM, CS_MODE_ARM | CS_MODE_LITTLE_ENDIAN)
    md.detail = False
    off = start - BASE
    code = data[off:off + n * 4]
    for insn in md.disasm(code, start):
        # show literal pool words too
        print("0x%08x  %-8s %s" % (insn.address, insn.mnemonic, insn.op_str))

if __name__ == "__main__":
    main()
