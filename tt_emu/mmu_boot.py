"""Authentic MMU boot: run the firmware under its own page table + demand-pager.

``memory-map-and-boot.md`` §5 seeds the machine to PROG's *pre-init entry* and lets the
firmware boot itself. This module supplies the two things the silicon + romboot provide so
that boot runs under the **real** ARMv5 MMU (Unicorn honours it), with the DMA engines as
genuine physical bus masters — no page-table inversion in Python:

1. **Handoff through nandboot's MMU builder.** Before PROG runs, we execute nandboot
   ``init2`` (:attr:`MmuAnchors.init2`), which builds the authentic L1/L2 tables and sets
   ``SCTLR.M=1`` (Unicorn then translates every CPU access). This is the same code the pen
   runs; we just enter it directly instead of replaying init1/init3.

2. **Exception entry + a romboot-style backing store.** The tables start most pages
   no-access (``AP=00``); the firmware demand-refines them on abort. On a prefetch/data
   abort Unicorn raises ``UC_HOOK_INTR`` (intno 3/4) but does not vector ARMv5 exceptions,
   so we decode the fault address, write FAR, bank to abort mode, and jump to nandboot's
   *own* handler (``vector_base + 0x0C/0x10``) — which refines the page and retries. The
   refiner zeroes each frame it maps; the romboot would have loaded PROG's bytes into the
   compact physical layout it maps onto, so at the handler's return we write PROG's content
   into whichever physical frame the refiner chose. A per-VA **shadow** (PROG image +
   captured runtime writes) makes that survive frame eviction/re-zeroing. Romboot is
   hardware, so this backing store is part of the modelled machine, not a firmware hook.

Both pen generations boot this identical way — the same Anyka boot-ROM family (the vector
table even tags itself ``ANYKANB1`` on the 2nd-gen MT, ``ANYKANB0`` on the 1st-gen ZC3201).
The only per-firmware differences are a table of nandboot addresses and three small structural
constants; :class:`MmuAnchors` captures them, and :data:`MT_MMU` / :data:`ZC3201_MMU` are the
two instances. The **architectural** anchors (the abort vectors at ``vector_base + 0x0C/0x10``)
and the mechanism are shared; nothing in the firmware is patched or intercepted. See
``firmware-re/mmu-prototype`` for the MT derivation (``54_shadow.py`` and ``FINDINGS.md``); the
ZC3201 twin was located by CP15-op scan + control-flow tracing on its nandboot blob.
"""

from __future__ import annotations

import logging
import struct
from dataclasses import dataclass

import unicorn.arm_const as ac
from capstone import CS_ARCH_ARM, CS_MODE_ARM, CS_MODE_THUMB, Cs  # type: ignore[import-untyped]
from capstone.arm_const import ARM_OP_MEM, ARM_OP_REG  # type: ignore[import-untyped]
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

from .loader import Firmware

if TYPE_CHECKING:
    from .peripherals.audio import AudioDma
    from .machine import Machine

log = logging.getLogger(__name__)

# --- architectural constants (same for every ARMv5 image) -------------------------------
#: ARM exception-vector offsets from the vector base: prefetch abort +0x0C, data abort +0x10.
PABT_VEC_OFF = 0x0C
DABT_VEC_OFF = 0x10
#: CP15 register selectors (coproc, is64, sec, CRn, CRm, opc1, opc2).
_FAR = (15, 0, 0, 6, 0, 0, 0)    # fault address register (c6)
_TTBR0 = (15, 0, 0, 2, 0, 0, 0)  # translation table base 0
_SCTLR = (15, 0, 0, 1, 0, 0, 0)  # system control register (M-bit = MMU enable)
#: L2 small-page descriptor "mapping present" bits.
_PRESENT = 0xFF0

_CPSR_MODE_MASK = 0x1F
_MODE_ABORT = 0x17
_CPSR_I = 0x80
_CPSR_T = 0x20


@dataclass(frozen=True)
class MmuAnchors:
    """The per-firmware nandboot addresses + structural constants an MMU boot needs.

    Every field is a runtime address or count recovered from that firmware's nandboot
    (MT: the ``.upd`` disassembly + ``firmware-re/mmu-prototype``; ZC3201: CP15-op scan +
    control-flow tracing). The mechanism in :class:`MmuBoot` is identical across gens; only
    these values change. The three genuine structural deltas between MT and ZC3201 are
    ``frame_count`` (37 vs 38), ``usage_clock_off`` (0x124 vs 0x120), and the per-site
    eviction register convention in ``evict_sites`` (ZC3201's PROG-evict swaps VA/idx).
    """

    #: nandboot ``init2`` — builds the L1/L2 page tables and enables the MMU; standalone-
    #: callable in SVC mode with a resident stack and a mapped return sentinel.
    init2: int
    init2_stack: int
    init2_sentinel: int
    #: The abort handlers' retry instructions (``subs pc,lr,#4`` / ``#8``); we place page
    #: content here, just before the firmware resumes the faulting instruction.
    pabt_ret: int
    dabt_ret: int
    #: Abort-mode banked stack (from the reset handler's literal).
    sp_abt: int
    #: The demand-pager frame pool: frame ``i`` is at ``frame_base + i*0x1000``.
    frame_base: int
    frame_count: int
    #: The pager's eviction table: ``evict_table[idx]`` = the page VA resident in frame idx
    #: (0 = free). The pager writes it when it maps a page in — we watch those writes to
    #: stamp the hardware frame-usage clock (:meth:`AudioDma.touch_frame`).
    evict_table: int
    #: Eviction snapshot sites: ``(pc, va_reg, idx_reg)`` — at ``pc`` the frame still holds
    #: the outgoing page whose VA is in ``va_reg`` and frame index in ``idx_reg`` (0 VA = free
    #: frame). MT uses ``(R5, R4)`` at both sites; ZC3201's PROG-evict swaps them to ``(R4, R5)``.
    evict_sites: tuple[tuple[int, int, int], ...]
    #: The firmware's unified-TLB invalidate leaf (``mcr p15,0,rN,c8,c7,0`` then ``mov pc,lr``),
    #: run after every page-table edit — we flush Unicorn's TLB here (its ARM926 ignores the op).
    tlb_invalidate_leaf: int
    #: Offset within the 0x04010000 block of the pager's per-frame usage-clock array
    #: (:data:`~tt_emu.peripherals.audio.PAGER_FRAME_LRU_OFF`), read by the LRU victim scan.
    usage_clock_off: int
    #: PROG load base (= the demand window start) and the top of the demand-paged domain.
    prog_load: int
    demand_top: int
    #: Where nandboot's exception vectors live (both gens: 0x08000000). The abort vectors are
    #: ``vector_base + 0x0C/0x10`` (architectural); we jump to the vector and let its branch
    #: route to the firmware's own handler.
    vector_base: int = 0x0800_0000

    @property
    def pabt_vec(self) -> int:
        return self.vector_base + PABT_VEC_OFF

    @property
    def dabt_vec(self) -> int:
        return self.vector_base + DABT_VEC_OFF


#: 2nd-gen MT (ZC3202N). The reference derivation — see ``firmware-re/mmu-prototype``.
MT_MMU = MmuAnchors(
    init2=0x0800_18D4,
    init2_stack=0x0800_8400,
    init2_sentinel=0x0800_7000,
    pabt_ret=0x0800_0190,
    dabt_ret=0x0800_01C4,
    sp_abt=0x0800_6000,
    frame_base=0x0800_9000,
    frame_count=37,
    evict_table=0x0800_7F8C + 0x800,  # SB base + 0x800
    evict_sites=(
        (0x0800_14F8, ac.UC_ARM_REG_R5, ac.UC_ARM_REG_R4),  # nandboot refiner capture
        (0x0800_9634, ac.UC_ARM_REG_R5, ac.UC_ARM_REG_R4),  # PROG's own page-evict
    ),
    tlb_invalidate_leaf=0x0800_3310,
    usage_clock_off=0x124,
    prog_load=0x0800_9000,
    demand_top=0x0840_0000,
)

#: 1st-gen ZC3201. Same boot architecture, different offsets + three structural deltas
#: (frame_count 38, usage_clock_off 0x120, PROG-evict register convention swapped).
ZC3201_MMU = MmuAnchors(
    init2=0x0800_1F80,
    init2_stack=0x0800_8000,
    init2_sentinel=0x0800_6000,  # resident, clear of the SB globals 0x080070e0..0x08007578
    pabt_ret=0x0800_01AC,
    dabt_ret=0x0800_01E0,
    sp_abt=0x0800_5000,
    frame_base=0x0800_8000,
    frame_count=38,
    evict_table=0x0800_70E0 + 0x400,  # SB base + 0x400 (MT is +0x800)
    evict_sites=(
        (0x0800_1C1C, ac.UC_ARM_REG_R5, ac.UC_ARM_REG_R4),  # refiner capture (same convention)
        (0x0800_8814, ac.UC_ARM_REG_R4, ac.UC_ARM_REG_R5),  # PROG's page-evict — VA/idx SWAPPED
    ),
    tlb_invalidate_leaf=0x0800_354C,
    usage_clock_off=0x120,
    prog_load=0x0800_8000,
    demand_top=0x0820_0000,
)


class MmuBoot:
    """Runs nandboot's MMU builder, then services aborts + backs demand-paging.

    Instantiate after the artifacts and boot seeds are in place, call :meth:`setup`, then
    seed PROG's entry state and run the machine normally. ``anchors`` selects the firmware
    (:data:`MT_MMU` / :data:`ZC3201_MMU`). The abort handler is wired into
    :class:`~tt_emu.machine.Machine` so the machine's single ``UC_HOOK_INTR`` still owns
    interrupt dispatch (SVC vs. abort).
    """

    def __init__(
        self,
        machine: "Machine",
        firmware: Firmware,
        audio: "AudioDma",
        anchors: MmuAnchors = MT_MMU,
    ) -> None:
        self.m = machine
        self.uc = machine.uc
        self.audio = audio
        self.a = anchors
        self.prog = firmware.prog.data
        self.prog_hi = anchors.prog_load + len(self.prog)

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
        a = self.a
        # init2 runs with a low always-accessible stack and returns to a sentinel.
        uc.reg_write(UC_ARM_REG_SP, a.init2_stack)
        uc.reg_write(UC_ARM_REG_LR, a.init2_sentinel)
        uc.reg_write(UC_ARM_REG_CPSR, 0x13)  # SVC
        uc.emu_start(a.init2, a.init2_sentinel, count=3_000_000)
        sctlr = uc.reg_read(UC_ARM_REG_CP_REG, _SCTLR)
        if not sctlr & 1:
            raise RuntimeError(f"init2 did not enable the MMU (SCTLR={sctlr:#010x})")
        self.l1_base = uc.reg_read(UC_ARM_REG_CP_REG, _TTBR0) & 0xFFFF_C000
        log.info("MMU boot: init2 built the page table, SCTLR=%#010x TTBR0=%#010x",
                 sctlr, self.l1_base)

        # Seed the abort-mode banked stack the fault handlers push onto.
        cpsr = uc.reg_read(UC_ARM_REG_CPSR)
        uc.reg_write(UC_ARM_REG_CPSR, (cpsr & ~_CPSR_MODE_MASK) | _MODE_ABORT)
        uc.reg_write(UC_ARM_REG_SP, a.sp_abt)
        uc.reg_write(UC_ARM_REG_CPSR, cpsr)

        # Snapshot each page the instant it is evicted from its frame (scoped hooks). There
        # are two eviction sites that clear the frame's eviction-table entry and reuse it: the
        # nandboot refiner and PROG's own eviction routine. Each carries the outgoing page's
        # VA / frame index in its own register pair (``evict_sites``) — MT uses (r5, r4) at
        # both; ZC3201's PROG-evict swaps them. Capturing at both is essential: the refiner
        # one alone leaves the PROG path's evictions uncaptured (dropping the heap allocator's
        # free-list metadata → the media double-alloc crash).
        for pc, va_reg, idx_reg in a.evict_sites:
            uc.hook_add(UC_HOOK_CODE, self._make_evict_hook(va_reg, idx_reg), begin=pc, end=pc)
        # Model the hardware per-frame usage clock: when the pager records a page
        # in a frame (eviction-table write), stamp that frame so its LRU victim
        # scan (the usage-clock array) can rank frames instead of thrashing a few.
        uc.hook_add(UC_HOOK_MEM_WRITE, self._on_frame_map,
                    begin=a.evict_table, end=a.evict_table + a.frame_count * 4 - 1)
        # Restore page content at the handlers' return instructions (scoped hooks: they fire
        # only at these two PCs, so there is no per-instruction cost).
        for ret in (a.pabt_ret, a.dabt_ret):
            uc.hook_add(UC_HOOK_CODE, self._on_handler_return, begin=ret, end=ret)
        # Honour the firmware's own TLB invalidate: flush Unicorn's stale translations when the
        # firmware runs its CP15 TLBIALL leaf (Unicorn's ARM926 ignores the op itself).
        uc.hook_add(UC_HOOK_CODE, self._on_tlb_invalidate,
                    begin=a.tlb_invalidate_leaf, end=a.tlb_invalidate_leaf)
        # The machine's UC_HOOK_INTR delegates prefetch/data aborts (intno 3/4) here.
        self.m.set_abort_handler(self._on_abort)

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

    def _make_evict_hook(self, va_reg: int, idx_reg: int):
        """A capture hook bound to one eviction site's ``(va_reg, idx_reg)`` convention.

        At the site the frame still holds the outgoing page's live content — copy it into the
        shadow before the pager reuses the frame, keyed by the firmware's own eviction record
        (VA in ``va_reg``, 0 if the frame is free; frame index in ``idx_reg``). Different sites
        use different register pairs (MT: (r5,r4) at both; ZC3201's PROG-evict swaps them), so
        each site gets its own bound hook.
        """
        def hook(_uc: object, _addr: int, _size: int, _ud: object) -> None:
            old_va = self.uc.reg_read(va_reg)
            if old_va != 0:
                frame = self.a.frame_base + self.uc.reg_read(idx_reg) * 0x1000
                self.shadow[old_va & ~0xFFF] = bytes(self.uc.mem_read(frame, 0x1000))
            # Stop-safe pace point: re-firing on resume just re-snapshots the same
            # frame content (idempotent).
            self.m.maybe_pace_stop()
        return hook

    def _on_tlb_invalidate(self, _uc: object, _addr: int, _size: int, _ud: object) -> None:
        """Flush Unicorn's softmmu TLB when the firmware executes its CP15 TLBIALL leaf.

        The firmware invalidates the whole unified TLB after every page-table edit; Unicorn's
        ARM926 model does not act on the ``mcr p15,c8,c7`` op, so we mirror it here to keep its
        cached VA->frame translations coherent with the page table the firmware just changed."""
        try:
            # name-mangled private helper; exists at runtime, invisible to the stubs
            self.uc._Uc__ctl_w(UC_CTL_TLB_FLUSH)  # type: ignore[attr-defined]
        except (UcError, AttributeError):
            pass
        # Stop-safe pace point: a re-fired flush on resume is idempotent, and
        # the leaf runs after every page-table edit — dense coverage of the
        # demand-paging-heavy phases (boot, first-touch decode).
        self.m.maybe_pace_stop()

    def _on_frame_map(self, _uc: object, _access: int, addr: int, _size: int,
                      _value: int, _ud: object) -> None:
        """Stamp the pager frame whose eviction-table entry was just written.

        The pager writes ``eviction_table[idx]`` when it maps a page into frame
        ``idx``; that is exactly when the hardware frame-usage clock advances for
        that frame, so we forward it to :meth:`AudioDma.touch_frame`. Observation
        only — the firmware's own write proceeds unchanged."""
        self.audio.touch_frame((addr - self.a.evict_table) // 4)

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
            vec, lroff = self.a.pabt_vec, 4
        else:
            fault = self._fault_addr(pc, bool(cpsr & _CPSR_T))
            vec, lroff = self.a.dabt_vec, 8
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
        if not (self.a.prog_load <= vp < self.a.demand_top):
            return
        pa = self._cur_pa(vp)
        if pa is None:
            return
        content = self.shadow.get(vp)
        if content is None and vp < self.prog_hi:
            off = vp - self.a.prog_load
            content = self.prog[off:off + 0x1000].ljust(0x1000, b"\x00")
        if content is not None:
            self.uc.mem_write(pa, content)
        try:
            self.uc.ctl_remove_cache(vp, vp + 0x1000)
        except UcError:
            pass
