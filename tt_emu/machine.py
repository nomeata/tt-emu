"""The machine core: ARM926EJ-S CPU, memory map, MMIO dispatch, IRQ delivery.

Implements:

* the CPU and address-space map of ``memory-map-and-boot.md`` §1/§2 (ARM926EJ-S =
  ARMv5TEJ, little-endian). The firmware boots under its **own** MMU: nandboot ``init2``
  builds the page table and Unicorn honours it (see :mod:`tt_emu.mmu_boot`); prefetch/data
  aborts are delegated to the MMU-boot layer for demand-paging. CP15 writes the guest issues
  during PROG (cache/TLB maintenance) are still neutralized per §1.2;
* the MMIO dispatch layer: peripherals register address ranges
  (:class:`~tt_emu.peripheral.Peripheral`); the whole peripheral window is
  backed by RAM-like scratch registers as the §5.3 default, with registered
  peripherals overriding their ranges;
* IRQ delivery per ``interrupts-and-timers.md`` §3: gate on
  ``(pending & enable) != 0`` (asked of the interrupt controller), CPSR.I == 0,
  and not already in IRQ mode; then the architectural exception entry to the
  loaded image's IRQ vector 0x08000018;
* semihosting ``svc 0xab`` logging (``memory-map-and-boot.md`` §1.1).
"""

from __future__ import annotations

import logging
import struct
import threading
import time
from bisect import bisect_right
from dataclasses import dataclass
from typing import Callable, Protocol

__all__ = ["Machine", "MachineConfig", "RunResult"]

from unicorn import (
    UC_ARCH_ARM,
    UC_ERR_EXCEPTION,
    UC_HOOK_CODE,
    UC_HOOK_INTR,
    UC_HOOK_MEM_READ,
    UC_HOOK_MEM_UNMAPPED,
    UC_HOOK_MEM_WRITE,
    UC_MEM_WRITE_UNMAPPED,
    UC_MODE_ARM,
    UC_MODE_LITTLE_ENDIAN,
    Uc,
    UcError,
)
from unicorn.arm_const import (
    UC_ARM_REG_CPSR,
    UC_ARM_REG_LR,
    UC_ARM_REG_PC,
    UC_ARM_REG_R0,
    UC_ARM_REG_R1,
    UC_ARM_REG_SP,
    UC_ARM_REG_SPSR,
    UC_CPU_ARM_926,
)

from .peripheral import Peripheral

log = logging.getLogger(__name__)

# --- Address map (memory-map-and-boot.md §2) -------------------------------------

#: MMIO peripheral register window (datasheet 0x04000000–0x040AFFFF; safe cover).
MMIO_BASE = 0x0400_0000
MMIO_SIZE = 0x0020_0000

#: The SoC "core" register page (SysCon, IntC/timer, GPIO, battery ADC — all at
#: ``0x040000xx``). It holds the firmware's hottest polled registers: GPIO_IN
#: (the button/OID input word) and the GPIO_OUT/GPIO_DIR pair the OID two-wire
#: link bit-bangs, together ~4.3M of the ~5.5M MMIO accesses on a play session,
#: overwhelmingly *reads*. An MMIO read costs a full C→Python callback round
#: trip (~36× a native load, measured), so those poll loops crawl. The machine
#: therefore backs this one page with **real memory**: reads run in TCG with no
#: callback (the hardware model exposing the register as memory — the pen's
#: silicon does exactly this), while writes — which carry side effects (OID
#: clock edges, the power-hold latch) — are caught by a single
#: ``UC_HOOK_MEM_WRITE`` and dispatched to the owning peripheral, and the few
#: registers whose read value is *not* the last write (constants, self-clearing
#: latches, computed status) keep a targeted ``UC_HOOK_MEM_READ``.
CORE_PAGE_SIZE = 0x1000

#: Base of the pen's main physical RAM (``memory-map-and-boot.md`` §2). This is
#: also the base of the DMA engine's 256-KiB physical-address aperture (a
#: hardware property, proven at runtime by :meth:`Machine.read_phys` resolving
#: every content submit — ``audio-dac-dma.md``); the source register's low 18
#: bits index into it.
PHYS_RAM_BASE = 0x0800_0000

#: RAM / RAM-like regions: (base, size, description).  Sizes are the emulator's
#: safe mapping cover (§2).  The two HW-config blocks are "RAM-like stub
#: suffices" per §2, so they are plain RAM here.
RAM_REGIONS: tuple[tuple[int, int, str], ...] = (
    (0x0000_0000, 0x1_0000, "mask-ROM area (zero stub; from-entry boot never runs it)"),
    (0x0500_0000, 0x2_0000, "HW config block 0 (mask-ROM scratch, RAM-like)"),
    (0x0600_0000, 0x2_0000, "HW config block 1 (mask-ROM scratch, RAM-like)"),
    (0x07FF_0000, 0x1_0000, "resident HAL / boot-SRAM window"),
    (0x0800_0000, 0x40_0000, "main RAM (boot blob, low globals, PROG)"),
    (0x0840_0000, 0x4_0000, "stack/heap headroom (SVC stack top 0x08420000)"),
)

# --- CPU constants (interrupts-and-timers.md §3, memory-map-and-boot.md §5.2) ----

#: The loaded image's IRQ vector (nandboot vector table at 0x08000000, +0x18).
IRQ_VECTOR = 0x0800_0018
#: IRQ-mode stack top set on first delivery (§3: emulator-chosen, proven).
#: Must lie inside the pen's real 4-MiB RAM window — the firmware's Utl_UStr*
#: routines reject pointers outside [0x08000000, 0x08400000] (see
#: boot.SVC_STACK_TOP); 64 KiB below the SVC stack top keeps them disjoint.
IRQ_STACK_TOP = 0x083F_0000

CPSR_I = 0x80  # IRQ disable
CPSR_T = 0x20  # Thumb state
MODE_MASK = 0x1F
MODE_IRQ = 0x12

#: ``until`` sentinel for emu_start that no 32-bit PC can reach.
_NEVER = 1 << 40

#: Nominal timer ticks per second (one tick = the 20 ms timer period,
#: ``interrupts-and-timers.md`` §4) — so ``instructions_per_tick * 50`` is the
#: modelled CPU's instruction rate, the wall-clock anchor of realtime pacing.
TICKS_PER_SECOND = 50

#: Realtime pacing: how long a count-free chunk may go unserved before the
#: pacer breaks it with the IRQ-deferred async stop (see
#: :meth:`Machine._run_realtime`) — the serve-rich phase that justified
#: count-free mode has evidently ended (typically a busy→idle transition).
_PACE_UNSERVED_STOP_SECONDS = 0.06
#: Realtime pacing: instruction count of count-paced chunks (smaller than the
#: deterministic chunk: IRQs deliver only at chunk ends, and serve-sparse
#: phases can run slowly — a small count keeps ticks and taps responsive).
_PACE_COUNT_CHUNK = 10_000
#: Realtime pacing: emulated ticks of count-paced warm-up before count-free
#: chunks are allowed — boot's calibrated self-tests (battery sampling, clock
#: probes) run within this window and need deterministic clock-vs-execution
#: proportions (100 ticks = 2 emulated seconds; book mode is reached later).
_PACE_WARMUP_TICKS = 100
#: Realtime pacing: count-free chunks advance the clock at this multiple of
#: wall time. The firmware schedules its audio decode by its own timer tick,
#: so PCM production tracks the emulated clock — at exactly 1.0x, any count
#: interlude leaves production a few percent behind the speaker's fixed
#: drain and every sound eventually stutters. A slight overdrive gives
#: production headroom; it is far below the guest's actual count-free speed
#: (~10x wall), and a sound's lead is bounded by its own length.
_PACE_FREE_OVERDRIVE = 1.1


@dataclass
class MachineConfig:
    """Tunables of the machine model (all cross-platform, no wall-clock use)."""

    #: Emulated instructions per 20 ms timer period — the pacing unit of
    #: ``interrupts-and-timers.md`` §7.4 ("~20,000 emulated instructions per
    #: tick is a working cadence").
    instructions_per_tick: int = 20_000
    #: Instructions per ``emu_start`` chunk; bounds IRQ-delivery latency.
    #: ``None`` (the default) scales with the tick: ``instructions_per_tick
    #: // 10``, i.e. IRQs are delivered within 10% of a timer period — the
    #: same 2000-instruction chunk as before at the boot cadence of 20k/tick,
    #: but far fewer Python-side chunk boundaries at the session cadence of
    #: 1M/tick. Set explicitly to pin an exact chunk size.
    chunk_instructions: int | None = None

    @property
    def effective_chunk(self) -> int:
        """The chunk size :meth:`Machine.run` uses (see ``chunk_instructions``)."""
        if self.chunk_instructions is not None:
            return self.chunk_instructions
        return max(1, self.instructions_per_tick // 10)
    #: Log every MMIO access to an address the first few times (diagnostics).
    trace_mmio: bool = False
    #: Max traced accesses per (address, direction) before muting.
    trace_mmio_limit: int = 8
    #: Back the SoC core register page with real memory so the firmware's
    #: hot busy-poll reads execute natively in TCG (see :data:`CORE_PAGE_SIZE`).
    #: ``False`` restores the uniform all-MMIO window (the pre-optimization
    #: model) — kept as a switch for A/B measurement and as a safe fallback.
    ram_backed_core: bool = True
    #: How chunks are paced — the speed/determinism trade-off of :meth:`Machine.run`:
    #: * ``"deterministic"`` (default): each chunk is exactly ``effective_chunk``
    #:   instructions (``emu_start(count=...)``), so a run is bit-for-bit
    #:   reproducible — the right mode for scripted tests. The count is however
    #:   implemented by Unicorn as a *global per-instruction* code hook, and once
    #:   that exists every executed instruction also walks the whole registered
    #:   code-hook list — measured at roughly a 10–20× throughput cost.
    #: * ``"realtime"``: chunks run count-free at full TCG speed and are ended by
    #:   a pacer thread calling ``emu_stop()`` every ``effective_chunk``-worth of
    #:   wall time; the clock advances by wall time at the modelled CPU rate
    #:   (``instructions_per_tick`` per 20 ms). The emulated pen runs on the real
    #:   pen's timeline — surplus host speed is absorbed by the firmware's own
    #:   idle loop, exactly as on real silicon — at the cost of run-to-run
    #:   determinism. The right mode for interactive use.
    pacing: str = "deterministic"
    #: How the DAC signals buffer completion. The firmware feeds audio only as
    #: fast as each submitted buffer "drains", so this sets the audio pace:
    #: * ``"fast"`` (default): completion is signalled as soon as the run loop
    #:   can deliver it, so the firmware produces audio at the emulator's own
    #:   (decode-bound) speed. The captured PCM is identical; it just reaches
    #:   the speaker with far less lag. Best for listening.
    #: * ``"faithful"``: completion is paced to the buffer's true playback
    #:   duration (``len/(4·rate)`` in emulated time), so the firmware runs on
    #:   the pen's real audio timeline — for reproducing timing-sensitive
    #:   behaviour at the cost of real-time-slow playback.
    dac_pacing: str = "fast"

    def __post_init__(self) -> None:
        if self.dac_pacing not in ("fast", "faithful"):
            raise ValueError(
                f"dac_pacing must be 'fast' or 'faithful', got {self.dac_pacing!r}"
            )
        if self.pacing not in ("deterministic", "realtime"):
            raise ValueError(
                f"pacing must be 'deterministic' or 'realtime', got {self.pacing!r}"
            )


@dataclass
class RunResult:
    """Outcome of a :meth:`Machine.run` call."""

    reason: str
    instructions: int
    pc: int


class IrqController(Protocol):
    """What the machine needs from the interrupt-controller peripheral."""

    def irq_asserted(self) -> bool:
        """True while ``(INT_PENDING & INT_ENABLE) != 0`` (level semantics)."""
        ...


class Machine:
    """CPU + memory + MMIO dispatch + IRQ delivery.

    Peripherals are added with :meth:`add_peripheral`; the interrupt controller
    additionally gets assigned to :attr:`intc` so the run loop can gate IRQ
    delivery on it.
    """

    def __init__(self, config: MachineConfig | None = None) -> None:
        self.config = config or MachineConfig()
        #: Machine clock in (approximately) executed instructions; the time
        #: unit peripherals pace themselves by (interrupts-and-timers.md §7.4).
        self.clock = 0
        #: The interrupt controller peripheral (set by the machine builder).
        self.intc: IrqController | None = None
        self.irqs_delivered = 0
        #: The MMU-boot layer (set by the machine builder, after the MMU is enabled).
        #: When present, ``read_u8/u16/u32`` translate firmware VIRTUAL addresses through
        #: the page table (the firmware maps many globals to non-identity frames); DMA and
        #: raw physical access use :meth:`read_phys` / :meth:`read_bytes` instead.
        self.mmu = None

        self._peripherals: list[Peripheral] = []
        self._ticking: list[Peripheral] = []  # peripherals that override tick()
        self._region_starts: list[int] = []
        self._regions: list[tuple[int, int, Peripheral]] = []  # (start, end, periph)
        #: Per-offset dispatch cache: offset -> (periph|None, read, write, base)
        #: — resolves the bisect region lookup once per distinct MMIO address
        #: (the firmware polls a small, fixed register set millions of times).
        self._mmio_cache: dict[int, tuple[Peripheral | None, Callable | None, Callable | None, int]] = {}
        self._scratch = bytearray(MMIO_SIZE)  # §5.3 RAM-like fallback registers
        self._trace_counts: dict[tuple[int, bool], int] = {}
        self._stop_reason: str | None = None
        self._fault: str | None = None
        self._irq_sp_initialized = False
        self._neutralized_cp15: set[int] = set()
        #: Prefetch/data-abort handler (installed by the MMU-boot layer; see
        #: :meth:`set_abort_handler`). ``None`` leaves aborts to the default path.
        self._abort_handler: Callable[[int], None] | None = None
        #: Realtime pacing: a chunk end is due (set by the pacer thread each
        #: interval, served synchronously by :meth:`maybe_pace_stop` from
        #: stop-safe callback contexts — see :meth:`_run_realtime`).
        self._pace_due = False
        #: Count of pace stops served from callbacks — the run loop's signal
        #: that serve points are dense enough to run count-free.
        self._pace_serves = 0
        #: Diagnostics seam: when set to a deque, the realtime run loop
        #: appends ``(mode, elapsed_s, served, skip_irq)`` per chunk. ``None``
        #: (the default) costs one attribute check per chunk.
        self.pace_trace: object | None = None
        #: The chunk just ended at a store boundary (or the pacer's async
        #: stop): defer this boundary's IRQ delivery. The stop may have
        #: rolled back an in-flight store; an ISR run before its re-execution
        #: would be clobbered by the store's stale read-modify-write value
        #: (observed: the firmware's INT_ENABLE restore undoing the ISR's
        #: mask update, wedging all interrupt delivery). With no ISR
        #: interleaved, the rolled-back store re-executes immediately and
        #: model/RAM reconverge; the run loop delivers the IRQ at the end of
        #: the tiny follow-up count chunk instead.
        self._pace_skip_irq = False
        #: MMIO offsets (MMIO_BASE-relative) whose *reads* are declared pure
        #: and may serve a pace stop from the read callback
        #: (:meth:`add_pace_serve_mmio`).
        self._pace_serve_mmio: set[int] = set()

        self.uc = Uc(UC_ARCH_ARM, UC_MODE_ARM | UC_MODE_LITTLE_ENDIAN)
        self.uc.ctl_set_cpu_model(UC_CPU_ARM_926)
        for base, size, _desc in RAM_REGIONS:
            self.uc.mem_map(base, size)
        #: MMIO callbacks receive an offset relative to the mmio_map base; when
        #: the core page is real memory the map starts one page in, so add it
        #: back to recover the MMIO_BASE-relative offset the dispatch uses.
        self._mmio_off_adjust = 0
        if self.config.ram_backed_core:
            self.uc.mem_map(MMIO_BASE, CORE_PAGE_SIZE)  # core page = real memory
            self.uc.mmio_map(MMIO_BASE + CORE_PAGE_SIZE, MMIO_SIZE - CORE_PAGE_SIZE,
                             self._mmio_read, None, self._mmio_write, None)
            self._mmio_off_adjust = CORE_PAGE_SIZE
            # One write hook over the whole core page dispatches to the owning
            # peripheral (side effects); the store itself lands in the backing
            # RAM, which is exactly the read-back value for storage registers.
            self.uc.hook_add(UC_HOOK_MEM_WRITE, self._core_write,
                             begin=MMIO_BASE, end=MMIO_BASE + CORE_PAGE_SIZE - 1)
        else:
            self.uc.mmio_map(MMIO_BASE, MMIO_SIZE,
                             self._mmio_read, None, self._mmio_write, None)
        self.uc.hook_add(UC_HOOK_INTR, self._on_intr)
        self.uc.hook_add(UC_HOOK_MEM_UNMAPPED, self._on_unmapped)

    # --- peripheral registration --------------------------------------------------

    def add_peripheral(self, peripheral: Peripheral) -> None:
        """Register a peripheral: claim its MMIO regions and attach it."""
        for region in peripheral.regions:
            for start, end, other in self._regions:
                if region.base < end and start < region.end:
                    raise ValueError(
                        f"{peripheral.name}: region {region.base:#x}+{region.size:#x} "
                        f"overlaps {other.name}"
                    )
            self._regions.append((region.base, region.end, peripheral))
        self._regions.sort(key=lambda r: r[0])
        self._region_starts = [r[0] for r in self._regions]
        self._mmio_cache.clear()  # region map changed; re-resolve lazily
        self._peripherals.append(peripheral)
        if type(peripheral).tick is not Peripheral.tick:
            self._ticking.append(peripheral)
        peripheral.attach(self)
        if self.config.ram_backed_core:
            self._wire_ram_backed(peripheral)

    def _in_core_page(self, addr: int) -> bool:
        return MMIO_BASE <= addr < MMIO_BASE + CORE_PAGE_SIZE

    def _wire_ram_backed(self, peripheral: Peripheral) -> None:
        """Install read hooks + seed the backing RAM for a core-page peripheral."""
        if not any(self._in_core_page(r.base) for r in peripheral.regions):
            return
        for addr in peripheral.read_hook_addrs():
            self.uc.hook_add(UC_HOOK_MEM_READ, self._core_read_hook,
                             begin=addr, end=addr + 3)
        peripheral.seed_ram(self.poke_core_reg)

    def poke_core_reg(self, addr: int, value: int) -> None:
        """Write a 32-bit register value straight into the core-page backing RAM.

        Used to seed power-on values and to push a *computed* native register
        (e.g. the recomposed GPIO input word) when the model changes it without
        a firmware write. A host-side store — it does not re-enter the write
        hook.
        """
        self.uc.mem_write(addr, struct.pack("<I", value & 0xFFFFFFFF))

    @property
    def ram_page_active(self) -> bool:
        """True when the SoC core register page is backed by real memory
        (native reads); peripherals in that page use it to decide whether to
        push computed read values into RAM (see :meth:`poke_core_reg`)."""
        return self.config.ram_backed_core

    def _find_peripheral(self, addr: int) -> Peripheral | None:
        i = bisect_right(self._region_starts, addr) - 1
        if i >= 0:
            start, end, periph = self._regions[i]
            if addr < end:
                return periph
        return None

    # --- MMIO dispatch -------------------------------------------------------------

    def _mmio_resolve(self, offset: int) -> tuple[Peripheral | None, Callable | None, Callable | None, int]:
        """Resolve + cache the dispatch entry for one MMIO ``offset`` (hot path)."""
        periph = self._find_peripheral(MMIO_BASE + offset)
        entry: tuple[Peripheral | None, Callable | None, Callable | None, int]
        if periph is None:
            entry = (None, None, None, 0)
        else:
            entry = (periph, periph.read, periph.write, MMIO_BASE - periph.base)
        self._mmio_cache[offset] = entry
        return entry

    def _mmio_read(self, _uc: Uc, offset: int, size: int, _ud: object) -> int:
        offset += self._mmio_off_adjust
        entry = self._mmio_cache.get(offset)
        if entry is None:
            entry = self._mmio_resolve(offset)
        periph, read, _write, rel = entry
        if read is not None:
            value = read(rel + offset, size)
        else:
            value = int.from_bytes(self._scratch[offset : offset + size], "little")
        if self.config.trace_mmio:
            self._trace(MMIO_BASE + offset, size, value, periph, is_write=False)
        if self._pace_due and offset in self._pace_serve_mmio:
            # A declared-pure poll register: stopping here rolls the load back
            # and replays a side-effect-free read (see add_pace_serve_mmio).
            self.maybe_pace_stop()
        return value

    def _mmio_write(self, _uc: Uc, offset: int, size: int, value: int, _ud: object) -> None:
        offset += self._mmio_off_adjust
        entry = self._mmio_cache.get(offset)
        if entry is None:
            entry = self._mmio_resolve(offset)
        periph, _read, write, rel = entry
        if self.config.trace_mmio:
            self._trace(MMIO_BASE + offset, size, value, periph, is_write=True)
        if write is not None:
            write(rel + offset, size, value)
        else:
            self._scratch[offset : offset + size] = value.to_bytes(size, "little")

    # --- RAM-backed core page (see CORE_PAGE_SIZE) ---------------------------------

    def _core_write(self, _uc: Uc, _access: int, addr: int, size: int,
                    value: int, _ud: object) -> None:
        """UC_HOOK_MEM_WRITE over the core page: run the peripheral's write side
        effects. The CPU store still lands in the backing RAM afterwards (that
        raw value is the read-back for storage registers); peripherals whose
        read value differs keep a read hook, so what lands here is moot for them.

        NEVER a pace point. A stop from inside a memory-write hook rewinds
        the CPU to the start of the translation block — several instructions
        before the store — while the store's memory effect PERSISTS. The
        resumed block then re-executes from the top, and any earlier load of
        the stored location re-reads the store's own committed value: rolled
        back code reading its own future. Measured against the firmware's
        interrupt-mask helper (``LDR old←EN; STR 0→EN; save old``): the serve
        on the STR resumed at the LDR, which re-read EN as 0, so the saved
        "old" — and every later restore — became 0, permanently masking all
        interrupts. No IRQ interleaving involved; the corruption is purely
        mechanical, so no deferral scheme can fix it.
        """
        offset = addr - MMIO_BASE
        entry = self._mmio_cache.get(offset)
        if entry is None:
            entry = self._mmio_resolve(offset)
        periph, _read, write, rel = entry
        if self.config.trace_mmio:
            self._trace(addr, size, value, periph, is_write=True)
        if write is not None:
            write(rel + offset, size, value)

    def _core_read_hook(self, uc: Uc, _access: int, addr: int, size: int,
                        _value: int, _ud: object) -> None:
        """UC_HOOK_MEM_READ for a behavioural core register (constant,
        self-clearing, or computed): serve the model's value by writing it into
        the backing RAM before the triggering load samples it (the read hook
        fires ahead of the load, and a host store from the hook feeds it).

        A pace point only for registers the peripheral declared read-pure
        (:meth:`add_pace_serve_mmio`) — e.g. the timer-latch status the
        firmware's idle loop polls while waiting for a tick, which makes the
        wait loop itself the chunk end that delivers the tick. Undeclared
        hooked registers may be *self-clearing*: a stop would roll the load
        back after the clearing read ran, and the replayed read would lose
        the event.
        """
        offset = addr - MMIO_BASE
        entry = self._mmio_cache.get(offset)
        if entry is None:
            entry = self._mmio_resolve(offset)
        periph, read, _write, rel = entry
        if read is None:
            return
        value = read(rel + offset, size)
        uc.mem_write(addr, value.to_bytes(size, "little"))
        if self.config.trace_mmio:
            self._trace(addr, size, value, periph, is_write=False)
        if self._pace_due and offset in self._pace_serve_mmio:
            self.maybe_pace_stop()

    def _trace(self, addr: int, size: int, value: int, periph: Peripheral | None, *, is_write: bool) -> None:
        key = (addr, is_write)
        count = self._trace_counts.get(key, 0)
        if count < self.config.trace_mmio_limit:
            self._trace_counts[key] = count + 1
            who = periph.name if periph else "scratch"
            arrow = "<=" if is_write else "=>"
            log.debug("mmio %-8s %s [%#010x] %s %#x (size %d)",
                      who, "wr" if is_write else "rd", addr, arrow, value, size)

    # --- memory helpers ------------------------------------------------------------

    def read_bytes(self, addr: int, size: int) -> bytes:
        return bytes(self.uc.mem_read(addr, size))

    # --- realtime pacing: synchronous chunk ends ------------------------------------

    def add_pace_serve_mmio(self, addr: int) -> None:
        """Declare a register whose *read* is pure, as a pace-serve point.

        Applies to both dispatch paths: the Python-dispatched MMIO window and
        read-hooked core-page registers. A pace stop taken from the read
        callback rolls the in-flight load back and **replays the read** on
        resume — only registers whose read has no model side effect
        (ready/status polls, latched values; never self-clearing reads) may
        be declared. Note that only a rolled-back *store* can lose an update
        to an interleaved ISR; a rolled-back load re-reads fresh. This is
        what keeps realtime pacing live through the phases the exception-hook
        serves never see — the NFC ready poll runs between every NAND
        micro-op of a stream, the DAC submit gate throughout playback, and
        the timer-latch status is the firmware's idle wait loop — with no
        runtime hook mutation (measured to crash under load) and no cost off
        the pace path (a set lookup only while a stop is due).
        """
        self._pace_serve_mmio.add(addr - MMIO_BASE)

    def maybe_pace_stop(self) -> None:
        """Serve a pending realtime pace stop from a stop-safe callback.

        An ``emu_stop`` requested from *inside* a Unicorn callback is
        synchronous and precise (measured), unlike the pacer thread's
        asynchronous stop, which can roll back an in-flight instruction whose
        side effect already fired. Call this at the end of callbacks where a
        stop-and-resume is idempotent: the interrupt hook after exception
        delivery is complete (the resume enters the handler), and scoped code
        hooks whose own effect tolerates re-firing on resume (the stop lands
        before the hooked instruction, so every hook at that PC fires again).
        Do NOT call it from hooks with one-shot effects (e.g. a
        stack-popping restore) or from MMIO callbacks of registers with
        read side effects (mid-instruction, the access would replay — see
        :meth:`add_pace_serve_mmio` for the declared-pure exception).
        No-op unless a pace stop is due.
        """
        if self._pace_due:
            self._pace_due = False
            self._pace_serves += 1
            self.uc.emu_stop()

    # --- abort delivery: the CPU MMU's demand-paging path --------------------------

    def set_abort_handler(self, handler: Callable[[int], None] | None) -> None:
        """Install the prefetch/data-abort handler (the MMU-boot layer).

        The firmware boots under its own ARMv5 MMU (nandboot ``init2`` builds the page
        table; Unicorn honours it — see :mod:`tt_emu.mmu_boot`). Unicorn surfaces a
        translation/permission abort as ``UC_HOOK_INTR`` with ``intno`` 3 (prefetch) or 4
        (data) but does not vector ARMv5 exceptions; :meth:`_on_intr` delegates those to
        this handler, which performs the architectural entry into the firmware's own
        handler so it can demand-refine the page.
        """
        self._abort_handler = handler

    # --- physical-memory access: the DMA bus-master view ---------------------------

    def read_phys(self, phys: int, size: int) -> bytes | None:
        """Read ``size`` bytes of physical memory as a DMA bus master sees them.

        A DMA engine is a **physical**-address bus master: it reads physical memory
        directly, not through the CPU MMU. Under the emulated MMU the CPU's writes land at
        their physical frames, so a physical read is just a host read at ``phys`` — no
        page-table inversion. Returns ``None`` when ``phys`` is not backed by RAM.
        """
        try:
            return self.read_bytes(phys, size)
        except UcError:
            return None

    def write_bytes(self, addr: int, data: bytes) -> None:
        self.uc.mem_write(addr, data)

    def read_u32(self, addr: int) -> int:
        if self.mmu is not None:
            return struct.unpack("<I", self.mmu.read_va(addr, 4))[0]
        return struct.unpack("<I", self.uc.mem_read(addr, 4))[0]

    def write_u32(self, addr: int, value: int) -> None:
        self.uc.mem_write(addr, struct.pack("<I", value & 0xFFFFFFFF))

    def read_u16(self, addr: int) -> int:
        if self.mmu is not None:
            return struct.unpack("<H", self.mmu.read_va(addr, 2))[0]
        return struct.unpack("<H", self.uc.mem_read(addr, 2))[0]

    def read_u8(self, addr: int) -> int:
        if self.mmu is not None:
            return self.mmu.read_va(addr, 1)[0]
        return self.uc.mem_read(addr, 1)[0]

    def write_u8(self, addr: int, value: int) -> None:
        self.uc.mem_write(addr, bytes((value & 0xFF,)))

    # --- CPU helpers ---------------------------------------------------------------

    @property
    def pc(self) -> int:
        return self.uc.reg_read(UC_ARM_REG_PC)

    @property
    def cpsr(self) -> int:
        return self.uc.reg_read(UC_ARM_REG_CPSR)

    def set_entry_state(self, pc: int, sp: int, cpsr: int) -> None:
        """Seed the initial CPU state (``memory-map-and-boot.md`` §5.2)."""
        self.uc.reg_write(UC_ARM_REG_CPSR, cpsr)
        self.uc.reg_write(UC_ARM_REG_SP, sp)
        self.uc.reg_write(UC_ARM_REG_PC, pc)

    def on_code(self, addr: int, callback: Callable[[Machine], None]) -> None:
        """Invoke ``callback`` whenever execution reaches ``addr`` (checkpoint hook)."""

        def hook(_uc: Uc, _address: int, _size: int, _ud: object) -> None:
            callback(self)

        self.uc.hook_add(UC_HOOK_CODE, hook, begin=addr, end=addr)


    # --- run control ---------------------------------------------------------------

    def request_stop(self, reason: str) -> None:
        """Stop emulation cleanly (e.g. GPIO15 power-off, fatal checkpoint)."""
        if self._stop_reason is None:
            self._stop_reason = reason
        self.uc.emu_stop()

    def clear_stop(self) -> None:
        """Clear a pending stop so :meth:`run` can be resumed.

        The synchronous scripting API (:mod:`tt_emu.emulator`) steps the machine
        as a sequence of bounded :meth:`run` calls, each ended from ``on_chunk``
        with :meth:`request_stop`; clearing the latched reason between calls lets
        the *same* machine resume forward. Does not touch CPU state, only the
        run-loop's stop latch. (The threaded TUI worker never calls this — it
        runs one open-ended :meth:`run`.)
        """
        self._stop_reason = None

    @property
    def stop_reason(self) -> str | None:
        return self._stop_reason

    def run(
        self,
        max_instructions: int,
        on_chunk: Callable[[Machine], None] | None = None,
    ) -> RunResult:
        """Emulate forward up to ``max_instructions`` (or until a stop is requested).

        Runs in chunks; between chunks the peripherals tick (advancing model
        time) and a pending+enabled IRQ is delivered per
        ``interrupts-and-timers.md`` §3/§7.3. How a chunk is bounded — and what
        the clock therefore means — is ``config.pacing``:

        * ``"deterministic"``: a chunk is exactly ``config.effective_chunk``
          instructions and the clock counts executed instructions (reproducible,
          slow — Unicorn's instruction counting instruments every instruction);
        * ``"realtime"``: a chunk is ``effective_chunk``-worth of *wall time*
          (a pacer thread ends it via ``emu_stop``) and the clock advances by
          wall time at the modelled rate of ``instructions_per_tick`` per 20 ms
          — emulated time tracks real time, at full TCG speed.

        ``max_instructions`` is a budget on the same clock either way.
        """
        if self.config.pacing == "realtime":
            self._run_realtime(max_instructions, on_chunk)
        else:
            self._run_deterministic(max_instructions, on_chunk)
        reason = self._stop_reason or "instruction budget exhausted"
        return RunResult(reason=reason, instructions=self.clock, pc=self.pc)

    def _run_deterministic(
        self,
        max_instructions: int,
        on_chunk: Callable[[Machine], None] | None,
    ) -> None:
        """Count-paced chunks: exact, reproducible, instrumented (see :meth:`run`)."""
        # Hoisted hot-loop locals: this loop runs once per chunk, and the
        # attribute lookups add up over the millions of chunks of a session.
        uc = self.uc
        reg_read = uc.reg_read
        emu_start = uc.emu_start
        chunk_size = self.config.effective_chunk
        ticking = self._ticking
        intc = self.intc
        deliver_irq = self._deliver_irq
        budget_end = self.clock + max_instructions
        while self._stop_reason is None and self.clock < budget_end:
            chunk = min(chunk_size, budget_end - self.clock)
            pc = reg_read(UC_ARM_REG_PC)
            if reg_read(UC_ARM_REG_CPSR) & CPSR_T:
                pc |= 1  # resume in Thumb state
            self._fault = None
            try:
                emu_start(pc, _NEVER, count=chunk)
            except UcError as err:
                if not self._handle_emu_error(err):
                    break
            # The chunk may have ended early (emu_stop/fault); the approximation
            # only paces peripherals, so counting the full chunk is acceptable.
            self.clock += chunk
            for peripheral in ticking:
                peripheral.tick(self.clock)
            if intc is not None and intc.irq_asserted():
                deliver_irq()
            if on_chunk is not None:
                on_chunk(self)

    def _run_realtime(
        self,
        max_instructions: int,
        on_chunk: Callable[[Machine], None] | None,
    ) -> None:
        """Wall-paced chunks: emulated time == real time, full TCG speed.

        ``emu_start`` runs count-free (no per-instruction instrumentation; the
        scoped code hooks are bounds-checked at translation time and cost
        nothing). A pacer thread requests a chunk end on the
        ``effective_chunk``-worth-of-wall-time cadence (2 ms at the session
        cadence and the default chunk — the same IRQ-latency bound as
        deterministic mode). Chunks run in one of two modes, adaptively:

        * **count-free** (full TCG speed) while stop-safe callbacks
          demonstrably end chunks promptly — :meth:`maybe_pace_stop` in the
          exception hook, the replay-idempotent core-page *write* hook, and
          the declared-pure MMIO poll registers
          (:meth:`add_pace_serve_mmio`). Busy phases (NAND streams, demand
          paging, audio) are exactly the serve-dense ones, so they get the
          speed.
        * **count-paced** (the deterministic primitive, ~10-20x slower but
          precise and self-terminating) everywhere else — idle polling,
          busy-delay loops, and under scheduler jitter. Those phases do not
          need surplus speed, and a count chunk can never wedge.

        The pacer thread itself only *requests* chunk ends: an asynchronous
        ``emu_stop`` can roll the in-flight instruction back *after* its I/O
        side effect fired, and the resume then replays the access (measured —
        it corrupts the NFC FIFO stream); no wall-clock quietness heuristic
        survives GIL/scheduler jitter (a starved worker looks quiet exactly
        while stalled mid-I/O). Its one exception is the emergency stop of a
        count-free chunk that stopped being served entirely
        (:data:`_PACE_UNSERVED_WARN_SECONDS` — after seconds of zero
        callbacks the CPU is in pure CPU/RAM code with near certainty, and
        hanging would be worse).

        The clock advances by measured wall time at the modelled CPU rate,
        capped at one second per chunk so a host stall (suspend) slips
        emulated time rather than replaying it.
        """
        uc = self.uc
        reg_read = uc.reg_read
        emu_start = uc.emu_start
        ipt = self.config.instructions_per_tick
        rate = ipt * TICKS_PER_SECOND  # modelled insn/s == the pen's own rate
        interval = self.config.effective_chunk / rate  # wall seconds per chunk
        ticking = self._ticking
        intc = self.intc
        deliver_irq = self._deliver_irq
        budget_end = self.clock + max_instructions

        pacer_stop = threading.Event()

        def pace() -> None:
            # The pacer is the only ender of a count-free chunk in a phase
            # that stopped producing serves: if this thread dies, such a
            # chunk runs forever and the machine freezes (observed via a
            # single swallowed exception). Never let one iteration's failure
            # kill the loop.
            unserved = 0.0
            while not pacer_stop.wait(interval):
                try:
                    if not self._pace_due:
                        # Ask for a chunk end. In count-free chunks a
                        # stop-safe callback serves it; count-paced chunks
                        # end by themselves (the run loop clears the flag
                        # either way).
                        self._pace_due = True
                        unserved = 0.0
                    else:
                        # Unserved: the phase stopped producing serves
                        # (typically a busy→idle transition wedging a
                        # count-free chunk). Break it with the async stop —
                        # cross-thread stops land on translation-block
                        # boundaries (consistent state), and a serve-less
                        # phase has no MMIO callbacks in flight to replay —
                        # deferring the boundary's IRQ to the next count end.
                        unserved += interval
                        if unserved >= _PACE_UNSERVED_STOP_SECONDS:
                            unserved = 0.0
                            self._pace_due = False
                            self._pace_skip_irq = True
                            uc.emu_stop()
                except Exception:  # noqa: BLE001 — pacer must survive anything
                    log.exception("realtime pacer: iteration failed (continuing)")

        pacer = threading.Thread(target=pace, name="tt-emu-pacer", daemon=True)
        pacer.start()
        # Count chunks stay small in realtime mode: their per-chunk cost is
        # dominated by the firmware's Python-visible traffic (e.g. the OID
        # bit-bang at idle), and IRQ delivery only happens at chunk ends —
        # a small count keeps ticks and tap handling responsive even when
        # the guest's effective speed is low.
        count_size = min(self.config.effective_chunk, _PACE_COUNT_CHUNK)
        count_free = False
        last = time.monotonic()
        try:
            while self._stop_reason is None and self.clock < budget_end:
                pc = reg_read(UC_ARM_REG_PC)
                if reg_read(UC_ARM_REG_CPSR) & CPSR_T:
                    pc |= 1  # resume in Thumb state
                self._fault = None
                serves_before = self._pace_serves
                was_free = count_free
                emu_t0 = time.monotonic()
                try:
                    if count_free:
                        emu_start(pc, _NEVER)
                    else:
                        emu_start(pc, _NEVER, count=count_size)
                except UcError as err:
                    if not self._handle_emu_error(err):
                        break
                now = time.monotonic()
                emu_dur = now - emu_t0
                elapsed = now - last
                last = now
                # The clock must never outrun the guest, or the firmware's
                # calibrated timing (battery sampling, delay loops) breaks:
                # * count chunks know the executed instructions exactly —
                #   advance by them (deterministic semantics; a slow host
                #   just runs slower than real time, correctly);
                # * count-free chunks run at full TCG speed, well above the
                #   modelled rate — wall time is the *smaller* advance there,
                #   which is what locks busy phases to real time.
                if count_free:
                    self.clock += min(
                        int(elapsed * rate * _PACE_FREE_OVERDRIVE), rate)
                else:
                    self.clock += count_size  # ended early by a serve: §"counting
                    # the full chunk is acceptable" (same as deterministic mode)
                self._pace_due = False  # any chunk end fulfils the request
                served = self._pace_serves != serves_before
                served_prompt = (
                    served
                    # Warm-up: the first emulated seconds run count-paced
                    # regardless — boot's calibrated self-tests (battery
                    # sampling, clock probes, §5.4) must see deterministic
                    # clock-vs-execution proportions or they trip.
                    and self.clock >= _PACE_WARMUP_TICKS * ipt
                )
                skip_irq, self._pace_skip_irq = self._pace_skip_irq, False
                if self.pace_trace is not None:
                    self.pace_trace.append(
                        ("free" if was_free else "count",
                         elapsed, served, skip_irq, emu_dur, self.pc))
                # Adaptive mode: count-free only while serve points are
                # demonstrably dense (chunks ended by a serve, promptly);
                # serve-sparse phases run count-paced — slower, but precise
                # and self-terminating. A skip-IRQ boundary forces the next
                # chunk onto the count side too: its end is where the
                # deferred IRQ gets delivered, and a count end is a clean
                # post-instruction boundary.
                count_free = served_prompt and not skip_irq
                if not count_free and was_free:
                    # Counted chunks after a count-free chunk need freshly
                    # instrumented translations: Unicorn re-adds its count
                    # hook without invalidating cached TBs, so counting
                    # silently never fires in them (measured: counted chunks
                    # running 60+ ms until the pacer's fallback).
                    try:
                        uc.ctl_flush_tb()
                    except UcError:
                        pass
                for peripheral in ticking:
                    peripheral.tick(self.clock)
                if not skip_irq and intc is not None and intc.irq_asserted():
                    deliver_irq()
                if on_chunk is not None:
                    on_chunk(self)
        finally:
            pacer_stop.set()
            pacer.join()
            self._pace_due = False
            self._pace_skip_irq = False

    # --- IRQ delivery (interrupts-and-timers.md §3) ---------------------------------

    def _deliver_irq(self) -> bool:
        """Deliver one asserted IRQ if the CPU accepts it (§3).

        The caller (the run loop) has already checked ``intc.irq_asserted()``.
        """
        cpsr = self.uc.reg_read(UC_ARM_REG_CPSR)
        if cpsr & CPSR_I or (cpsr & MODE_MASK) == MODE_IRQ:
            return False
        interrupted_pc = self.uc.reg_read(UC_ARM_REG_PC)
        # Architectural entry sequence (§3): bank to IRQ mode, save CPSR to
        # SPSR_irq, LR_irq = interrupted PC + 4, jump to the image's vector.
        # Unicorn banks SP/LR/SPSR on the CPSR mode write.
        self.uc.reg_write(UC_ARM_REG_CPSR, (cpsr & ~0x3F) | MODE_IRQ | CPSR_I)
        self.uc.reg_write(UC_ARM_REG_SPSR, cpsr)
        if not self._irq_sp_initialized:
            # The from-entry boot skips the blob's per-mode stack setup (§3).
            self.uc.reg_write(UC_ARM_REG_SP, IRQ_STACK_TOP)
            self._irq_sp_initialized = True
        self.uc.reg_write(UC_ARM_REG_LR, (interrupted_pc + 4) & 0xFFFFFFFF)
        self.uc.reg_write(UC_ARM_REG_PC, IRQ_VECTOR)
        self.irqs_delivered += 1
        return True

    # --- exception / fault handling --------------------------------------------------

    def _handle_emu_error(self, err: UcError) -> bool:
        """Deal with an emulation exception; return True to continue running.

        Neutralizes CP15 (MCR/MRC p15) instructions that Unicorn refuses, per
        ``memory-map-and-boot.md`` §1.2 ("accept and ignore/neutralize all CP15
        writes"); everything else becomes a stop with diagnostics.
        """
        pc = self.uc.reg_read(UC_ARM_REG_PC)
        if err.errno == UC_ERR_EXCEPTION and not self.uc.reg_read(UC_ARM_REG_CPSR) & CPSR_T:
            try:
                insn = self.read_u32(pc)
            except UcError:
                insn = 0
            is_cp15 = (insn & 0x0F000010) == 0x0E000010 and ((insn >> 8) & 0xF) == 15
            if is_cp15:
                if pc not in self._neutralized_cp15:
                    self._neutralized_cp15.add(pc)
                    log.debug("neutralized CP15 op %08x at pc=%#010x (§1.2)", insn, pc)
                self.uc.reg_write(UC_ARM_REG_PC, pc + 4)
                return True
        detail = self._fault or f"cpu exception ({err})"
        self._stop_reason = f"{detail} at pc={pc:#010x}"
        return False

    def _on_unmapped(self, _uc: Uc, access: int, addr: int, size: int, value: int, _ud: object) -> bool:
        kind = "write" if access == UC_MEM_WRITE_UNMAPPED else "read"
        self._fault = f"unmapped {kind} of {size} bytes at {addr:#010x} (value {value:#x})"
        return False  # let emu_start raise; run() records the fault

    # --- semihosting (memory-map-and-boot.md §1.1) ------------------------------------

    def _on_intr(self, _uc: Uc, intno: int, _ud: object) -> None:
        """Interrupt hook: dispatch MMU aborts, else log semihosting ``svc 0xab``.

        Prefetch(3)/data(4) aborts go to the MMU-boot handler (demand-paging); the
        firmware's fault handlers otherwise print via semihosting, which is not on the
        happy path (§1.1), so logging + returning is sufficient.
        """
        if intno in (3, 4):
            if self._abort_handler is not None:
                self._abort_handler(intno)
            # Exception state is fully rewritten (PC = the firmware's vector):
            # a stop here resumes cleanly into the handler — and faults are the
            # densest stop-safe event in fault-heavy phases (boot, decode).
            self.maybe_pace_stop()
            return
        pc = self.uc.reg_read(UC_ARM_REG_PC)
        cpsr = self.uc.reg_read(UC_ARM_REG_CPSR)
        try:
            if cpsr & CPSR_T:
                imm = struct.unpack("<H", self.uc.mem_read(pc - 2, 2))[0] & 0xFF
            else:
                imm = self.read_u32(pc - 4) & 0x00FFFFFF
        except UcError:
            imm = -1
        if imm != 0xAB:
            log.warning("svc #%#x at pc=%#010x (intno=%d) — ignored", imm, pc, intno)
            return
        op = self.uc.reg_read(UC_ARM_REG_R0)
        arg = self.uc.reg_read(UC_ARM_REG_R1)
        text = ""
        try:
            if op == 0x03:  # SYS_WRITEC
                text = self.read_bytes(arg, 1).decode("latin-1")
            elif op == 0x04:  # SYS_WRITE0
                raw = self.read_bytes(arg, 256)
                text = raw.split(b"\x00", 1)[0].decode("latin-1", "replace")
            elif op == 0x05:  # SYS_WRITE {fd, buf, len}
                _fd, buf, length = struct.unpack("<III", self.read_bytes(arg, 12))
                text = self.read_bytes(buf, min(length, 512)).decode("latin-1", "replace")
        except UcError:
            text = "<unreadable>"
        if text:
            log.info("semihosting: %s", text.rstrip("\n"))
        self.uc.reg_write(UC_ARM_REG_R0, 0)
        self.maybe_pace_stop()  # SVC handled; resume continues after it
