"""Headless runner: load firmware, boot, emulate forward, report progress.

Boots per ``memory-map-and-boot.md`` §5 and runs to a checkpoint or a stop
condition (power-off, fault, budget). Boot health checkpoints (§5.8) are wired
as code hooks so progress is observable: ``app_init_main`` entry/return, the
event-pump idle loop, and the fatal-hang addresses of the self-tests.

:func:`run_session` layers a scripted tap session on top: boot, enter book
mode, inject OID taps through the sensor model (``oid-sensor.md`` §6), let the
firmware mount the book and play, and capture the audio the DAC DMA moves
(``audio-dac-dma.md`` §7) — the full boot → book → tap product → tap content →
WAV chain, firmware unmodified. Book mode is reached authentically via the
``B:/FLAG.bin`` resume marker (``nand-image-layout.md`` §7.3.1): splash sets
the resume byte ``+0x24 = 1`` and the first standby heartbeat auto-posts the
mount event, descending splash → standby(3) → mount(12) → book(13) with no
tap and no ``+0x1d`` gate.
"""

from __future__ import annotations

import logging
import struct
from dataclasses import dataclass, field
from pathlib import Path

from .audio_capture import AudioStats
from .boot import BootedMachine, build_machine
from .fat16 import files_from_dir
from .loader import Firmware, load_upd
from .machine import Machine, MachineConfig, RunResult

log = logging.getLogger(__name__)

# --- Boot checkpoints (memory-map-and-boot.md §4/§5.8) -----------------------------

#: Named PC checkpoints; reaching one is logged and recorded.
CHECKPOINTS: dict[int, str] = {
    0x08039100: "PROG entry (§5.2)",
    0x08038F5C: "app_init_main entry (§4)",
    0x08038DA8: "power_battery_check (§5.4)",
    0x0804C47C: "anticlone_zc90b_verify (§5.4 item 3)",
    0x0800B4A4: "event-pump main loop (§4 / §5.8 'main loop idle')",
}

#: Fatal-hang addresses the self-tests branch to on failure (§5.4).
FATAL_ADDRS: dict[int, str] = {
    0x0804E50C: "ZC90B auth failure fatal hang (§5.4 item 3)",
    0x08038D24: "fatal_low_battery_blink (battery-and-power.md §2.3)",
}

# --- Mount health globals (memory-map-and-boot.md §5.8, index.md RAM table) ---------

#: Mem-driver vtable "keystone": *0x081db984 == 0x08038cf8 after a good mount.
KEYSTONE_ADDR = 0x081D_B984
KEYSTONE_EXPECTED = 0x0803_8CF8
#: Codepage-active flag byte (nonzero once codepage_load succeeded).
CODEPAGE_FLAG_ADDR = 0x081D_B730
#: Booklist head (populated by the discovery scan).
BOOKLIST_HEAD_ADDR = 0x081D_A080

# --- Session-observability globals (index.md RAM table; oid/audio docs) --------------

#: ``g_state`` — the statechart state global of memory-map-and-boot.md §5.8.
#: Observed to *lag* on the FLAG.bin auto-descent (stays 3 while the statechart
#: is in book(13)) — the QHsm frame stack below is the authoritative leaf.
G_STATE_ADDR = 0x081D_B904
STATE_STANDBY = 3
STATE_BOOK = 13

#: QHsm statechart frame stack: 12-byte frames at 0x08007e80 + 12·depth (the
#: boot seed QHSM_FRAME_ADDR is frame[0]), byte 0 = the state number, 0 =
#: unused. The current leaf is the **deepest non-zero frame** (Observed: after
#: the FLAG.bin descent the stack reads [3, 12, 13] = standby → mount → book
#: while ``g_state`` still says 3).
QHSM_FRAME_BASE = 0x0800_7E80
QHSM_FRAME_STRIDE = 12
QHSM_MAX_DEPTH = 6
#: Current mounted product id — written by the tap-2 header parse
#: (nand-image-layout.md §7.3: "current product → 0x081da08c"); 0 = none.
CURRENT_PRODUCT_ADDR = 0x081D_A08C
#: Book-entry tail PC: book entry plays the book-open voice and only then
#: clears the pen-down flag and re-enables capture — this instruction is the
#: "book entry finished" marker (nand-image-layout.md §7.3.2 item 2).
BOOK_ENTRY_TAIL_PC = 0x0803_4850
#: Game-context resume byte +0x24 (base 0x080089a4): 1 once splash saw
#: ``B:/FLAG.bin`` (§7.3.1 "resume descent") — diagnostics only, never written.
RESUME_BYTE_ADDR = 0x0800_89C8
#: Settle gap between a tap's decode and the next tap (§7.3.2: ~3 M emulated
#: instructions, pen lifted in between).
SETTLE_INSTRUCTIONS = 3_000_000
#: Audio chain flag byte: bit0 = chain idle, bit2 = chain active (audio-dac-dma.md §3).
AUDIO_FLAGS_ADDR = 0x0800_8C60
AUDIO_CHAIN_ACTIVE = 1 << 2
#: AO event ring head/tail (16 records; head==tail means the pump is drained).
EVENT_RING_HEAD_ADDR = 0x0800_8958
EVENT_RING_TAIL_ADDR = 0x0800_895A
#: The event-pump main-loop checkpoint address (also in :data:`CHECKPOINTS`).
EVENT_PUMP_ADDR = 0x0800_B4A4
#: OID working-buffer pointer (game context +0x20); buf+4 = the current OID the
#: classifier stored (oid-sensor.md §4/§5).
AKOID_BUF_PTR_ADDR = 0x0800_89C4

#: GME container: the product/book OID code is the little-endian u32 at file
#: offset 0x14 (game-data format, not firmware RE).
GME_PRODUCT_CODE_OFFSET = 0x14

#: The post-update resume marker (``nand-image-layout.md`` §7.3.1 "resume
#: descent"): when ``B:/FLAG.bin`` exists, splash sets the resume byte
#: ``+0x24 = 1``; the first accepted standby heartbeat then posts the mount
#: event 0x1058 itself and the pen descends into book(13) **autonomously** —
#: no tap-1, no ``+0x1d`` gate. Only its existence is load-bearing; the
#: content is a minimal 1-byte marker.
FLAG_BIN_NAME = "FLAG.bin"
FLAG_BIN_CONTENT = b"\x01"


def gme_product_code(data: bytes) -> int:
    """The product OID of a ``.gme`` file (u32 LE at offset 0x14)."""
    return struct.unpack_from("<I", data, GME_PRODUCT_CODE_OFFSET)[0]


def statechart_leaf(machine: Machine) -> int:
    """The current QHsm statechart leaf: the deepest non-zero frame's state.

    Walks the frame stack at :data:`QHSM_FRAME_BASE` (1 = splash, 3 = standby,
    12 = mount, 13 = book); returns 0 before the first statechart entry.
    """
    leaf = 0
    for depth in range(QHSM_MAX_DEPTH):
        state = machine.read_u8(QHSM_FRAME_BASE + depth * QHSM_FRAME_STRIDE)
        if state == 0:
            break
        leaf = state
    return leaf


@dataclass
class BootReport:
    """Structured result of a headless boot run."""

    result: RunResult
    checkpoints_hit: list[tuple[int, int, str]] = field(default_factory=list)
    timer_irqs: int = 0
    irqs_delivered: int = 0
    keystone: int = 0
    codepage_flag: int = 0
    booklist_head: int = 0
    nand_reads: int = 0
    nand_programs: int = 0
    nand_erases: int = 0

    @property
    def mount_ok(self) -> bool:
        """§5.8 "Keystone self-built" — the NFTL init ran through the real mount."""
        return self.keystone == KEYSTONE_EXPECTED

    def format_log(self) -> str:
        lines = ["boot run log:"]
        for clock, pc, name in self.checkpoints_hit:
            lines.append(f"  [{clock:>10} insn] pc={pc:#010x}  {name}")
        lines.append(
            f"  stop: {self.result.reason} at pc={self.result.pc:#010x} "
            f"after ~{self.result.instructions} insn"
        )
        lines.append(f"  timer IRQs latched: {self.timer_irqs}, delivered: {self.irqs_delivered}")
        lines.append(
            f"  mount: keystone={self.keystone:#010x} "
            f"({'OK' if self.mount_ok else 'not built'}), "
            f"codepage flag={self.codepage_flag:#x}, booklist head={self.booklist_head:#010x}"
        )
        lines.append(
            f"  nand: {self.nand_reads} reads, {self.nand_programs} programs, "
            f"{self.nand_erases} erases"
        )
        return "\n".join(lines)


def _prepare(
    path: str,
    config: MachineConfig | None,
    a_dir: str | None,
    b_dir: str | None,
    b_files: dict[str, bytes] | None = None,
) -> tuple[BootedMachine, list[tuple[int, int, str]]]:
    """Load + build the machine and wire the §5.8 checkpoint hooks."""
    firmware: Firmware = load_upd(path)
    all_b = dict(files_from_dir(b_dir)) if b_dir else {}
    if b_files:
        all_b.update(b_files)
    booted: BootedMachine = build_machine(
        firmware,
        config,
        a_files=files_from_dir(a_dir) if a_dir else None,
        b_files=all_b or None,
    )
    machine = booted.machine

    hits: list[tuple[int, int, str]] = []
    seen: set[int] = set()

    def make_checkpoint_hook(addr: int, name: str, fatal: bool):
        def hook(m: Machine) -> None:
            if addr not in seen:
                seen.add(addr)
                hits.append((m.clock, addr, name))
                log.info("checkpoint pc=%#010x %s", addr, name)
            if fatal:
                m.request_stop(f"fatal: {name}")

        return hook

    for addr, name in CHECKPOINTS.items():
        machine.on_code(addr, make_checkpoint_hook(addr, name, fatal=False))
    for addr, name in FATAL_ADDRS.items():
        machine.on_code(addr, make_checkpoint_hook(addr, name, fatal=True))
    return booted, hits


def _fill_report(report: BootReport, booted: BootedMachine) -> None:
    machine = booted.machine
    report.timer_irqs = booted.machine.intc.timer_irqs if booted.machine.intc else 0  # type: ignore[attr-defined]
    report.irqs_delivered = machine.irqs_delivered
    report.keystone = machine.read_u32(KEYSTONE_ADDR)
    report.codepage_flag = machine.read_u8(CODEPAGE_FLAG_ADDR)
    report.booklist_head = machine.read_u32(BOOKLIST_HEAD_ADDR)
    report.nand_reads = booted.nand.reads
    report.nand_programs = booted.nand.programs
    report.nand_erases = booted.nand.erases


def boot_firmware(
    path: str,
    *,
    max_instructions: int = 40_000_000,
    config: MachineConfig | None = None,
    a_dir: str | None = None,
    b_dir: str | None = None,
    b_files: dict[str, bytes] | None = None,
) -> BootReport:
    """Load ``path``, boot, and emulate up to ``max_instructions``; return a report.

    ``a_dir``/``b_dir`` are optional host directories mirrored onto the A:
    (system) / B: (user ``.gme``) NAND partitions (``nand-image-layout.md`` §5);
    ``b_files`` adds individual files (name → bytes) to B:.
    """
    booted, hits = _prepare(path, config, a_dir, b_dir, b_files)
    result = booted.machine.run(max_instructions)
    report = BootReport(result=result, checkpoints_hit=hits)
    _fill_report(report, booted)
    return report


# --- Scripted tap session (the boot → tap → play → WAV chain) -------------------------


@dataclass
class SessionReport(BootReport):
    """Result of a scripted tap session (extends the boot report)."""

    taps: list[int] = field(default_factory=list)
    taps_fired: int = 0
    taps_served: int = 0
    flag_resume: bool = False
    mounted_product: int = 0
    resume_byte: int = 0
    state_chain: list[tuple[int, int]] = field(default_factory=list)  # (clock, g_state)
    audio_stats: AudioStats | None = None
    wav_path: str | None = None
    dac_submits: int = 0
    flush_submits: int = 0
    unresolved_submits: int = 0
    audio_completions: int = 0
    ring_volume: int = 0
    last_oid_seen: int = 0
    stall: str | None = None
    discovery_note: str | None = None

    def format_log(self) -> str:
        lines = [super().format_log(), "session:"]
        chain = " -> ".join(f"{s}@{c}" for c, s in self.state_chain) or "(none)"
        lines.append(f"  statechart leaf chain (state@clock): {chain}")
        if self.flag_resume:
            lines.append(
                f"  flag-resume: B:/FLAG.bin route, resume byte +0x24={self.resume_byte}"
            )
        lines.append(f"  mounted product: {self.mounted_product}")
        lines.append(
            f"  taps: planned {self.taps}, fired {self.taps_fired}, "
            f"frames served {self.taps_served}, classifier OID {self.last_oid_seen}"
        )
        lines.append(
            f"  audio: {self.dac_submits} DAC submits ({self.flush_submits} teardown "
            f"flushes, {self.unresolved_submits} unresolved), "
            f"{self.audio_completions} completions, ring volume {self.ring_volume:#x}"
        )
        if self.audio_stats is not None:
            s = self.audio_stats
            lines.append(
                f"  capture: {s.chunks} chunks, {s.total_bytes} bytes, "
                f"{s.duration_s:.2f} s @ {s.rate} Hz, peak {s.peak}/32768, "
                f"{s.nonzero_pct:.1f}% nonzero samples, {s.audible_pct:.0f}% amp-audible"
            )
        if self.wav_path:
            lines.append(f"  wav written: {self.wav_path}")
        if self.stall:
            lines.append(f"  STALL: {self.stall}")
        if self.discovery_note:
            lines.append(f"  NOTE: {self.discovery_note}")
        return "\n".join(lines)


class _TapSession:
    """Drives a scripted tap sequence off the machine's chunk callback.

    Observes only firmware RAM (g_state, audio flags, the event ring, the
    mounted-product word) and the capture byte count; never writes firmware
    state. Each tap is a physical **press-and-hold** (``oid-sensor.md`` §6
    "Repeat / anti-repeat"): the frame is re-served every capture until the
    firmware visibly reacts, then the pen is lifted. Holding is what makes
    taps reliable at standby, where the 32-bit status polls (§4.2) can consume
    frames without posting events.

    Two entry routes (``nand-image-layout.md`` §7.3):

    * ``flag_resume=False`` — the tap-1 route: the first tap fires at fresh
      standby and must win the one-heartbeat ``+0x1d == 2`` window (§7.3.1) —
      **known to lose the race**; kept for diagnosis.
    * ``flag_resume=True`` — the authentic ``B:/FLAG.bin`` resume (§7.3.1):
      the firmware auto-descends into book(13) with no tap; the session waits
      for book entry to finish (the §7.3.2 tail marker + audio drained +
      settle gap), then taps the product (the real mount) and the content
      OIDs.
    """

    def __init__(
        self,
        booted: BootedMachine,
        taps: list[int],
        checkpoint_hits: list[tuple[int, int, str]],
        *,
        flag_resume: bool = False,
        settle_ticks: int = 8,
        quiet_ticks: int = 50,
        react_timeout_ticks: int = 250,
        standby_timeout_ticks: int = 2_000,
        book_timeout_ticks: int = 300,
    ) -> None:
        self.booted = booted
        self.taps = taps
        self.flag_resume = flag_resume
        self.settle_ticks = settle_ticks
        self.quiet_ticks = quiet_ticks
        self.react_timeout_ticks = react_timeout_ticks
        self.standby_timeout_ticks = standby_timeout_ticks
        self.book_timeout_ticks = book_timeout_ticks

        self.state_chain: list[tuple[int, int]] = []
        self.taps_fired = 0
        self.stall: str | None = None

        self._hits = checkpoint_hits  # shared, appended by the checkpoint hooks
        self._phase = "book-wait" if flag_resume else "standby-wait"
        self._tap_index = 0
        self._g_last: int | None = None
        self._g_changed_at = 0
        self._g_at_tap = 0
        self._tap_at = 0
        self._cap_last = 0
        self._cap_grew_at = 0
        self._cap_at_tap = 0
        self._mounted_at_tap = 0
        self._gameplay_at_tap = 0
        self._standby_at: int | None = None
        self._book_tail_at: int | None = None
        self._last_progress_log = 0
        # PC marker: book entry's tail has run (§7.3.2 item 2). Observation
        # only — the hook reads the clock, it never touches firmware state.
        booted.machine.on_code(BOOK_ENTRY_TAIL_PC, self._on_book_tail)

    def _on_book_tail(self, machine: Machine) -> None:
        if self._book_tail_at is None:
            log.info("session: book-entry tail reached (clock=%d)", machine.clock)
        self._book_tail_at = machine.clock

    # -- helpers ------------------------------------------------------------------------

    def _ticks(self, machine: Machine, insns: int) -> float:
        return insns / machine.config.instructions_per_tick

    def _pump_idle(self, machine: Machine) -> bool:
        return machine.read_u16(EVENT_RING_HEAD_ADDR) == machine.read_u16(EVENT_RING_TAIL_ADDR)

    def _pump_running(self) -> bool:
        return any(addr == EVENT_PUMP_ADDR for _, addr, _ in self._hits)

    def _standby_entered(self, machine: Machine) -> bool:
        """True once the statechart's standby(3) ENTRY action has run — proven by
        a non-NULL booklist head (``memory-map-and-boot.md`` §5.8 "standby truly
        entered"; ``nand-image-layout.md`` §7.2). This is the authoritative
        "at fresh standby" signal: ``g_state`` alone flips to 3 early during init,
        long before the statechart actually reaches standby through the event
        pump, so gating a tap on it fires far too early."""
        return machine.read_u32(BOOKLIST_HEAD_ADDR) != 0

    def _classifier_oid(self, machine: Machine) -> int:
        """The OID the firmware's classifier last stored (``akoid_buf+4``, §5).

        Nonzero-equal-to-the-tap proves the *unmodified* firmware decoded the
        injected frame all the way through its capture → event → classifier
        chain — independent of whether a book then loaded.
        """
        ptr = machine.read_u32(AKOID_BUF_PTR_ADDR)
        return machine.read_u32(ptr + 4) & 0xFFFF if ptr else 0

    def _mounted(self, machine: Machine) -> int:
        """The currently mounted product id (0 = none) — §7.3 tap 2's output."""
        return machine.read_u32(CURRENT_PRODUCT_ADDR)

    def _press(self, machine: Machine) -> None:
        oid = self.taps[self._tap_index]
        log.info("session: pressing OID %d (g_state=%s, clock=%d)", oid, self._g_last, machine.clock)
        self.booted.oid.hold(oid)
        self.taps_fired += 1
        self._g_at_tap = self._g_last if self._g_last is not None else 0
        self._tap_at = machine.clock
        self._cap_at_tap = self.booted.audio.capture.total_bytes
        self._mounted_at_tap = self._mounted(machine)
        self._gameplay_at_tap = self.booted.oid.gameplay_frames_served
        self._phase = "tap-wait"

    def _fail(self, machine: Machine, reason: str) -> None:
        self.booted.oid.lift()
        self.stall = (
            f"{reason} (phase={self._phase}, tap#{self._tap_index}, "
            f"g_state={self._g_last}, captured={self._cap_last} B)"
        )
        machine.request_stop(f"session stalled: {reason}")

    # -- the chunk callback ----------------------------------------------------------------

    def on_chunk(self, machine: Machine) -> None:
        now = machine.clock
        g = statechart_leaf(machine)
        if g != self._g_last:
            self._g_last = g
            self._g_changed_at = now
            self.state_chain.append((now, g))
            log.info("session: statechart leaf -> %d (clock=%d)", g, now)
        cap = self.booted.audio.capture.total_bytes
        if cap != self._cap_last:
            self._cap_last = cap
            self._cap_grew_at = now
        if now - self._last_progress_log >= 25_000_000:
            self._last_progress_log = now
            log.info(
                "session: clock=%d phase=%s g_state=%s captured=%d B",
                now, self._phase, g, cap,
            )

        if self._standby_at is None and self._standby_entered(machine):
            self._standby_at = now

        if self._phase == "standby-wait":
            # Fire the first tap as soon as the standby ENTRY action has run
            # (booklist head allocated) and the event pump is idle. The window
            # before the firmware re-arms the fresh-standby marker +0x1d from 2
            # to 8 (~1 tick after discovery, nand-image-layout.md §7.3) is
            # short, so tap promptly — do not wait out a settle timer.
            if (
                self._standby_entered(machine)
                and self._pump_running()
                and self._pump_idle(machine)
            ):
                self._press(machine)
            elif self._ticks(machine, now) >= self.standby_timeout_ticks:
                self._fail(machine, "standby entry action never ran (booklist head 0)")

        elif self._phase == "book-wait":
            # FLAG.bin resume (§7.3.1): the firmware descends into book(13)
            # on its own. Tap the product only once book entry has fully run
            # (the §7.3.2 tail marker), its book-open voice drained (audio
            # chain idle + capture quiet), the pump is idle, and the settle
            # gap has elapsed — then the tap routes to the book handler's OID
            # dispatch and mounts the GME.
            book_tail_at = self._book_tail_at
            if g == STATE_BOOK and book_tail_at is not None:
                chain_active = machine.read_u8(AUDIO_FLAGS_ADDR) & AUDIO_CHAIN_ACTIVE
                quiet_for = self._ticks(machine, now - max(
                    self._cap_grew_at,
                    self.booted.audio.last_dac_submit_at,
                    book_tail_at,
                ))
                if (
                    now - book_tail_at >= SETTLE_INSTRUCTIONS
                    and quiet_for >= self.settle_ticks
                    and not chain_active
                    and self._pump_idle(machine)
                ):
                    self._press(machine)
            elif self._standby_at is None:
                if self._ticks(machine, now) >= self.standby_timeout_ticks:
                    self._fail(machine, "standby entry action never ran (booklist head 0)")
            elif self._ticks(machine, now - self._standby_at) >= self.book_timeout_ticks:
                resume = machine.read_u8(RESUME_BYTE_ADDR)
                self._fail(
                    machine,
                    f"FLAG.bin auto-descent never reached book(13) "
                    f"(g_state={g}, book-entry tail "
                    f"{'hit' if self._book_tail_at is not None else 'not hit'}, "
                    f"resume byte +0x24={resume} — expected 1 after splash saw "
                    f"B:/FLAG.bin, nand-image-layout.md §7.3.1)",
                )

        elif self._phase == "tap-wait":
            oid = self.taps[self._tap_index]
            if (
                self.booted.oid.pending
                and self.booted.oid.gameplay_frames_served > self._gameplay_at_tap
            ):
                # The 23-bit gameplay capture latched the frame (§7.3.2:
                # "re-serve the same frame until the firmware's own decode has
                # latched it") — the physical tap-and-lift ends here. One
                # gameplay capture = one event 0x1060; lifting now keeps a
                # held pen from dispatching the code a second time (Observed:
                # holding through the mount replays the welcome sound).
                self.booted.oid.lift()
            if (
                cap > self._cap_at_tap
                or g != self._g_at_tap
                or self._mounted(machine) != self._mounted_at_tap
                or self.booted.audio.last_dac_submit_at > self._tap_at
            ):
                # The firmware reacted (audio started / statechart moved /
                # a product mounted): the tap is delivered (§7.3.2).
                self.booted.oid.lift()
                self._phase = "play-wait"
            elif self._ticks(machine, now - self._tap_at) >= self.react_timeout_ticks:
                decoded = self._classifier_oid(machine) == oid
                if decoded and self._g_at_tap == STATE_BOOK:
                    # In book(13) a decoded product tap that produces no mount
                    # means the booklist probe rejected every record (§7.3
                    # tap 2: magic 0x238B / language / product-id match), and a
                    # decoded content tap without audio means the script never
                    # reached play_media.
                    booklist = machine.read_u32(BOOKLIST_HEAD_ADDR)
                    count = machine.read_u16(booklist) if booklist else 0
                    self._fail(
                        machine,
                        f"OID {oid} decoded in book(13) but no reaction — "
                        f"booklist count={count}, mounted product="
                        f"{self._mounted(machine)} (§7.3 tap-2/tap-3 dispatch)",
                    )
                elif decoded:
                    # The firmware's own capture decoded the tap (classifier
                    # holds akoid_buf+4 == our OID) but the statechart did not
                    # advance to book(13). On the first (product) tap this is the
                    # first-load gate losing the fresh-standby race: the OID is
                    # classified only *after* the firmware has re-armed the
                    # fresh-standby marker game-context +0x1d from 2 back to 8
                    # (Observed simultaneity — see run_session's note), and the
                    # first-load branch requires +0x1d == 2 (nand-image-layout.md
                    # §7.3). akoid_buf[0x21] stays 0xFF (the frame seed holds),
                    # so this is purely a decode-vs-rearm timing loss.
                    self._fail(
                        machine,
                        f"OID {oid} decoded by firmware (classifier holds it) but "
                        f"the statechart stayed at standby — first-load gate lost the "
                        f"fresh-standby race (game-context +0x1d re-armed 2->8 before "
                        f"the standby OID decode completed; see report). Use the "
                        f"authentic B:/FLAG.bin route (flag_resume/--flag-resume, "
                        f"§7.3.1)",
                    )
                else:
                    self._fail(
                        machine,
                        f"no reaction to held OID {oid} — firmware never decoded it "
                        f"({self.booted.oid.taps_served} frames served)",
                    )

        elif self._phase == "play-wait":
            # Quiet = no DAC submit (playback truly drained — the chain flag
            # alone flickers when the emulated decoder runs behind real time
            # and the ring transiently drains mid-track) and no capture growth.
            quiet_for = self._ticks(machine, now - max(
                self._cap_grew_at,
                self.booted.audio.last_dac_submit_at,
                self._tap_at,
            ))
            chain_active = machine.read_u8(AUDIO_FLAGS_ADDR) & AUDIO_CHAIN_ACTIVE
            if (
                quiet_for >= self.quiet_ticks
                and now - self._tap_at >= SETTLE_INSTRUCTIONS  # §7.3.2 settle gap
                and not chain_active
                and not self.booted.oid.pending
                and self._pump_idle(machine)
            ):
                self._tap_index += 1
                if self._tap_index < len(self.taps):
                    self._press(machine)
                else:
                    self._phase = "done"
                    machine.request_stop("session complete")


#: Session pacing: emulated instructions per 20 ms tick. Boot progress is
#: instruction-bound (measured: the pump is reached at ~17M instructions at any
#: ipt), but the firmware's busy-delay loops are calibrated for the real
#: ~100 MHz ARM926 — e.g. the standby OID status-poll trigger pulse
#: (``oid-sensor.md`` §3.4, ~100 ms) burns ~9M instructions. At the boot-doc
#: cadence of 20k insn/tick that one delay warps to ~9 emulated seconds, the
#: standby loop starves the event pump and auto-off (~30 s = 1500 ticks) wins
#: before a tap is ever dispatched. 1M insn/tick ≈ 50 MIPS restores realistic
#: proportions (pulse ≈ 0.18 s) — measured working end-to-end.
SESSION_INSTRUCTIONS_PER_TICK = 1_000_000


def run_session(
    path: str,
    taps: list[int],
    *,
    flag_resume: bool = False,
    wav_path: str | Path | None = None,
    max_instructions: int = 800_000_000,
    config: MachineConfig | None = None,
    a_dir: str | None = None,
    b_dir: str | None = None,
    b_files: dict[str, bytes] | None = None,
) -> SessionReport:
    """Boot, inject the ``taps`` sequence, capture audio, optionally write a WAV.

    The full milestone chain: boot → standby → tap the product OID (the game
    mounts) → tap content OIDs (scripts play) → the DAC DMA stream is captured
    and written as S16LE stereo WAV. ``b_files`` (name → bytes) provisions
    ``.gme`` files onto partition B: (``nand-image-layout.md`` §5).

    ``flag_resume`` provisions the ``B:/FLAG.bin`` resume marker
    (``nand-image-layout.md`` §7.3.1): the unmodified firmware then descends
    into book(13) autonomously and ``taps`` should be [product, content, …] —
    the authentic route around the one-heartbeat ``+0x1d`` standby window.
    """
    if config is None:
        config = MachineConfig(instructions_per_tick=SESSION_INSTRUCTIONS_PER_TICK)
    if flag_resume:
        b_files = dict(b_files or {})
        b_files.setdefault(FLAG_BIN_NAME, FLAG_BIN_CONTENT)
    booted, hits = _prepare(path, config, a_dir, b_dir, b_files)
    session = _TapSession(booted, taps, hits, flag_resume=flag_resume)
    result = booted.machine.run(max_instructions, on_chunk=session.on_chunk)

    machine = booted.machine
    report = SessionReport(result=result, checkpoints_hit=hits)
    _fill_report(report, booted)
    report.taps = taps
    report.taps_fired = session.taps_fired
    report.flag_resume = flag_resume
    report.mounted_product = machine.read_u32(CURRENT_PRODUCT_ADDR)
    report.resume_byte = machine.read_u8(RESUME_BYTE_ADDR)
    report.taps_served = booted.oid.taps_served
    report.state_chain = session.state_chain
    report.dac_submits = booted.audio.dac_submits
    report.flush_submits = booted.audio.flush_submits
    report.unresolved_submits = booted.audio.unresolved_submits
    report.audio_completions = booted.audio.completions
    report.ring_volume = booted.audio.ring_volume()
    report.stall = session.stall
    akoid_ptr = machine.read_u32(AKOID_BUF_PTR_ADDR)
    if akoid_ptr:
        report.last_oid_seen = machine.read_u32(akoid_ptr + 4)
    # Discovery/tap diagnostic. The boot now *does* enter statechart standby (the
    # ZC90B auth exchange passes — see peripherals/zc90b.py — so the run no longer
    # powers off at the auth-fail hang) and the standby(3) ENTRY action runs
    # discovery: booklist head 0x081da080 becomes non-NULL (the §5.8/§7.2 health
    # checkpoints "standby truly entered" + "discovery ran"). Two gaps remain
    # before audio; report whichever applies.
    if taps and not booted.audio.capture.chunks and flag_resume:
        booklist = machine.read_u32(BOOKLIST_HEAD_ADDR)
        count = machine.read_u16(booklist) if booklist else 0
        report.discovery_note = (
            f"No audio on the FLAG.bin route: resume byte +0x24="
            f"{report.resume_byte}, booklist count={count}, mounted product="
            f"{report.mounted_product} (see STALL for the failing step)."
        )
    elif taps and not booted.audio.capture.chunks:
        booklist = machine.read_u32(BOOKLIST_HEAD_ADDR)
        count = machine.read_u16(booklist) if booklist else 0
        decoded = (report.last_oid_seen & 0xFFFF) == (taps[0] & 0xFFFF)
        entered = "YES" if booklist else "NO"
        report.discovery_note = (
            f"No audio. Standby entered (booklist head {booklist:#x}, discovery ran="
            f"{entered}), booklist count={count}. "
            + (
                "The first product tap was decoded by the unmodified firmware "
                f"(classifier akoid_buf+4 = {report.last_oid_seen & 0xFFFF} == OID "
                f"{taps[0]}) but the statechart did not reach book(13): the first-load "
                "gate needs game-context +0x1d == 2, and at standby the OID decode "
                "completes only as the firmware re-arms +0x1d 2->8 (a decode-vs-rearm "
                "race, nand-image-layout.md §7.3). "
                if decoded else
                "The first product tap was not decoded in time (frames served="
                f"{report.taps_served}). "
            )
            + (
                "Discovery also found 0 games: even with the .gme on A:, the standby "
                "entry action creates no files on A: (no a:/oidfilelist.lst, no log) — "
                "A: writes reach NAND but no directory entry is ever committed, a "
                "FAT/NFTL file-creation gap in the write path. "
                if count == 0 else
                f"Booklist has {count} entries. "
            )
            + "The OID and audio peripherals are unit-verified; these are "
            "storage-write and standby-timing gaps."
        )
    if booted.audio.capture.chunks:
        report.audio_stats = booted.audio.capture.stats()
        if wav_path is not None:
            booted.audio.capture.write_wav(wav_path)
            report.wav_path = str(wav_path)
    return report
