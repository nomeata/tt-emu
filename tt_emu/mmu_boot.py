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
from unicorn import UC_CTL_TLB_FLUSH, UC_HOOK_CODE, UC_HOOK_MEM_WRITE, UcError
from unicorn.arm_const import (
    UC_ARM_REG_CPSR,
    UC_ARM_REG_LR,
    UC_ARM_REG_PC,
    UC_ARM_REG_SP,
    UC_ARM_REG_SPSR,
    UC_ARM_REG_CP_REG,
)

from typing import TYPE_CHECKING

from .loader import Firmware, PROG_LOAD_ADDR
from .machine import Machine

if TYPE_CHECKING:
    from .peripherals.audio import AudioDma

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
#: PROG's own page-eviction routine clears the frame's eviction-table entry here (str r0,
#: [fp,#0x800] with r0=0), after swapping/invalidating, before paging in a replacement — the
#: same eviction shape as the refiner but outside nandboot, so it needs the same content
#: snapshot (r5 = outgoing page VA, r4 = frame index) or the evicted page's writes are lost.
PAGER_PROG_EVICT = 0x0800_9634
PAGER_FRAME_BASE = 0x0800_9000
#: The pager's per-frame globals base (the refiner's ``[pc,#-0x8d0]`` literal) and
#: its eviction table (``[base+0x800+idx*4]`` = the page VA resident in frame idx,
#: 0 = free). The pager writes an entry when it maps a page into a frame; we watch
#: those writes to stamp the hardware frame-usage clock (:meth:`AudioDma.touch_frame`)
#: so the pager's LRU victim scan can rank frames instead of thrashing a few.
PAGER_SB_BASE = 0x0800_7F8C
PAGER_EVICT_TABLE = PAGER_SB_BASE + 0x800
PAGER_FRAME_COUNT = 37
#: The firmware's unified-TLB invalidate leaf (``mcr p15,0,rN,c8,c7,0`` then ``mov pc,lr``).
#: It runs it after every page-table edit (via the L2 updater 0x08001860). Unicorn's ARM926
#: model does not act on that CP15 op, so its softmmu TLB keeps stale VA->frame translations
#: — a later access to an evicted VA then silently hits the frame the pager has since reused,
#: clobbering the new occupant (the media-path object → the book/2nd-GME crash). We honour the
#: firmware's own invalidate by flushing Unicorn's TLB at this leaf.
TLB_INVALIDATE_LEAF = 0x0800_3310
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

    def __init__(self, machine: Machine, firmware: Firmware, audio: "AudioDma") -> None:
        self.m = machine
        self.uc = machine.uc
        self.audio = audio
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

        # Snapshot each page the instant it is evicted from its frame (scoped hooks). There
        # are two eviction sites that clear the frame's eviction-table entry and reuse it: the
        # nandboot refiner's :data:`PAGER_REFINER_CAPTURE`, and PROG's own eviction routine at
        # :data:`PAGER_PROG_EVICT` (0x08009634) — same register convention (r5=outgoing VA,
        # r4=frame index). The refiner one alone leaves the PROG path's evictions uncaptured,
        # dropping ~6 pages a run (incl. the heap allocator's free-list metadata → the media
        # double-alloc crash). Capture at both.
        for cap in (PAGER_REFINER_CAPTURE, PAGER_PROG_EVICT):
            uc.hook_add(UC_HOOK_CODE, self._on_refiner_evict, begin=cap, end=cap)
        # Model the hardware per-frame usage clock: when the pager records a page
        # in a frame (eviction-table write), stamp that frame so its LRU victim
        # scan (the 0x04010124 array) can rank frames instead of thrashing a few.
        uc.hook_add(UC_HOOK_MEM_WRITE, self._on_frame_map,
                    begin=PAGER_EVICT_TABLE, end=PAGER_EVICT_TABLE + PAGER_FRAME_COUNT * 4 - 1)
        # Restore page content at the handlers' return instructions (scoped hooks: they fire
        # only at these two PCs, so there is no per-instruction cost).
        for ret in (PABT_RET, DABT_RET):
            uc.hook_add(UC_HOOK_CODE, self._on_handler_return, begin=ret, end=ret)
        # Honour the firmware's own TLB invalidate: flush Unicorn's stale translations when the
        # firmware runs its CP15 TLBIALL leaf (Unicorn's ARM926 ignores the op itself).
        uc.hook_add(UC_HOOK_CODE, self._on_tlb_invalidate,
                    begin=TLB_INVALIDATE_LEAF, end=TLB_INVALIDATE_LEAF)
        # The machine's UC_HOOK_INTR delegates prefetch/data aborts (intno 3/4) here.
        self.m.set_abort_handler(self._on_abort)

    def _make_data_resident(self) -> None:
        """No-op: let the firmware's own page table + demand-pager own the work/data region.

        This used to pin ``[frame-threshold, dynamic-pool-base)`` resident (identity, AP=11),
        on the theory that the loader keeps data resident and demand-pages only code. But the
        firmware in fact **remaps pages in that window into its demand-paged frame pool** at
        runtime (e.g. the audio player object at VA 0x081487a0 → a pool frame), so the identity
        pin conflicts with the firmware's mapping: when such a page is evicted and re-faulted,
        the pinned-vs-paged split corrupts its content and the object's live writes are lost.
        That broke audio played from an idle chain (the AO's decoder source pointer was dropped
        on eviction). Leaving these pages to the firmware's page table + the demand-paging
        backing store is both more faithful (no intervention) and correct — it fixes the audio.
        """
        return

    def _unused_pin_data_resident(self) -> None:  # kept for reference; see _make_data_resident
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

    def _read_va_page(self, va: int, size: int) -> bytes:
        """Read up to a page's worth of a VA's content (single page; ``size`` must not
        cross the page boundary)."""
        pa = self._phys(va)
        if pa is not None:
            return bytes(self.uc.mem_read(pa, size))
        page = self.shadow.get(va & ~0xFFF)
        if page is not None:
            off = va & 0xFFF
            return page[off:off + size]
        return bytes(self.uc.mem_read(va, size))

    def read_va(self, va: int, size: int) -> bytes:
        """Read a virtual address's content, even if its page is paged out or remapped.

        Prefers the live physical frame (freshest), falls back to the demand-paging shadow
        (authoritative content of an evicted page), then to a plain physical read for
        identity/low addresses outside PROG's image. Splits the read at 4 KiB page
        boundaries so it is correct for spans crossing pages that the firmware mapped to
        discontiguous frames. Under the MMU a plain ``machine.read_u32(va)`` would read the
        wrong bytes for a remapped VA — inspection code must translate through here.
        """
        out = bytearray()
        while size > 0:
            n = min(size, ((va & ~0xFFF) + 0x1000) - va)
            out += self._read_va_page(va, n)
            va += n
            size -= n
        return bytes(out)

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

    def _on_tlb_invalidate(self, _uc: object, _addr: int, _size: int, _ud: object) -> None:
        """Flush Unicorn's softmmu TLB when the firmware executes its CP15 TLBIALL leaf.

        The firmware invalidates the whole unified TLB after every page-table edit; Unicorn's
        ARM926 model does not act on the ``mcr p15,c8,c7`` op, so we mirror it here to keep its
        cached VA->frame translations coherent with the page table the firmware just changed."""
        try:
            self.uc._Uc__ctl_w(UC_CTL_TLB_FLUSH)
        except (UcError, AttributeError):
            pass

    def _on_frame_map(self, _uc: object, _access: int, addr: int, _size: int,
                      _value: int, _ud: object) -> None:
        """Stamp the pager frame whose eviction-table entry was just written.

        The pager writes ``eviction_table[idx]`` when it maps a page into frame
        ``idx``; that is exactly when the hardware frame-usage clock advances for
        that frame, so we forward it to :meth:`AudioDma.touch_frame`. Observation
        only — the firmware's own write proceeds unchanged."""
        self.audio.touch_frame((addr - PAGER_EVICT_TABLE) // 4)

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
