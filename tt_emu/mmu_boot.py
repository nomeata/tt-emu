"""Authentic MMU boot: run the firmware under its own page table + demand-pager.

``memory-map-and-boot.md`` §5 seeds the machine to PROG's *pre-init entry* and lets the
firmware boot itself. This module supplies the two things the silicon + romboot provide so
that boot runs under the **real** ARMv5 MMU (Unicorn honours it), with the DMA engines as
genuine physical bus masters — no page-table inversion in Python:

1. **Handoff through nandboot's MMU builder.** Before PROG runs, we execute nandboot
   ``init2`` (:data:`INIT2`), which builds the authentic L1/L2 tables and sets ``SCTLR.M=1``
   (Unicorn then translates every CPU access). This is the same code the pen runs; we just
   enter it directly instead of replaying init1/init3.

2. **Exception entry + a romboot-style backing store.** The tables start most pages
   no-access (``AP=00``); the firmware demand-refines them on abort. On a prefetch/data
   abort Unicorn raises ``UC_HOOK_INTR`` (intno 3/4) but does not vector ARMv5 exceptions,
   so we decode the fault address, write FAR, bank to abort mode, and jump to nandboot's
   *own* handler (:data:`PABT_VEC`/:data:`DABT_VEC`) — which refines the page and retries.
   The refiner zeroes each frame it maps; the romboot would have loaded PROG's bytes into
   the compact physical layout it maps onto, so at the handler's return we write PROG's
   content into whichever physical frame the refiner chose. A per-VA **shadow** (PROG image
   + captured runtime writes) makes that survive frame eviction/re-zeroing. Romboot is
   hardware, so this backing store is part of the modelled machine, not a firmware hook.

Nothing in the firmware is patched or intercepted. See ``firmware-re/mmu-prototype`` for the
derivation (``54_shadow.py`` and ``FINDINGS.md``).
"""

from __future__ import annotations

import logging
import struct

import unicorn.arm_const as ac
from capstone import CS_ARCH_ARM, CS_MODE_ARM, CS_MODE_THUMB, Cs
from capstone.arm_const import ARM_OP_MEM, ARM_OP_REG
from unicorn import UC_HOOK_CODE, UcError
from unicorn.arm_const import (
    UC_ARM_REG_CPSR,
    UC_ARM_REG_LR,
    UC_ARM_REG_PC,
    UC_ARM_REG_SP,
    UC_ARM_REG_SPSR,
    UC_ARM_REG_CP_REG,
)

from .loader import Firmware, PROG_LOAD_ADDR
from .machine import Machine

log = logging.getLogger(__name__)

# --- nandboot addresses this boot generation exposes (from the .upd disassembly) --------
#: nandboot ``init2`` — builds the L1/L2 page tables and enables the MMU.
INIT2 = 0x0800_18D4
#: A low, ``AP=11`` (always-accessible) stack for the init2 call, and the return sentinel
#: it returns to (LR); both live in the page-table/globals region init2 keeps mapped.
INIT2_STACK = 0x0800_8400
INIT2_SENTINEL = 0x0800_7000
#: nandboot's own abort vectors (prefetch, data) — the reset table at 0x08000000.
PABT_VEC = 0x0800_000C
DABT_VEC = 0x0800_0010
#: The handlers' retry instructions (``subs pc,lr,#4`` / ``#8``); we place page content
#: here, just before the firmware resumes the faulting instruction.
PABT_RET = 0x0800_0190
DABT_RET = 0x0800_01C4
#: Abort-mode stack (from the reset handler's literal); the ``push {r0-r12,lr}`` prologue
#: of the fault handlers uses it.
SP_ABT = 0x0800_6000
#: The firmware's demand-pager is direct-mapped: a VA hashes to a frame index and lands at
#: frame ``PAGER_FRAME_BASE + index*0x1000``; an eviction table records the page VA at each
#: frame. At :data:`PAGER_REFINER_CAPTURE` (early in the refiner ``0x080014d8``) r4 holds the
#: index and r5 the page VA about to be evicted from that frame (0 if the frame is free) —
#: the exact point to snapshot the outgoing page's content before the refiner reuses it.
PAGER_REFINER_CAPTURE = 0x0800_14F8
PAGER_FRAME_BASE = 0x0800_9000
#: The loader keeps the work/data region **resident** (identity, AP=11) and demand-pages
#: only the code range below it. These nandboot literals bound it: the allocator's frame
#: threshold (page-aligned) is the code/data split, and its dynamic-alloc VA pool base is
#: the top of the fixed-data region (the pool above stays unmapped for the firmware to hand
#: out). Without residency the data pages get demand-paged into code frames and the pager
#: clobbers them (the DMA-pool aliasing that crashed the book/media path).
ALLOC_THRESHOLD_LIT = 0x0800_0C2C   #: -> frame threshold ~0x08120a63; split @0x08120000
ALLOC_POOL_START_LIT = 0x0800_0C34  #: -> dynamic-VA pool base ~0x081dcf44
#: CP15 register selectors (coproc, is64, sec, CRn, CRm, opc1, opc2).
_FAR = (15, 0, 0, 6, 0, 0, 0)   # fault address register (c6)
_TTBR0 = (15, 0, 0, 2, 0, 0, 0)  # translation table base 0
#: L2 small-page descriptor "mapping present" bits.
_PRESENT = 0xFF0
#: Top of the demand-paged main-RAM domain (the refiner refines [0x08009000, this)).
#: PROG's image occupies the low part; runtime stack/heap pages live above it and must be
#: shadowed too — the refiner re-zeroes any frame it maps, so an evicted stack page would
#: otherwise lose its return addresses.
DEMAND_TOP = 0x0840_0000

_CPSR_MODE_MASK = 0x1F
_MODE_ABORT = 0x17
_CPSR_I = 0x80
_CPSR_T = 0x20


class MmuBoot:
    """Runs nandboot's MMU builder, then services aborts + backs demand-paging.

    Instantiate after the artifacts and boot seeds are in place, call :meth:`setup`, then
    seed PROG's entry state and run the machine normally. The abort handler is wired into
    :class:`~tt_emu.machine.Machine` so the machine's single ``UC_HOOK_INTR`` still owns
    interrupt dispatch (SVC vs. abort).
    """

    def __init__(self, machine: Machine, firmware: Firmware) -> None:
        self.m = machine
        self.uc = machine.uc
        self.prog = firmware.prog.data
        self.prog_hi = PROG_LOAD_ADDR + len(self.prog)

        # Demand-paging backing store (romboot stand-in). The firmware pages evicted frames
        # out to NAND swap and back, but that round-trip does not reconstruct content under
        # emulation, so we shadow each page: snapshot it the instant the refiner evicts it
        # (keyed by the firmware's own eviction record) and restore it when it next faults.
        self.shadow: dict[int, bytes] = {}   # VA page -> latest content
        self.pgstack: list[int] = []         # LIFO of fault pages awaiting restore
        self.l1_base = 0                     # set from TTBR0 after init2

        self._md = Cs(CS_ARCH_ARM, CS_MODE_ARM)
        self._md.detail = True
        self._mdt = Cs(CS_ARCH_ARM, CS_MODE_THUMB)
        self._mdt.detail = True
        # capstone's ARM operand register names, incl. the ABI aliases (sb/sl/fp/ip) it
        # emits for r9-r12 — a missing alias silently reads base=0 and mis-decodes faults.
        self._creg = {f"r{i}": getattr(ac, f"UC_ARM_REG_R{i}") for i in range(13)}
        self._creg.update(
            sp=ac.UC_ARM_REG_SP, lr=ac.UC_ARM_REG_LR, pc=ac.UC_ARM_REG_PC,
            sb=ac.UC_ARM_REG_R9, sl=ac.UC_ARM_REG_R10,
            fp=ac.UC_ARM_REG_R11, ip=ac.UC_ARM_REG_R12,
        )

    # --- setup --------------------------------------------------------------------------

    def setup(self) -> None:
        """Run init2 (build tables + enable MMU), then install the abort machinery."""
        uc = self.uc
        # init2 runs with a low always-accessible stack and returns to a sentinel.
        uc.reg_write(UC_ARM_REG_SP, INIT2_STACK)
        uc.reg_write(UC_ARM_REG_LR, INIT2_SENTINEL)
        uc.reg_write(UC_ARM_REG_CPSR, 0x13)  # SVC
        uc.emu_start(INIT2, INIT2_SENTINEL, count=3_000_000)
        sctlr = uc.reg_read(UC_ARM_REG_CP_REG, (15, 0, 0, 1, 0, 0, 0))
        if not sctlr & 1:
            raise RuntimeError(f"init2 did not enable the MMU (SCTLR={sctlr:#010x})")
        self.l1_base = uc.reg_read(UC_ARM_REG_CP_REG, _TTBR0) & 0xFFFF_C000
        log.info("MMU boot: init2 built the page table, SCTLR=%#010x TTBR0=%#010x",
                 sctlr, self.l1_base)
        self._make_data_resident()

        # Seed the abort-mode banked stack the fault handlers push onto.
        cpsr = uc.reg_read(UC_ARM_REG_CPSR)
        uc.reg_write(UC_ARM_REG_CPSR, (cpsr & ~_CPSR_MODE_MASK) | _MODE_ABORT)
        uc.reg_write(UC_ARM_REG_SP, SP_ABT)
        uc.reg_write(UC_ARM_REG_CPSR, cpsr)

        # Snapshot each page the instant the refiner evicts it (scoped hook).
        uc.hook_add(UC_HOOK_CODE, self._on_refiner_evict,
                    begin=PAGER_REFINER_CAPTURE, end=PAGER_REFINER_CAPTURE)
        # Restore page content at the handlers' return instructions (scoped hooks: they fire
        # only at these two PCs, so there is no per-instruction cost).
        for ret in (PABT_RET, DABT_RET):
            uc.hook_add(UC_HOOK_CODE, self._on_handler_return, begin=ret, end=ret)
        # The machine's UC_HOOK_INTR delegates prefetch/data aborts (intno 3/4) here.
        self.m.set_abort_handler(self._on_abort)

    def _make_data_resident(self) -> None:
        """Pin the work/data region resident (identity, AP=11), like the loader does.

        init2 leaves every page AP=00 (fault-on-access), which would demand-page the *data*
        region too — mapping it into the small code-frame pool and letting the pager clobber
        it. The real loader keeps data resident and demand-pages only code. We replicate that
        for the fixed-data window ``[frame-threshold, dynamic-pool-base)`` (bounds read from
        the firmware's own allocator literals), seeding each page's image content; the
        dynamic-alloc pool above stays untouched for the firmware to manage.
        """
        uc = self.uc
        lo = struct.unpack("<I", uc.mem_read(ALLOC_THRESHOLD_LIT, 4))[0] & ~0xFFF
        hi = struct.unpack("<I", uc.mem_read(ALLOC_POOL_START_LIT, 4))[0] & ~0xFFF
        n = 0
        for va in range(lo, hi, 0x1000):
            l1e = struct.unpack("<I", uc.mem_read(self.l1_base + (va >> 20) * 4, 4))[0]
            if (l1e & 3) != 1:  # only coarse-mapped pages have an L2 entry to flip
                continue
            l2a = (l1e & 0xFFFFFC00) + ((va >> 12) & 0xFF) * 4
            uc.mem_write(l2a, struct.pack("<I", (va & 0xFFFFF000) | 0xFFE))  # identity, AP=11
            if va < self.prog_hi:
                off = va - PROG_LOAD_ADDR
                uc.mem_write(va, self.prog[off:off + 0x1000].ljust(0x1000, b"\x00"))
            n += 1
        try:
            uc.ctl_remove_cache(lo, hi)
        except UcError:
            pass
        log.info("MMU boot: pinned %d data pages resident [%#010x, %#010x)", n, lo, hi)

    # --- page-table walk (read the firmware's live tables) ------------------------------

    def _cur_pa(self, va: int) -> int | None:
        """Physical frame base mapped for ``va``'s page, or ``None`` if not present."""
        l1 = struct.unpack("<I", self.uc.mem_read(self.l1_base + (va >> 20) * 4, 4))[0]
        if (l1 & 3) != 1:  # not a coarse-page-table descriptor
            return None
        entry = struct.unpack("<I", self.uc.mem_read((l1 & 0xFFFFFC00) + ((va >> 12) & 0xFF) * 4, 4))[0]
        return (entry & 0xFFFFF000) if (entry & _PRESENT) else None

    def _phys(self, va: int) -> int | None:
        base = self._cur_pa(va)
        return None if base is None else (base | (va & 0xFFF))

    def read_va(self, va: int, size: int) -> bytes:
        """Read a virtual address's content, even if its page is paged out.

        Prefers the live physical frame (freshest), falls back to the demand-paging shadow
        (authoritative content of an evicted page), then to a plain physical read for
        identity/low addresses outside PROG's image. For inspection (health checkpoints),
        so it assumes the read stays within one page. Under the MMU a plain
        ``machine.read_u32(va)`` would read the wrong bytes for a remapped VA.
        """
        pa = self._phys(va)
        if pa is not None:
            return bytes(self.uc.mem_read(pa, size))
        page = self.shadow.get(va & ~0xFFF)
        if page is not None:
            off = va & 0xFFF
            return page[off:off + size]
        return bytes(self.uc.mem_read(va, size))

    def _faulting(self, va: int) -> bool:
        """True if ``va``'s page currently faults (unmapped or AP=00 no-access)."""
        l1 = struct.unpack("<I", self.uc.mem_read(self.l1_base + (va >> 20) * 4, 4))[0]
        if (l1 & 3) == 2:  # 1 MB section, always accessible here
            return False
        if (l1 & 3) != 1:
            return True
        entry = struct.unpack("<I", self.uc.mem_read((l1 & 0xFFFFFC00) + ((va >> 12) & 0xFF) * 4, 4))[0]
        return (entry & _PRESENT) == 0

    def _first_fault_page(self, addr: int, size: int) -> int:
        """The first faulting page in ``[addr, addr+size)`` (for multi-register accesses)."""
        p = addr & ~0xFFF
        while p < addr + size:
            if self._faulting(p):
                return p
            p += 0x1000
        return addr

    # --- fault-address decode -----------------------------------------------------------

    def _reg(self, name: str) -> int:
        return self.uc.reg_read(self._creg.get(name, 0))

    def _read_insn(self, va: int) -> bytes:
        # uc.mem_read is physical; translate so we decode the instruction the CPU fetched,
        # not whatever shares the identical physical address on a VA != PA page.
        p = self._phys(va)
        return bytes(self.uc.mem_read(p if p is not None else va, 4))

    def _fault_addr(self, pc: int, thumb: bool) -> int:
        """The data address a faulting load/store at ``pc`` targets."""
        dis = self._mdt if thumb else self._md
        try:
            insn = next(dis.disasm(self._read_insn(pc), pc))
        except StopIteration:
            return pc
        for op in insn.operands:
            if op.type == ARM_OP_MEM:
                base = self._reg(insn.reg_name(op.mem.base)) if op.mem.base else 0
                idx = self._reg(insn.reg_name(op.mem.index)) if op.mem.index else 0
                addr = (base + op.mem.disp + (idx << op.mem.lshift)) & 0xFFFFFFFF
                return self._first_fault_page(addr, 8)
        # block transfers (push/pop/ldm/stm): fault page is somewhere in the reg-list range
        mn = insn.mnemonic
        regs = [o for o in insn.operands if o.type == ARM_OP_REG]
        n = len(regs)
        if mn in ("push", "pop"):
            base = self.uc.reg_read(UC_ARM_REG_SP)
        elif mn.startswith("ldm") or mn.startswith("stm"):
            if not regs:
                return pc
            base = self._reg(insn.reg_name(regs[0].reg))
            n -= 1
        else:
            return pc
        decr = (mn == "push" or mn.startswith(("stmdb", "stmfd", "ldmdb", "ldmea")))
        start = (base - n * 4) & 0xFFFFFFFF if decr else base
        return self._first_fault_page(start, max(n * 4, 4))

    # --- abort handling + backing store -------------------------------------------------

    def _on_refiner_evict(self, uc: object, _addr: int, _size: int, _ud: object) -> None:
        """Snapshot the page the refiner is about to evict from its frame.

        Runs at :data:`PAGER_REFINER_CAPTURE`: r5 is the outgoing page's VA (0 if the frame
        is free), r4 the frame index. The frame still holds that page's live content — copy
        it into the shadow before the refiner reuses the frame. Keyed by the firmware's own
        eviction record, so it needs no ownership tracking and has no capture-timing gap.
        """
        old_va = self.uc.reg_read(ac.UC_ARM_REG_R5)
        if old_va == 0:
            return
        frame = PAGER_FRAME_BASE + self.uc.reg_read(ac.UC_ARM_REG_R4) * 0x1000
        self.shadow[old_va & ~0xFFF] = bytes(self.uc.mem_read(frame, 0x1000))

    def _on_abort(self, intno: int) -> None:
        """Vector a prefetch(3)/data(4) abort into nandboot's own handler.

        Called from :meth:`Machine._on_intr`. Performs the ARMv5 abort entry Unicorn omits:
        FAR, bank to abort mode (I=1), SPSR, LR, then jump to the firmware's vector.
        """
        uc = self.uc
        pc = uc.reg_read(UC_ARM_REG_PC)
        cpsr = uc.reg_read(UC_ARM_REG_CPSR)
        if intno == 3:
            fault = pc
            vec, lroff = PABT_VEC, 4
        else:
            fault = self._fault_addr(pc, bool(cpsr & _CPSR_T))
            vec, lroff = DABT_VEC, 8
        self.pgstack.append(fault & ~0xFFF)
        uc.reg_write(UC_ARM_REG_CP_REG, _FAR + (fault,))
        uc.reg_write(UC_ARM_REG_CPSR, (cpsr & ~0x3F) | _MODE_ABORT | _CPSR_I)
        uc.reg_write(UC_ARM_REG_SPSR, cpsr)
        uc.reg_write(UC_ARM_REG_LR, (pc + lroff) & 0xFFFFFFFF)
        uc.reg_write(UC_ARM_REG_PC, vec)

    def _on_handler_return(self, _uc: object, _addr: int, _size: int, _ud: object) -> None:
        """At a fault handler's retry instruction: restore the refined page's content."""
        if self.pgstack:
            self._place(self.pgstack.pop())

    def _place(self, vp: int) -> None:
        """Restore ``vp``'s content into the frame the refiner just mapped.

        The refiner memsets a fresh frame (or attempts a NAND page-in that does not
        reconstruct content under emulation), so on every fault we rewrite the page's
        authoritative bytes: its shadow if we have snapshotted it (captured runtime writes),
        else PROG's image for a code/data page. A page above the image with no shadow is a
        genuinely fresh runtime page — leave the refiner's zeros.
        """
        if not (PROG_LOAD_ADDR <= vp < DEMAND_TOP):
            return
        pa = self._cur_pa(vp)
        if pa is None:
            return
        content = self.shadow.get(vp)
        if content is None and vp < self.prog_hi:
            off = vp - PROG_LOAD_ADDR
            content = self.prog[off:off + 0x1000].ljust(0x1000, b"\x00")
        if content is not None:
            self.uc.mem_write(pa, content)
        try:
            self.uc.ctl_remove_cache(vp, vp + 0x1000)
        except UcError:
            pass
