"""Interactive terminal UI: run the emulator live, tap OID codes, hear the audio.

Architecture (all cross-platform, no firmware hooks — observation only):

* :class:`EmulatorSession` — a background **emulation thread**. It builds the
  machine exactly like :func:`tt_emu.runner.run_session` (the power button is
  held through the app-init sample, ``nand-image-layout.md`` §7.3.1a, so the
  pen descends into book mode as on a real power-on and a game can mount),
  then runs
  :meth:`~tt_emu.machine.Machine.run` continuously. The UI talks to it only
  through thread-safe surfaces: a command deque (taps / buttons / quit are
  *applied on the emulation thread* between chunks), an immutable
  :class:`EmuSnapshot` republished a few times per emulated second, an event
  deque for the log panel, and the audio ring below.
* :class:`AudioRing` — a byte FIFO between the emulation thread (fed live by
  :attr:`tt_emu.audio_capture.AudioCapture.listener`, i.e. every chunk the DAC
  DMA moves) and the sounddevice output callback.
* :class:`AudioOutput` — a sounddevice output stream at the firmware's rate
  (22050 Hz S16LE stereo, ``audio-dac-dma.md`` §1). **Degrades gracefully**: if
  PortAudio / an output device is missing it reports "audio unavailable" and
  the TUI keeps running. Underruns output silence: the emulator runs at only a
  fraction of the pen's real speed (~2.5 M insn/s vs. the ~50 MIPS pacing), so
  gaps during playback are an emulation-speed limit, not a bug.
* :class:`TtEmuApp` — the Textual app: state / tap / audio panels + a log.
* **Firmware-aware debugger** — when the loaded image is the recognized 2N
  "MT" build (byte-exact fingerprint, ``firmware-2n-mt.md`` §1), an
  :class:`~tt_emu.firmware.mt.MtDebugger` adds hook-free live readers (RAM
  polling + the documented read-only PC watchpoints) and the TUI reveals
  debugger panels: the live statechart hierarchy, a state-transition log, the
  GME interpreter (product, ``$``-registers, playlist/media), and the
  OID→script-line routing of the last tap. ``--yaml book.yaml`` joins a
  tttool source file for symbolic names (register/script/media names). On any
  other firmware the debugger stays off and the generic panels remain.

Taps are physical **press-and-hold** injections (``oid-sensor.md`` §6): the
frame is re-served until the firmware's own 23-bit gameplay capture latches it
(one event ``0x1060``), then the pen lifts — the same discipline as the
scripted runner. Buttons follow ``gpio-buttons-led.md`` §8 item 4: drive the
pin to its active level for ~2 scan periods (short press) or past the ~600 ms
hold threshold (power-off needs a hold), then release.

Run it: ``tt-emu-tui --game game.gme`` (the firmware is auto-downloaded and
cached when no path is given) or ``python -m tt_emu.tui …``.
"""

from __future__ import annotations

import argparse
import logging
import sys
import struct
import threading
import time
from collections import deque
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterable, Protocol, cast

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Footer, Header, Input, RichLog, Static

from .audio_capture import DEFAULT_RATE, FRAME_BYTES, CapturedChunk
from .boot import BootedMachine, build_machine
from .firmware import detect as detect_firmware
from .firmware import mt as fw_mt
from .firmware.symbols import GmeScripts, TttoolSymbols, derive_media_names, load_tttool_yaml
from .firmware_fetch import FirmwareDownloadError, FirmwareIntegrityError, ensure_firmware
from .loader import load_upd
from .machine import Machine, MachineConfig
from .runner import (
    AUDIO_CHAIN_ACTIVE,
    AUDIO_FLAGS_ADDR,
    BOOK_ENTRY_TAIL_PC,
    CHECKPOINTS,
    CURRENT_PRODUCT_ADDR,
    EVENT_RING_HEAD_ADDR,
    EVENT_RING_TAIL_ADDR,
    FATAL_ADDRS,
    RESUME_BYTE_ADDR,
    SESSION_INSTRUCTIONS_PER_TICK,
    SETTLE_INSTRUCTIONS,
    STATE_BOOK,
    STATE_NAMES,
    gme_product_code,
    statechart_leaf,
)

log = logging.getLogger(__name__)

__all__ = [
    "AudioOutput",
    "AudioRing",
    "EmuSnapshot",
    "EmulatorSession",
    "TtEmuApp",
    "gme_content_oids",
    "main",
]

# --- Pen buttons (gpio-buttons-led.md §2/§3): name -> (pin, active level) --------------

BUTTONS: dict[str, tuple[int, int]] = {
    "power": (11, 1),   # active HIGH, key code 8
    "vol+": (0, 0),     # active LOW, key code 5
    "vol-": (1, 1),     # active HIGH, key code 6
}

#: Button press durations in 20 ms ticks (gpio-buttons-led.md §8 item 4):
#: short press ~400 ms (>= 2 scan periods + debounce, < the ~600 ms hold
#: threshold); hold ~800 ms (past the hold threshold — power-off needs this).
BUTTON_PRESS_TICKS = 20
BUTTON_HOLD_TICKS = 40

#: Give up on a held tap the firmware never latched after this many emulated
#: instructions (mirrors the scripted runner's react timeout: 250 ticks).
TAP_TIMEOUT_INSTRUCTIONS = 250 * SESSION_INSTRUCTIONS_PER_TICK

#: Snapshot republish cadence (emulated instructions).
SNAPSHOT_INTERVAL = 200_000


def gme_content_oids(data: bytes, limit: int = 3) -> list[int]:
    """A few content OIDs from a ``.gme`` script table (for quick-tap buttons).

    Game-data format (not firmware RE): u32 at offset 0 points at the script
    table = ``u32 last_oid, u32 first_oid`` then one u32 script offset per code
    (``0xFFFFFFFF`` = unused). Returns the first ``limit`` used codes; empty on
    any parse doubt.
    """
    try:
        (table,) = struct.unpack_from("<I", data, 0)
        last, first = struct.unpack_from("<II", data, table)
        count = last - first + 1
        if not (0 < first <= last <= 0x3FFFF and count <= 20_000):
            return []
        offsets = struct.unpack_from(f"<{count}I", data, table + 8)
    except struct.error:
        return []
    return [first + i for i, off in enumerate(offsets) if off != 0xFFFFFFFF][:limit]


# --- Audio plumbing -------------------------------------------------------------------


class AudioRing:
    """Thread-safe PCM byte FIFO: emulation thread pushes, audio callback pulls.

    Bounded (oldest bytes dropped past ``max_seconds``) so a long unattended
    session can never grow without limit. :meth:`pull` never blocks and never
    raises — a short read is padded with silence, which is exactly the
    underrun behaviour the output callback needs.
    """

    def __init__(self, max_seconds: float = 60.0, rate: int = DEFAULT_RATE) -> None:
        self._lock = threading.Lock()
        self._chunks: deque[bytes] = deque()
        self._fill = 0
        self._max_bytes = int(max_seconds * rate) * FRAME_BYTES
        self.pushed_bytes = 0
        self.pulled_bytes = 0
        self.dropped_bytes = 0

    def push(self, data: bytes) -> None:
        if not data:
            return
        with self._lock:
            self._chunks.append(data)
            self._fill += len(data)
            self.pushed_bytes += len(data)
            while self._fill > self._max_bytes:
                dropped = self._chunks.popleft()
                self._fill -= len(dropped)
                self.dropped_bytes += len(dropped)

    def pull(self, nbytes: int) -> tuple[bytes, int]:
        """Up to ``nbytes`` of PCM, silence-padded to exactly ``nbytes``.

        Returns ``(data, got)`` where ``got`` is how many real bytes were
        available (0 = pure underrun/idle silence).
        """
        parts: list[bytes] = []
        got = 0
        with self._lock:
            while got < nbytes and self._chunks:
                chunk = self._chunks.popleft()
                take = min(len(chunk), nbytes - got)
                parts.append(chunk[:take])
                if take < len(chunk):
                    self._chunks.appendleft(chunk[take:])
                got += take
                self._fill -= take
            self.pulled_bytes += got
        if got < nbytes:
            parts.append(b"\x00" * (nbytes - got))
        return b"".join(parts), got

    @property
    def fill_bytes(self) -> int:
        with self._lock:
            return self._fill

    def fill_seconds(self, rate: int = DEFAULT_RATE) -> float:
        return self.fill_bytes / (rate * FRAME_BYTES)


class AudioOutput:
    """Real-time audio out via sounddevice, draining an :class:`AudioRing`.

    ``start()`` never raises: on any failure (no PortAudio library, no output
    device, …) it records the error and the TUI shows "audio unavailable".
    A small prebuffer smooths chunk jitter; a starved stream outputs silence
    and is reported as an underrun — expected while the emulator runs slower
    than real time.
    """

    #: Buffered audio required before playback starts (smooths submit jitter).
    PREBUFFER_SECONDS = 0.4
    #: Consecutive silent callbacks after which "underrun" decays to "idle".
    _IDLE_AFTER_BLOCKS = 20

    def __init__(self, ring: AudioRing, rate: int = DEFAULT_RATE) -> None:
        self.ring = ring
        self.rate = rate
        self.available = False
        self.error: str | None = None
        self.underrun_blocks = 0
        self.level = 0.0  #: peak of the last played block, 0..1
        self.state = "off"  #: off / idle / buffering / playing / underrun
        self._stream: object | None = None
        self._playing = False
        self._silent_blocks = 0
        self._prebuffer_bytes = int(self.PREBUFFER_SECONDS * rate) * FRAME_BYTES

    def start(self) -> bool:
        """Open + start the output stream; False (with :attr:`error`) on failure."""
        try:
            import numpy as np  # noqa: F401  (needed by the callback)
            import sounddevice as sd  # type: ignore[import-untyped]

            stream = sd.OutputStream(
                samplerate=self.rate,
                channels=2,
                dtype="int16",
                callback=self._callback,
            )
            stream.start()
            self._stream = stream
        except Exception as exc:  # noqa: BLE001 — any failure means "no audio"
            self.error = str(exc) or type(exc).__name__
            self.available = False
            self.state = "off"
            return False
        self.available = True
        self.state = "idle"
        return True

    def close(self) -> None:
        stream = self._stream
        self._stream = None
        self.available = False
        self.state = "off"
        if stream is not None:
            try:
                stream.stop()  # type: ignore[attr-defined]
                stream.close()  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001 — shutdown must not raise
                pass

    # NB: runs on the PortAudio callback thread — keep it allocation-light.
    def _callback(self, outdata: object, frames: int, _time: object, _status: object) -> None:
        import numpy as np

        nbytes = frames * FRAME_BYTES
        if not self._playing:
            fill = self.ring.fill_bytes
            if fill >= self._prebuffer_bytes:
                self._playing = True
                self.state = "playing"
            else:
                self.state = "buffering" if fill else "idle"
                outdata[:] = 0  # type: ignore[index]
                self.level = 0.0
                return
        data, got = self.ring.pull(nbytes)
        samples = np.frombuffer(data, dtype=np.int16).reshape(-1, 2)
        outdata[:] = samples  # type: ignore[index]
        if got == 0:
            self.underrun_blocks += 1
            self._silent_blocks += 1
            self.level = 0.0
            if self._silent_blocks >= self._IDLE_AFTER_BLOCKS:
                # The track drained (or the emulator fell far behind): go back
                # to prebuffering so the next burst starts smoothly.
                self._playing = False
                self.state = "idle"
            else:
                self.state = "underrun"
        else:
            self._silent_blocks = 0
            self.state = "playing"
            self.level = float(np.abs(samples).max()) / 32768.0


# --- The emulation thread --------------------------------------------------------------


@dataclass(frozen=True)
class EmuSnapshot:
    """An immutable status snapshot, republished by the emulation thread."""

    power: str = "starting"  #: starting / booting / running / stopped: <reason>
    clock: int = 0  #: emulated instructions executed
    leaf: int = 0  #: QHsm statechart leaf (frame stack, not the lagging g_state)
    mounted_product: int = 0
    resume_byte: int = 0
    pen_down: bool = False
    chain_active: bool = False  #: firmware audio chain busy (playing/decoding)
    insn_per_s: float = 0.0  #: recent emulation speed (wall)
    speed_ratio: float = 0.0  #: emulated seconds per wall second (1.0 = real time)
    captured_bytes: int = 0
    captured_chunks: int = 0
    capture_rate: int = DEFAULT_RATE
    dac_submits: int = 0
    last_chunk_bytes: int = 0
    last_chunk_clock: int = 0
    taps_fired: int = 0
    firmware_label: str = ""  #: non-empty once a known firmware was recognized
    debug: fw_mt.MtDebugSnapshot | None = None  #: rich debug view (recognized fw only)

    @property
    def leaf_name(self) -> str:
        if self.debug is not None and self.debug.ready:
            return fw_mt.state_name(self.leaf)
        return STATE_NAMES.get(self.leaf, f"state {self.leaf}")


class EmulatorSession:
    """Owns the emulator worker thread and its thread-safe control surfaces.

    The worker builds the machine (the power-on descent boot of
    ``nand-image-layout.md`` §7.3.1a + the provided ``.gme`` files on B:),
    then runs continuously. All mutation of emulator state happens **on the
    worker thread**: public methods only enqueue commands, which the run
    loop's per-chunk callback applies.
    """

    def __init__(
        self,
        firmware_path: str | Path,
        game_paths: Iterable[str | Path] = (),
        *,
        yaml_path: str | Path | None = None,
        instructions_per_tick: int = SESSION_INSTRUCTIONS_PER_TICK,
        max_instructions: int = 1_000_000_000_000,
    ) -> None:
        self.firmware_path = str(firmware_path)
        self._ipt = instructions_per_tick
        self._max_instructions = max_instructions
        self._b_files: dict[str, bytes] = {}
        self.product_code: int | None = None
        self.content_oids: list[int] = []
        for game in game_paths:
            data = Path(game).read_bytes()
            self._b_files[Path(game).name] = data
            if self.product_code is None:
                self.product_code = gme_product_code(data)
                self.content_oids = gme_content_oids(data)

        # tttool YAML symbols (optional; --yaml). Failure degrades to raw numbers.
        self.symbols: TttoolSymbols | None = None
        self._symbols_error: str | None = None
        if yaml_path is not None:
            try:
                self.symbols = load_tttool_yaml(yaml_path)
            except Exception as exc:  # noqa: BLE001 — symbols are best-effort
                self._symbols_error = f"{yaml_path}: {exc}"
        if self.symbols is not None:
            # Join media names: align the YAML's P(...) args with the matching
            # game's binary playlists (firmware-2n-mt.md §5).
            for name, data in self._b_files.items():
                try:
                    scripts = GmeScripts(data)
                except (ValueError, struct.error, IndexError):
                    continue
                if scripts.product == self.symbols.product_id:
                    self.symbols.media_names.update(
                        derive_media_names(self.symbols, scripts)
                    )

        self.ring = AudioRing()
        self.booted: BootedMachine | None = None
        self.snapshot = EmuSnapshot()
        self.debugger: fw_mt.MtDebugger | None = None  #: set on the worker thread
        self._firmware_label = ""

        self._commands: deque[tuple[object, ...]] = deque()  # thread-safe deque
        self._events: deque[str] = deque(maxlen=2000)
        self._events_lock = threading.Lock()
        self._transitions: deque[str] = deque(maxlen=2000)
        self._transitions_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

        # Worker-thread-only state:
        self._pending_taps: deque[int] = deque()
        self._held: tuple[int, int, int] | None = None  # (oid, press clock, gameplay base)
        self._last_lift = 0
        self._book_tail_at: int | None = None  # set by the book-entry-tail hook
        self._button_releases: list[tuple[int, str, int]] = []  # (clock, name, pin)
        self._last_snapshot_clock = 0
        self._last_leaf = -1
        self._last_mounted = 0
        self._last_chain = False
        self._last_chunk: CapturedChunk | None = None
        self._wall_t0 = 0.0
        self._rate_clock = 0
        self._rate_wall = 0.0
        self._insn_per_s = 0.0

    # --- UI-thread API (thread-safe) ---------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="tt-emu", daemon=True)
        self._thread.start()

    def shutdown(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def tap(self, oid: int) -> None:
        """Queue an OID tap (validated here; injected on the emulation thread)."""
        if not 0 <= oid <= 0x3FFFF:
            raise ValueError(f"OID {oid} out of the 18-bit index range")
        self._commands.append(("tap", oid))

    def press_button(self, name: str, *, hold: bool = False) -> None:
        """Queue a physical button press ('power', 'vol+', 'vol-')."""
        if name not in BUTTONS:
            raise ValueError(f"unknown button {name!r}")
        self._commands.append(("button", name, hold))

    def post_event(self, message: str) -> None:
        """Append a line to the event log (thread-safe; also used by logging)."""
        with self._events_lock:
            self._events.append(message)

    def drain_events(self) -> list[str]:
        out: list[str] = []
        with self._events_lock:
            while self._events:
                out.append(self._events.popleft())
        return out

    def drain_transitions(self) -> list[str]:
        """Statechart-transition log lines (recognized firmware only)."""
        out: list[str] = []
        with self._transitions_lock:
            while self._transitions:
                out.append(self._transitions.popleft())
        return out

    # --- worker thread -------------------------------------------------------------------

    def _run(self) -> None:
        self.snapshot = replace(self.snapshot, power="booting")
        self.post_event(f"loading firmware {Path(self.firmware_path).name} …")
        try:
            firmware = load_upd(self.firmware_path)
            # chunk_instructions is pinned to the historical 2000: the
            # interactive tap injection here (unlike runner._TapSession) is
            # not gated on the book-entry settle window, and coarser chunks
            # shift a held tap's first post-book-entry capture into the
            # welcome-jingle window where the firmware discards it.
            booted = build_machine(
                firmware,
                MachineConfig(instructions_per_tick=self._ipt, chunk_instructions=2_000),
                b_files=self._b_files or None,
            )
        except Exception as exc:  # noqa: BLE001 — surface any build failure in the UI
            self.snapshot = replace(self.snapshot, power=f"failed: {exc}")
            self.post_event(f"FAILED to build the machine: {exc}")
            return
        self.booted = booted
        self.post_event(
            f"machine built (build {firmware.build_id}); B: = "
            f"{', '.join(self._b_files) or '(no games)'}"
        )

        # Firmware-aware debugger: only after positively recognizing the image
        # (firmware-2n-mt.md §1). Unrecognized firmware -> generic panels only.
        self._firmware_label = detect_firmware(firmware.prog.data) or ""
        if self._firmware_label:
            game_blobs = list(self._b_files.values())
            self.debugger = fw_mt.MtDebugger(
                booted.machine,
                gme_files=game_blobs,
                symbols=self.symbols,
                log=self._debug_log,
            )
            self.debugger.attach_watches()
            self.post_event(
                f"firmware recognized: {self._firmware_label} — debugger panels enabled"
            )
        else:
            self.post_event("firmware not recognized — generic panels only")
        if self._symbols_error:
            self.post_event(f"WARNING: could not load tttool YAML symbols: {self._symbols_error}")
        elif self.symbols is not None:
            sym = self.symbols
            self.post_event(
                f"symbols: {Path(sym.path).name} (product {sym.product_id}, "
                f"{len(sym.scripts)} scripts, {len(sym.register_names)} registers, "
                f"{len(sym.media_names)} media names)"
            )
        for addr, name in CHECKPOINTS.items():
            booted.machine.on_code(addr, self._make_checkpoint(name))
        for addr, name in FATAL_ADDRS.items():
            booted.machine.on_code(addr, self._make_checkpoint(f"FATAL: {name}", fatal=True))
        booted.machine.on_code(
            BOOK_ENTRY_TAIL_PC, self._make_checkpoint("book entry finished (§7.3.2)")
        )
        booted.machine.on_code(BOOK_ENTRY_TAIL_PC, self._note_book_tail)
        booted.audio.capture.listener = self._on_audio_chunk

        self.snapshot = replace(self.snapshot, power="running")
        self._wall_t0 = time.monotonic()
        self._rate_wall = self._wall_t0
        result = booted.machine.run(self._max_instructions, on_chunk=self._on_chunk)
        self.snapshot = replace(
            self._build_snapshot(booted.machine), power=f"stopped: {result.reason}"
        )
        self.post_event(f"emulation stopped: {result.reason} (pc={result.pc:#010x})")

    def _make_checkpoint(self, name: str, *, fatal: bool = False):  # -> Callable[[Machine], None]
        seen = False

        def hook(machine: Machine) -> None:
            nonlocal seen
            if not seen:
                seen = True
                self.post_event(f"[{machine.clock:>12,}] checkpoint: {name}")
            if fatal:
                # The self-test hang addresses spin forever — stop cleanly
                # (mirrors the headless runner's fatal checkpoints).
                machine.request_stop(name)

        return hook

    def _on_audio_chunk(self, chunk: CapturedChunk) -> None:
        """AudioCapture listener (emulation thread): feed the playback ring."""
        self._last_chunk = chunk
        if chunk.audible:
            self.ring.push(chunk.data)

    def _debug_log(self, message: str) -> None:
        """Debugger watchpoint sink (emulation thread): clock-stamped event."""
        booted = self.booted
        clock = booted.machine.clock if booted is not None else 0
        self.post_event(f"[{clock:>12,}] {message}")

    def _note_book_tail(self, machine: Machine) -> None:
        """Record when book entry's tail ran (§7.3.2) — the earliest a tap mounts."""
        if self._book_tail_at is None:
            self._book_tail_at = machine.clock

    def _ready_for_tap(self, machine: Machine, now: int) -> bool:
        """Gate the product tap on book readiness (mirrors runner._TapSession's
        book-wait): a tap pressed during the book-entry jingle is captured and
        discarded without mounting. Once a product is mounted we are in-game, so
        content taps proceed on the settle gap alone."""
        if self._book_tail_at is None:
            return False
        if machine.read_u32(CURRENT_PRODUCT_ADDR) != 0:
            return True  # already mounted -> in-game, content taps are receptive
        if statechart_leaf(machine) != STATE_BOOK:
            return False
        if now - self._book_tail_at < SETTLE_INSTRUCTIONS:
            return False
        if machine.read_u8(AUDIO_FLAGS_ADDR) & AUDIO_CHAIN_ACTIVE:
            return False  # book-open jingle still draining
        return machine.read_u16(EVENT_RING_HEAD_ADDR) == machine.read_u16(EVENT_RING_TAIL_ADDR)

    def _on_chunk(self, machine: Machine) -> None:
        """Per-chunk callback on the emulation thread: apply commands, observe."""
        if self._stop.is_set():
            machine.request_stop("TUI closed")
            return
        now = machine.clock

        while self._commands:
            self._apply_command(machine, self._commands.popleft())

        # Held-tap lifecycle: lift once the 23-bit gameplay capture latched the
        # frame (one event 0x1060 — holding longer replays the code).
        booted = self.booted
        assert booted is not None
        if self._held is not None:
            oid, pressed_at, gameplay_base = self._held
            if booted.oid.gameplay_frames_served > gameplay_base:
                booted.oid.lift()
                self._held = None
                self._last_lift = now
                self.post_event(f"[{now:>12,}] tap {oid}: latched by the firmware, pen lifted")
            elif now - pressed_at >= TAP_TIMEOUT_INSTRUCTIONS:
                booted.oid.lift()
                self._held = None
                self._last_lift = now
                self.post_event(f"[{now:>12,}] tap {oid}: never latched — pen lifted (timeout)")
        elif (
            self._pending_taps
            and now - self._last_lift >= SETTLE_INSTRUCTIONS
            and self._ready_for_tap(machine, now)
        ):
            oid = self._pending_taps.popleft()
            self._held = (oid, now, booted.oid.gameplay_frames_served)
            booted.oid.hold(oid)
            self.post_event(f"[{now:>12,}] tap {oid}: pen down (press-and-hold)")

        # Scheduled button releases.
        if self._button_releases:
            due = [entry for entry in self._button_releases if entry[0] <= now]
            if due:
                self._button_releases = [e for e in self._button_releases if e[0] > now]
                for _, name, pin in due:
                    booted.gpio.clear_input(pin)
                    self.post_event(f"[{now:>12,}] button {name}: released")

        # Statechart-transition detection: a cheap per-chunk RAM poll of the
        # QHsm (SP, depth, leaf) triple (firmware-2n-mt.md §3, pure-RAM method).
        if self.debugger is not None:
            transition = self.debugger.poll_transition()
            if transition is not None:
                with self._transitions_lock:
                    self._transitions.append(f"[{now:>12,}] {transition.format()}")

        if now - self._last_snapshot_clock >= SNAPSHOT_INTERVAL:
            self._last_snapshot_clock = now
            self.snapshot = self._build_snapshot(machine)

    def _apply_command(self, machine: Machine, command: tuple[object, ...]) -> None:
        booted = self.booted
        assert booted is not None
        if command[0] == "tap":
            oid = cast(int, command[1])
            self._pending_taps.append(oid)
            if self._held is not None or len(self._pending_taps) > 1:
                self.post_event(f"tap {oid}: queued (waiting for the previous tap)")
        elif command[0] == "button":
            name = str(command[1])
            hold = bool(command[2])
            pin, active = BUTTONS[name]
            ticks = BUTTON_HOLD_TICKS if hold else BUTTON_PRESS_TICKS
            booted.gpio.set_input(pin, active)
            self._button_releases.append(
                (machine.clock + ticks * machine.config.instructions_per_tick, name, pin)
            )
            kind = "hold" if hold else "press"
            self.post_event(f"[{machine.clock:>12,}] button {name}: {kind} (GPIO{pin})")

    def _build_snapshot(self, machine: Machine) -> EmuSnapshot:
        booted = self.booted
        assert booted is not None
        now = machine.clock
        wall = time.monotonic()
        if wall - self._rate_wall >= 0.5:
            self._insn_per_s = (now - self._rate_clock) / (wall - self._rate_wall)
            self._rate_clock = now
            self._rate_wall = wall
        leaf = statechart_leaf(machine)
        if leaf != self._last_leaf:
            self._last_leaf = leaf
            if self.debugger is not None:
                name = fw_mt.state_name(leaf)
            else:
                name = STATE_NAMES.get(leaf, f"state {leaf}")
            self.post_event(f"[{now:>12,}] statechart leaf -> {name} ({leaf})")
        mounted = machine.read_u32(CURRENT_PRODUCT_ADDR)
        if mounted != self._last_mounted:
            self._last_mounted = mounted
            self.post_event(f"[{now:>12,}] mounted product -> {mounted}")
        chain = bool(machine.read_u8(AUDIO_FLAGS_ADDR) & AUDIO_CHAIN_ACTIVE)
        if chain != self._last_chain:
            self._last_chain = chain
            self.post_event(
                f"[{now:>12,}] audio chain {'started (play_media)' if chain else 'stopped'}"
            )
        capture = booted.audio.capture
        last = self._last_chunk
        # One emulated second = instructions_per_tick * 50 instructions.
        emu_seconds_per_insn = 1.0 / (machine.config.instructions_per_tick * 50)
        return EmuSnapshot(
            power="running",
            clock=now,
            leaf=leaf,
            mounted_product=mounted,
            resume_byte=machine.read_u8(RESUME_BYTE_ADDR),
            pen_down=booted.oid.pending,
            chain_active=chain,
            insn_per_s=self._insn_per_s,
            speed_ratio=self._insn_per_s * emu_seconds_per_insn,
            captured_bytes=capture.total_bytes,
            captured_chunks=len(capture.chunks),
            capture_rate=capture.wav_rate,
            dac_submits=booted.audio.dac_submits,
            last_chunk_bytes=len(last.data) if last else 0,
            last_chunk_clock=last.clock if last else 0,
            taps_fired=booted.oid.taps_served,
            firmware_label=self._firmware_label,
            debug=self.debugger.snapshot() if self.debugger is not None else None,
        )


class SessionControl(Protocol):
    """What :class:`TtEmuApp` needs from a session (real or test fake)."""

    product_code: int | None
    content_oids: list[int]
    ring: AudioRing
    snapshot: EmuSnapshot

    def start(self) -> None: ...
    def shutdown(self, timeout: float = ...) -> None: ...
    def tap(self, oid: int) -> None: ...
    def press_button(self, name: str, *, hold: bool = ...) -> None: ...
    def drain_events(self) -> list[str]: ...
    def drain_transitions(self) -> list[str]: ...
    def post_event(self, message: str) -> None: ...


# --- The Textual app ---------------------------------------------------------------------


class _SessionLogHandler(logging.Handler):
    """Routes emulator log records into the session's event log (the TUI owns
    the terminal, so nothing may print to stdout/stderr)."""

    def __init__(self, session: SessionControl) -> None:
        super().__init__(level=logging.WARNING)
        self._session = session

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._session.post_event(f"{record.levelname}: {record.getMessage()}")
        except Exception:  # noqa: BLE001 — logging must never raise
            pass


class TtEmuApp(App[None]):
    """The interactive tiptoi emulator: live state, taps, audio, event log."""

    TITLE = "tt-emu — tiptoi pen emulator"

    CSS = """
    #panels { height: 16; }
    #state-panel, #tap-panel, #audio-panel {
        border: round $primary;
        padding: 0 1;
    }
    #state-panel { width: 34%; }
    #tap-panel { width: 33%; }
    #audio-panel { width: 33%; }
    #debug-panels { height: 15; }
    #statechart-panel, #gme-panel, #oid-panel {
        border: round $secondary;
        padding: 0 1;
    }
    #statechart-panel { width: 28%; }
    #gme-panel { width: 38%; }
    #oid-panel { width: 34%; }
    #logs { height: 1fr; }
    #log {
        border: round $primary;
        width: 1fr;
        height: 100%;
    }
    #transitions {
        border: round $secondary;
        width: 44%;
        height: 100%;
    }
    #oid-input { margin-bottom: 1; }
    #quick-taps, #hw-buttons { height: auto; }
    #quick-taps Button, #hw-buttons Button {
        margin-right: 1;
        min-width: 6;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("t", "focus_tap", "Tap an OID"),
        Binding("d", "toggle_debug", "Debug panels"),
        Binding("p", "power", "Power (hold)"),
        Binding("plus", "volume('vol+')", "Vol +"),
        Binding("minus", "volume('vol-')", "Vol -"),
    ]

    def __init__(self, session: SessionControl, audio: AudioOutput | None = None) -> None:
        super().__init__()
        self.session = session
        self.audio = audio
        self._log_handler: _SessionLogHandler | None = None
        #: Debug panels wanted (key 'd' toggles); they only show once the
        #: recognized firmware actually publishes a ready debug snapshot.
        self.debug_enabled = True
        self._debug_shown = True  # composed visible; on_mount hides until ready

    # --- layout --------------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="panels"):
            with Vertical(id="state-panel"):
                yield Static(id="state-body")
            with Vertical(id="tap-panel"):
                yield Input(
                    placeholder="OID code, e.g. 4716 — Enter taps",
                    id="oid-input",
                    type="text",
                )
                with Horizontal(id="quick-taps"):
                    if self.session.product_code is not None:
                        yield Button(
                            f"Product {self.session.product_code}",
                            id=f"tap-{self.session.product_code}",
                            variant="primary",
                        )
                    for oid in self.session.content_oids:
                        yield Button(str(oid), id=f"tap-{oid}")
                with Horizontal(id="hw-buttons"):
                    yield Button("Power", id="btn-power", variant="error")
                    yield Button("Vol +", id="btn-vol-up")
                    yield Button("Vol -", id="btn-vol-down")
            with Vertical(id="audio-panel"):
                yield Static(id="audio-body")
        with Horizontal(id="debug-panels"):
            with Vertical(id="statechart-panel"):
                yield Static(id="statechart-body")
            with Vertical(id="gme-panel"):
                yield Static(id="gme-body")
            with Vertical(id="oid-panel"):
                yield Static(id="oid-body")
        with Horizontal(id="logs"):
            yield RichLog(id="log", markup=False, highlight=False, wrap=True)
            yield RichLog(id="transitions", markup=False, highlight=False, wrap=True)
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#state-panel").border_title = "Pen state"
        self.query_one("#tap-panel").border_title = "Tap"
        self.query_one("#audio-panel").border_title = "Audio"
        self.query_one("#log").border_title = "Events"
        self.query_one("#statechart-panel").border_title = "Statechart"
        self.query_one("#gme-panel").border_title = "GME interpreter"
        self.query_one("#oid-panel").border_title = "OID → script"
        self.query_one("#transitions").border_title = "Transitions"
        self._set_debug_display(False)
        # Keep the event log focused (the pre-debugger auto-focus target) so
        # the single-key bindings ('t', 'd', …) work out of the box.
        self.query_one("#log", RichLog).focus()
        self._log_handler = _SessionLogHandler(self.session)
        logging.getLogger("tt_emu").addHandler(self._log_handler)
        if self.audio is not None and self.audio.start():
            self._log_line(f"audio output open: {self.audio.rate} Hz stereo (sounddevice)")
        elif self.audio is not None:
            self._log_line(f"audio unavailable ({self.audio.error}) — running silent")
        else:
            self._log_line("audio output disabled — running silent")
        self.session.start()
        self.set_interval(0.1, self._refresh)
        self._refresh()

    def on_unmount(self) -> None:
        if self._log_handler is not None:
            logging.getLogger("tt_emu").removeHandler(self._log_handler)
            self._log_handler = None
        self.session.shutdown()
        if self.audio is not None:
            self.audio.close()

    # --- periodic refresh ------------------------------------------------------------------

    def _refresh(self) -> None:
        snap = self.session.snapshot
        self.query_one("#state-body", Static).update(self._state_text(snap))
        self.query_one("#audio-body", Static).update(self._audio_text(snap))
        rich_log = self.query_one("#log", RichLog)
        for line in self.session.drain_events():
            rich_log.write(line)
        self._refresh_debug(snap)

    # --- debugger panels (recognized firmware only) -----------------------------------------

    def _set_debug_display(self, show: bool) -> None:
        if self._debug_shown == show:
            return
        self._debug_shown = show
        self.query_one("#debug-panels").display = show
        self.query_one("#transitions").display = show

    def _refresh_debug(self, snap: EmuSnapshot) -> None:
        debug = snap.debug
        available = debug is not None and debug.ready
        self._set_debug_display(available and self.debug_enabled)
        transitions = self.session.drain_transitions()
        if not available:
            return
        assert debug is not None
        log = self.query_one("#transitions", RichLog)
        for line in transitions:
            log.write(line)  # RichLog buffers while hidden — nothing is lost
        if self._debug_shown:
            self.query_one("#statechart-body", Static).update(self._statechart_text(debug))
            self.query_one("#gme-body", Static).update(self._gme_text(debug))
            self.query_one("#oid-body", Static).update(self._oid_text(debug))

    def _statechart_text(self, debug: fw_mt.MtDebugSnapshot) -> str:
        lines: list[str] = []
        chain = list(zip(debug.chain, debug.chain_names))
        for i, (state, name) in enumerate(chain):
            prefix = "" if i == 0 else "  " * (i - 1) + "└─ "
            entry = f"{prefix}{name} ({state})"
            if i == len(chain) - 1:
                entry = f"[b reverse]{entry}[/]  ◀ active"
            lines.append(entry)
        if not lines:
            lines.append("(statechart not entered yet)")
        lines.append("")
        lines.append(f"[b]Depth[/b] {max(len(chain) - 1, 0)}")
        lines.append(f"[b]Tick[/b] {debug.tick:,}   [b]Heartbeat[/b] {debug.heartbeat:,}")
        return "\n".join(lines)

    def _gme_text(self, debug: fw_mt.MtDebugSnapshot) -> str:
        product = str(debug.product) if debug.product else "(none)"
        if debug.product_label:
            product += f" — {debug.product_label}"
        mounted = "mounted" if debug.gme_mounted else "not mounted"
        if debug.gme_handle == -1:
            handle = "no handle"
        elif debug.gme_handle > 0xFFFF:  # a FILE*-style handle: show as a pointer
            handle = f"handle {debug.gme_handle:#x}"
        else:
            handle = f"handle {debug.gme_handle}"
        path = debug.gme_path or "(no record)"
        lines = [
            f"[b]Product[/b]  {product}",
            f"[b]File[/b]     {path}  ({handle}, {mounted})",
            f"[b]Books[/b]    {debug.book_count} found"
            f"   [b]OIDs[/b] {debug.oid_first}…{debug.oid_last}",
        ]
        now: list[str] = []
        if debug.last_play:
            now.append(debug.last_play)
        now.append("busy" if debug.play_busy else "idle")
        now.append("GME media (XOR)" if debug.xor_active else "system source")
        if debug.playall_mode:
            now.append(f"play-all @{debug.playall_cursor}")
        lines.append(f"[b]Playing[/b]  {', '.join(now)}")
        extras: list[str] = []
        extras.append(
            f"timer slot {debug.timer_slot}" if debug.timer_slot is not None else "timer —"
        )
        if debug.deferred_jump is not None:
            target = debug.deferred_jump_label or str(debug.deferred_jump)
            extras.append(f"Jump → {target} pending (on audio stop)")
        lines.append(f"[b]Timer[/b]    {'   '.join(extras)}")
        if debug.registers:
            lines.append(f"[b]Registers[/b] ({len(debug.registers)}):")
            cells = [
                f"{name}={value}"
                for name, value in zip(debug.register_names, debug.registers)
            ]
            shown = cells[:24]
            if len(cells) > 24:
                shown.append(f"… +{len(cells) - 24} more")
            lines.append("  ".join(shown))
        else:
            lines.append("[b]Registers[/b] (none — no GME mounted)")
        return "\n".join(lines)

    def _oid_text(self, debug: fw_mt.MtDebugSnapshot) -> str:
        if not debug.last_oid:
            return "(no tap decoded yet)"
        routing = debug.routing
        label = f' "{routing.label}"' if routing and routing.label else ""
        lines = [f"[b]Tap[/b]     {debug.last_oid}{label}"
                 f"   [dim]first tap {debug.first_tap_oid}[/dim]"]
        if routing is None:
            return lines[0]
        if routing.kind == "product-band":
            lines.append("[b]Route[/b]   product band (≤999) → mount/classifier")
            return "\n".join(lines)
        if routing.kind == "out-of-range":
            lines.append("[b]Route[/b]   outside the mounted GME's OID range → error voice")
            return "\n".join(lines)
        if routing.kind == "no-script":
            lines.append("[b]Route[/b]   no script at this OID (offset 0xFFFFFFFF)")
            return "\n".join(lines)
        where = (
            f"line {routing.matched_line + 1}/{routing.line_count}"
            if routing.matched_line >= 0
            else f"{routing.line_count} lines (resident line unmatched)"
        )
        script = f"'{routing.label}'" if routing.label else f"OID {routing.oid}"
        lines.append(f"[b]Script[/b]  {script} → {where}")
        if routing.conditions:
            lines.append(f"[b]Cond[/b]    {'; '.join(routing.conditions)}")
        if routing.actions:
            lines.append(f"[b]Actions[/b] {'; '.join(routing.actions)}")
        if routing.playlist:
            lines.append(f"[b]Playlist[/b] {', '.join(routing.playlist)}")
        if routing.source:
            lines.append(f"[b]YAML[/b]    {routing.source}")
        if debug.recent_actions:
            lines.append(f"[dim]exec: {'; '.join(debug.recent_actions[-6:])}[/dim]")
        return "\n".join(lines)

    def _state_text(self, snap: EmuSnapshot) -> str:
        if snap.speed_ratio > 0:
            speed = f"{snap.insn_per_s / 1e6:5.2f} M insn/s ({snap.speed_ratio:.2f}x real time)"
        else:
            speed = "…"
        mounted = str(snap.mounted_product) if snap.mounted_product else "(none)"
        resume = "power-on descent (+0x24=1)" if snap.resume_byte == 1 else f"{snap.resume_byte}"
        return (
            f"[b]Statechart[/b]  {snap.leaf_name} ({snap.leaf})\n"
            f"[b]Power[/b]       {snap.power}\n"
            f"[b]Mounted[/b]     {mounted}\n"
            f"[b]Pen[/b]         {'down' if snap.pen_down else 'up'}"
            f"   [b]Taps[/b] {snap.taps_fired}\n"
            f"[b]Clock[/b]       {snap.clock:,} insn\n"
            f"[b]Speed[/b]       {speed}\n"
            f"[b]Resume[/b]      {resume}"
        )

    def _audio_text(self, snap: EmuSnapshot) -> str:
        if self.audio is None or not self.audio.available:
            reason = self.audio.error if self.audio is not None else "disabled"
            out_line = f"[b]Output[/b]   unavailable ({reason})"
            state = "capture only"
            level_bar = "-" * 16
            underruns = "-"
            buffered = f"{self.session.ring.fill_seconds():.2f} s (never drained)"
        else:
            out_line = f"[b]Output[/b]   {self.audio.rate} Hz stereo, sounddevice"
            state = self.audio.state
            filled = round(self.audio.level * 16)
            level_bar = "#" * filled + "." * (16 - filled)
            underruns = str(self.audio.underrun_blocks)
            buffered = f"{self.session.ring.fill_seconds():.2f} s"
        chain = "playing (chain active)" if snap.chain_active else "idle"
        duration = snap.captured_bytes / (FRAME_BYTES * snap.capture_rate)
        last = (
            f"{snap.last_chunk_bytes} B @ clock {snap.last_chunk_clock:,}"
            if snap.last_chunk_clock
            else "(none yet)"
        )
        note = ""
        if 0 < snap.speed_ratio < 0.9:
            note = (
                f"\n[dim]emulation at {snap.speed_ratio:.2f}x real time — playback"
                f" gaps/underruns are expected, not a bug[/dim]"
            )
        return (
            f"{out_line}\n"
            f"[b]Status[/b]   {state}   [b]Firmware[/b] {chain}\n"
            f"[b]Level[/b]    {level_bar}\n"
            f"[b]Buffered[/b] {buffered}   [b]Underruns[/b] {underruns}\n"
            f"[b]Captured[/b] {snap.captured_chunks} chunks, {duration:.1f} s"
            f" @ {snap.capture_rate} Hz ({snap.dac_submits} DAC submits)\n"
            f"[b]Last[/b]     {last}"
            f"{note}"
        )

    def _log_line(self, message: str) -> None:
        self.query_one("#log", RichLog).write(message)

    # --- input ---------------------------------------------------------------------------

    def _do_tap(self, oid: int) -> None:
        try:
            self.session.tap(oid)
        except ValueError as exc:
            self._log_line(f"tap rejected: {exc}")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        event.input.value = ""
        if not text:
            return
        try:
            oid = int(text, 0)
        except ValueError:
            self._log_line(f"not an OID code: {text!r}")
            return
        self._do_tap(oid)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id or ""
        if bid.startswith("tap-"):
            self._do_tap(int(bid[4:]))
        elif bid == "btn-power":
            self.session.press_button("power", hold=True)
        elif bid == "btn-vol-up":
            self.session.press_button("vol+")
        elif bid == "btn-vol-down":
            self.session.press_button("vol-")

    def action_focus_tap(self) -> None:
        self.query_one("#oid-input", Input).focus()

    def action_toggle_debug(self) -> None:
        self.debug_enabled = not self.debug_enabled
        debug = self.session.snapshot.debug
        if debug is None or not debug.ready:
            self._log_line(
                "debug panels: no recognized firmware state yet"
                + (" (enabled once ready)" if self.debug_enabled else "")
            )
            return
        self._refresh()

    def action_power(self) -> None:
        self.session.press_button("power", hold=True)

    def action_volume(self, name: str) -> None:
        self.session.press_button(name)


# --- entry point ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tt-emu",
        description="Interactive tiptoi 2N ('MT') pen emulator — tap OIDs, hear the audio.",
        epilog="Add --headless for the scripted, no-UI mode: `tt-emu --headless --help`.",
    )
    parser.add_argument(
        "firmware", nargs="?", default=None,
        help="path to the .upd firmware (optional — downloads + caches the "
        "official update3202MT.upd if omitted)",
    )
    parser.add_argument(
        "--firmware-cache", metavar="DIR", default=None,
        help="directory for the downloaded-firmware cache (default: platform cache dir)",
    )
    parser.add_argument(
        "--gme", metavar="GME", action="append", default=[],
        help=".gme file placed on partition B: (repeatable; the first one's "
        "product/content codes become quick-tap buttons)",
    )
    parser.add_argument(
        "--yaml", metavar="YAML", default=None,
        help="tttool source .yaml of the game: joins symbolic names (register "
        "$-names, script/OID labels, media names) into the debugger panels; a "
        "sibling <book>.codes.yaml supplies the script→OID codes",
    )
    parser.add_argument(
        "--instructions-per-tick", type=int, default=SESSION_INSTRUCTIONS_PER_TICK,
        help="emulated instructions per 20 ms tick (default: %(default)s)",
    )
    parser.add_argument(
        "--no-audio", action="store_true", help="disable the sounddevice output stream"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.getLogger().setLevel(logging.WARNING)  # no console handler: TUI owns the tty
    try:
        firmware = str(ensure_firmware(args.firmware, cache_dir=args.firmware_cache))
    except (FirmwareDownloadError, FirmwareIntegrityError) as exc:
        print(f"tt-emu-tui: {exc}", file=sys.stderr)
        return 1
    session = EmulatorSession(
        firmware,
        args.gme,
        yaml_path=args.yaml,
        instructions_per_tick=args.instructions_per_tick,
    )
    audio = None if args.no_audio else AudioOutput(session.ring)
    app = TtEmuApp(session, audio)
    app.run()
    session.shutdown()
    if audio is not None:
        audio.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
