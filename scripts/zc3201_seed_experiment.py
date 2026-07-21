#!/usr/bin/env python3
"""EXPERIMENT: seed the producer's file-records table + drive cmd7, observe writes.

Builds on zc3201_producer_capture.py. Leg-9 shortcut probe: instead of reversing
the host file-upload USB command, populate the file-records table directly:
  * table header @0x08020888: {u32 ?; u32 count@+4; ...}   (iterator reads count @+4)
  * records @0x08020898, 0x24 bytes each; name[16] at record+0x14 (strcmp target)
Then drive cmd 2->3->5->4(erase)->7(data name=PROG) and log:
  * the 8 static gNand leaves (write=desc+0x2c=0x08005b14, erase=+0x38=0x08005d44)
  * the 0x08027060 library-services vtable slots touched during cmd7
  * whether the iterator 0x08007bc8 now returns a hit and cmd7 fires any write leaf.

Run: .venv/bin/python scripts/zc3201_seed_experiment.py
"""
import struct, sys
from unicorn import (
    UC_ARCH_ARM, UC_HOOK_CODE, UC_HOOK_MEM_FETCH_UNMAPPED, UC_HOOK_MEM_READ,
    UC_HOOK_MEM_READ_UNMAPPED, UC_HOOK_MEM_WRITE, UC_HOOK_MEM_WRITE_UNMAPPED,
    UC_MEM_FETCH, UC_MEM_FETCH_PROT, UC_MEM_FETCH_UNMAPPED, UC_MODE_ARM,
    UC_MODE_LITTLE_ENDIAN, Uc, UcError)
from unicorn.arm_const import (
    UC_ARM_REG_CPSR, UC_ARM_REG_LR, UC_ARM_REG_PC, UC_ARM_REG_R0, UC_ARM_REG_R1,
    UC_ARM_REG_R2, UC_ARM_REG_R3, UC_ARM_REG_SP)

PROD = "/home/jojo/tiptoi/tt-firmware-reveng/ZC3201/data/producer.bin"
UPD = "/home/jojo/tiptoi/tt-firmware-reveng/ZC3201/data/update.upd"
BASE = 0x08000000; USB_LOOP = 0x080034A4; DISPATCH = 0x08003664; SVC_SP = 0x0802B000
PRINTF = 0x080003DC; MALLOC_W = 0x080004D4; FREE_W = 0x080004E0
GEOM_PTR = 0x0801569C; DEV_OBJ = 0x08024B50
NFC_READID_REG = 0x0404A150; SOC_CHIPID_REG = 0x04000000
CHIP_ID = 0xBDA575EC; SOC_CHIP_ID = 0x33323931
NBLOCKS = 2048
TBL = 0x08020888; RECS = 0x08020898; DESC = 0x08022B00; VTABLE = 0x08027060
MODE_BYTE = 0x080156A0
LEAVES = {0x28:0x08005C1C,0x2C:0x08005B14,0x34:0x08005BE0,0x38:0x08005D44,
          0x3C:0x08005DB0,0x40:0x08005A04,0x44:0x08005AF8,0x30:0x08005CD8}

def main():
    prod = open(PROD,"rb").read(); upd = open(UPD,"rb").read()
    uc = Uc(UC_ARCH_ARM, UC_MODE_ARM|UC_MODE_LITTLE_ENDIAN)
    uc.mem_map(BASE,0x00400000); uc.mem_map(0x09000000,0x08000000); uc.mem_write(BASE,prod)
    uc.mem_map(0x04000000,0x00200000)
    leaf_log=[]; vt_log=[]
    def rd(a,n=4): return int.from_bytes(uc.mem_read(a,n),"little")
    def hook_mmio_r(uc,acc,addr,sz,val,ud):
        b0=addr&~3
        if addr==NFC_READID_REG: uc.mem_write(b0,struct.pack("<I",CHIP_ID)); return True
        if b0==SOC_CHIPID_REG: uc.mem_write(b0,struct.pack("<I",SOC_CHIP_ID)); return True
        if (0x04070000<=addr<0x04072000 or addr==0x04010010 or 0x0404A000<=addr<0x0404C000
                or 0x0405B000<=addr<0x0405C000):
            uc.mem_write(b0,struct.pack("<I",0xFFFFFFFF)); return True
        uc.mem_write(b0,struct.pack("<I",0)); return True
    uc.hook_add(UC_HOOK_MEM_READ,hook_mmio_r,None,0x04000000,0x04200000)
    uc.hook_add(UC_HOOK_MEM_WRITE,lambda *a:True,None,0x04000000,0x04200000)
    def on_unmapped(uc,acc,addr,sz,val,ud):
        if acc in (UC_MEM_FETCH,UC_MEM_FETCH_UNMAPPED,UC_MEM_FETCH_PROT):
            print("  [FETCH-FAULT] addr=%#x PC=%#x LR=%#x"%(addr,uc.reg_read(UC_ARM_REG_PC),uc.reg_read(UC_ARM_REG_LR))); return False
        try: uc.mem_map(addr&~0xFFFF,0x10000); return True
        except UcError: return False
    uc.hook_add(UC_HOOK_MEM_READ_UNMAPPED|UC_HOOK_MEM_WRITE_UNMAPPED|UC_HOOK_MEM_FETCH_UNMAPPED,on_unmapped)
    def ret(v): uc.reg_write(UC_ARM_REG_R0,v&0xFFFFFFFF); uc.reg_write(UC_ARM_REG_PC,uc.reg_read(UC_ARM_REG_LR)&~1)
    heap=[0x09000000]
    def do_malloc(uc,a,s,ud):
        n=(uc.reg_read(UC_ARM_REG_R0)+0x1F)&~0x1F; p=heap[0]; heap[0]+=n; uc.mem_write(p,b"\x00"*n); ret(p)
    def read_cstr(p,mx=256):
        out=b""
        while len(out)<mx:
            c=uc.mem_read(p+len(out),1)[0]
            if c==0: break
            out+=bytes([c])
        return out.decode("latin1","replace")
    def do_printf(uc,a,s,ud):
        import re
        fmt=read_cstr(uc.reg_read(UC_ARM_REG_R0))
        args=[uc.reg_read(r) for r in (UC_ARM_REG_R1,UC_ARM_REG_R2,UC_ARM_REG_R3)]
        args+=[rd(uc.reg_read(UC_ARM_REG_SP)+i*4) for i in range(6)]; it=iter(args)
        def sub(m):
            v=next(it); c=m.group(0)[-1]
            if c in "xX": return hex(v)
            if c in "du": return str(v)
            if c=="s": return read_cstr(v)
            if c=="c": return chr(v&0xFF)
            return m.group(0)
        try: out=re.sub(r"%[0-9.\-+ lh#]*[xXdusc%]",sub,fmt)
        except Exception: out=fmt
        print("  [pr]",out.rstrip()); ret(0)
    uc.hook_add(UC_HOOK_CODE,do_malloc,None,MALLOC_W,MALLOC_W)
    uc.hook_add(UC_HOOK_CODE,lambda *a:ret(0),None,FREE_W,FREE_W)
    uc.hook_add(UC_HOOK_CODE,do_printf,None,PRINTF,PRINTF)
    def mk_leaf(off):
        def cb(uc,a,sz,ud):
            r=[uc.reg_read(x) for x in (UC_ARM_REG_R0,UC_ARM_REG_R1,UC_ARM_REG_R2,UC_ARM_REG_R3)]
            sp=uc.reg_read(UC_ARM_REG_SP); st=[rd(sp+i*4) for i in range(4)]
            leaf_log.append((off,r,st,uc.reg_read(UC_ARM_REG_LR)))
        return cb
    for off,ad in LEAVES.items(): uc.hook_add(UC_HOOK_CODE,mk_leaf(off),None,ad,ad)
    uc.reg_write(UC_ARM_REG_CPSR,0x13|0xC0); uc.reg_write(UC_ARM_REG_SP,SVC_SP)
    print("== startup ==")
    try: uc.emu_start(BASE,USB_LOOP,count=30_000_000)
    except UcError as e: print("STARTUP FAULT",e)
    print("reached loop=%s"%(uc.reg_read(UC_ARM_REG_PC)==USB_LOOP))
    SENTINEL=0x08380000; uc.mem_write(SENTINEL,struct.pack("<I",0xFFFFFFFF))
    PKT=0x08050000; ARGBUF=0x08052000
    def call_cmd(cmd,arg0=0,arg1=0,budget=200_000_000,label=""):
        uc.mem_write(PKT,struct.pack("<IIHH",cmd,arg0,arg1&0xFFFF,0).ljust(64,b"\x00"))
        uc.reg_write(UC_ARM_REG_R0,PKT); uc.reg_write(UC_ARM_REG_R1,0)
        uc.reg_write(UC_ARM_REG_SP,SVC_SP); uc.reg_write(UC_ARM_REG_LR,SENTINEL)
        uc.reg_write(UC_ARM_REG_CPSR,0x13|0xC0); n0=len(leaf_log); v0=len(vt_log)
        try:
            uc.emu_start(DISPATCH,SENTINEL,count=budget); pc=uc.reg_read(UC_ARM_REG_PC)
            print("[cmd %d %s] -> %s r0=%#x leaves+%d vt+%d"%(cmd,label,
                "RET" if pc==SENTINEL else "PC=%#x"%pc,uc.reg_read(UC_ARM_REG_R0),
                len(leaf_log)-n0,len(vt_log)-v0))
        except UcError as e:
            print("[cmd %d %s] FAULT %s PC=%#x"%(cmd,label,e,uc.reg_read(UC_ARM_REG_PC)))
    blob=bytearray(upd[0x200:0x240].ljust(287,b"\x00"))
    call_cmd(2,label="get_chip_id")
    uc.mem_write(ARGBUF,bytes(blob)); call_cmd(3,arg0=ARGBUF,label="set_chip_param")
    print("  dev=%#x geom=%#x mode@156a0=%#x"%(rd(DEV_OBJ),rd(GEOM_PTR),uc.mem_read(MODE_BYTE,1)[0]))
    call_cmd(5,label="format")
    print("  mode@156a0 after fmt=%#x  156a2=%#x  156b0=%#x"%(
        uc.mem_read(MODE_BYTE,1)[0],uc.mem_read(0x080156a2,1)[0],uc.mem_read(0x080156b0,1)[0]))
    # dump the library-services vtable + hook its slots
    vt=[rd(VTABLE+i*4) for i in range(8)]
    print("  vtable@0x08027060:",[hex(x) for x in vt])
    def mk_vt(slot):
        def cb(uc,a,sz,ud):
            vt_log.append((slot,uc.reg_read(UC_ARM_REG_R0),uc.reg_read(UC_ARM_REG_R1),uc.reg_read(UC_ARM_REG_R2)))
        return cb
    for i,tgt in enumerate(vt):
        if 0x08000000<=tgt<0x08016000: uc.hook_add(UC_HOOK_CODE,mk_vt(i*4),None,tgt,tgt)
    # erase
    EA=0x08053000; uc.mem_write(EA,struct.pack("<II",0,NBLOCKS)); call_cmd(4,arg0=EA,label="erase",budget=400_000_000)
    # ---- SEED the file-records table: 1 record named PROG ----
    name=b"PROG\x00"
    rec=bytearray(0x24)
    struct.pack_into("<I",rec,0,0x100)      # guess: size at +0
    rec[0x14:0x14+len(name)]=name           # name[16] at +0x14
    uc.mem_write(RECS,bytes(rec))
    uc.mem_write(TBL,struct.pack("<II",0,1))  # header {?, count=1}
    print("  seeded: count@%#x=%d rec.name=%r"%(TBL+4,rd(TBL+4),read_cstr(RECS+0x14)))
    # cmd7 transc_data: desc {dataLen; ?; name[16]}
    DA=0x08053100; uc.mem_write(DA,struct.pack("<II",0x100,0)+b"PROG".ljust(16,b"\x00"))
    call_cmd(7,arg0=DA,label="data:PROG",budget=400_000_000)
    nw=lambda: sum(1 for x in leaf_log if x[0]==0x2c)
    print("  write-leaf(0x2c) count after cmd7=%d"%nw())
    # cmd 26 download_end_data == the FAT/content flush (worker 0x8000a74)
    call_cmd(26,label="download_end_data(flush)",budget=800_000_000)
    print("  write-leaf(0x2c) count after cmd26=%d"%nw())
    # cmd 6 transc_nandboot: write PROG.bin to boot area. arg0 -> 0x18-byte desc.
    NB=0x08053200; uc.mem_write(NB,struct.pack("<II",0x200,0)+b"PROG".ljust(16,b"\x00"))
    call_cmd(6,arg0=NB,label="nandboot",budget=800_000_000)
    print("  write-leaf(0x2c) count after cmd6=%d"%nw())
    print("\n== cmd7 static-leaf calls ==")
    from collections import Counter
    post=[x for x in leaf_log if x[3]!=0x80083c8]  # exclude erase-leaf frames
    print("leaf histogram (all):",dict(Counter(o for o,*_ in leaf_log)))
    print("vtable-slot histogram during run:",dict(Counter(s for s,*_ in vt_log)))
    print("count@%#x after cmd7=%d"%(TBL+4,rd(TBL+4)))
    return 0

if __name__=="__main__": sys.exit(main())
