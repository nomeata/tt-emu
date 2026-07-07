"""Synchronous Python scripting API: drive the pen, assert on what plays.

A small, blocking wrapper around the emulator for **automation and testing of
GMEs** — script pen taps and button presses from Python and assert on the audio
the firmware plays, with no threads and no callbacks. It is a sibling of the
interactive :mod:`tt_emu.tui` (which runs the same machine on a background
thread): here every method *advances the emulation and returns*, so a test reads
top-to-bottom.

Nothing about the firmware is hooked or modified (the project's hook-free
principle). Taps are injected through the authentic OID sensor model and gated
on real book-readiness (the shared :func:`tt_emu.runner.book_ready_for_tap`
condition); "what is playing" is read from the media index the firmware's own
interpreter requested (the ``play_media`` / ``fwl_play_voice_by_id`` watchpoints
of ``firmware-2n-mt.md`` §4.5), resolved to a name via the tttool YAML — the
same signals the TUI debugger shows.

Example::

    from tt_emu import Emulator

    with Emulator(gme="taschenrechner.gme", yaml="taschenrechner.yaml") as pen:
        pen.tap("product")               # mount the game (its product code)
        pen.tap("acht")                  # OID by symbolic name (or int code)
        pen.expect_play("acht")          # run until it plays; assert the media
        pen.expect(pen.registers["eingabe"] == 8, "eingabe should be 8")
        pen.wait("400ms")
        pen.tap("neun")
        clip = pen.wait_for_audio()      # the primitive: returns a Clip
        assert clip.media == "neun"      # (pytest-style; -O strips this assert)
        pen.save_wav("session.wav")

Prefer the ``expect_*`` methods in scripts: unlike a bare ``assert`` they are
not stripped by ``python -O``, they carry a domain-specific message, and
:meth:`expect_play` fuses "wait for the next playback" and "check it" into one
intention-revealing statement. The :meth:`wait_for_audio` primitive and the
read-only properties (:attr:`state`, :attr:`registers`, :attr:`now_playing`, …)
remain available for inspection, branching, and flexible pytest asserts.

Style rule for callers: an **action** (:meth:`tap`, :meth:`wait`,
:meth:`wait_for_audio`, …) is always its own statement — never place one inside
an ``assert`` (which ``python -O`` strips). Bind the result to a variable first,
or use :meth:`expect` / :meth:`expect_play`.
"""

from __future__ import annotations

import struct
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Iterator

from unicorn.arm_const import UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R3

from .audio_capture import CHANNELS, FRAME_BYTES, SAMPLE_BYTES, AudioCapture, AudioStats
from .boot import BootedMachine, build_machine
from .firmware import mt as fw_mt
from .firmware.mt import MtDebugger, Transition
from .firmware.symbols import GmeScripts, TttoolSymbols, derive_media_names, load_tttool_yaml
from .firmware_fetch import ensure_firmware
from .loader import load_upd
from .machine import Machine, MachineConfig
from .runner import (
    AUDIO_CHAIN_ACTIVE,
    AUDIO_FLAGS_ADDR,
    BOOK_ENTRY_TAIL_PC,
    CURRENT_PRODUCT_ADDR,
    EVENT_RING_HEAD_ADDR,
    EVENT_RING_TAIL_ADDR,
    SESSION_INSTRUCTIONS_PER_TICK,
    SETTLE_INSTRUCTIONS,
    STATE_NAMES,
    book_ready_for_tap,
    gme_product_code,
    statechart_leaf,
)

__all__ = [
    "Clip",
    "Emulator",
    "ExpectationError",
    "PenTimeout",
    "ScriptingError",
]

#: Emulated milliseconds per statechart tick (``interrupts-and-timers.md``: the
#: 20 ms timer period is the pacing unit).
_TICK_MS = 20

#: Pen buttons (``gpio-buttons-led.md`` §2/§3): name -> (pin, active level).
_BUTTONS: dict[str, tuple[int, int]] = {
    "power": (11, 1),  # active HIGH
    "vol+": (0, 0),    # active LOW
    "vol-": (1, 1),    # active HIGH
}
#: Button press durations in 20 ms ticks (short press < the ~600 ms hold
#: threshold; a hold clears it — power-off needs a hold). Mirrors the TUI.
_BUTTON_PRESS_TICKS = 20
_BUTTON_HOLD_TICKS = 40

#: GME action opcode "Play n" (``firmware-2n-mt.md`` §4.4): play ``playlist[n]``.
_OPCODE_PLAY = 0xFFE8

#: Plausible heap-pointer window for validating ``akoid_buf`` (RAM + headroom).
_PTR_MIN, _PTR_MAX = 0x0800_0000, 0x0844_0000

#: Instruction budgets (at the session cadence these are generous caps; a wait
#: stops as soon as its condition holds, so the cap only bounds a failure).
_BOOT_BUDGET = 500_000_000       #: boot -> book-ready
_READY_BUDGET = 400_000_000      #: wait for tap readiness (incl. audio draining)
_LATCH_BUDGET = 300_000_000      #: wait for the held frame to be latched

#: ``run`` stop reason used to signal "the wait predicate was met".
_SENTINEL = "tt-emu:scripting-condition-met"
#: ``run``'s benign "ran out of budget" reason (a timeout, not a halt).
_BUDGET_EXHAUSTED = "instruction budget exhausted"


class ScriptingError(RuntimeError):
    """Base class for scripting-API errors (bad OID, halted machine, …)."""


class PenTimeout(ScriptingError, TimeoutError):
    """A bounded wait elapsed before its condition held (subclasses the built-in
    :class:`TimeoutError`, so ``except TimeoutError`` catches it)."""


class ExpectationError(AssertionError):
    """An :meth:`Emulator.expect` / :meth:`Emulator.expect_play` check failed.

    Subclasses :class:`AssertionError` so it reads like a failed assertion in
    pytest, but — unlike a bare ``assert`` — it is *not* removed by ``python -O``
    and it always carries a domain-specific message.
    """


@dataclass
class _AudioEvent:
    """One playback the firmware started (a ``play_media`` / voice watchpoint hit)."""

    start_clock: int         #: machine clock when playback began
    index: int | None        #: media-table index (or system-voice id); None if unresolved
    media: str | int         #: media/voice **name** (YAML/voice table) or the raw index
    kind: str                #: "media" (GME) or "voice" (system prompt)


class _Registers:
    """Read-only view of the GME ``$``-register file (``firmware-2n-mt.md`` §4.3).

    Indexable by ``$``-name (with or without the leading ``$``) when a YAML is
    loaded — ``pen.registers["eingabe"]`` — and always by integer index —
    ``pen.registers[0]``. Iterating yields the known names. Empty before a GME
    is mounted.
    """

    def __init__(self, values: tuple[int, ...], names: tuple[str, ...]) -> None:
        self._values = values
        self._names = names
        self._by_name = {name: i for i, name in enumerate(names)}

    def __getitem__(self, key: str | int) -> int:
        if isinstance(key, bool):  # bool is an int subclass; reject it explicitly
            raise KeyError(key)
        if isinstance(key, int):
            return self._values[key]
        name = key[1:] if key.startswith("$") else key
        try:
            return self._values[self._by_name[name]]
        except KeyError:
            raise KeyError(f"no register named {key!r} (known: {', '.join(self._names) or 'none'})") from None

    def __contains__(self, key: object) -> bool:
        if isinstance(key, bool):
            return False
        if isinstance(key, int):
            return 0 <= key < len(self._values)
        if isinstance(key, str):
            name = key[1:] if key.startswith("$") else key
            return name in self._by_name
        return False

    def get(self, key: str | int, default: int | None = None) -> int | None:
        try:
            return self[key]
        except (KeyError, IndexError):
            return default

    def __iter__(self) -> Iterator[str]:
        return iter(self._names)

    def __len__(self) -> int:
        return len(self._values)

    def keys(self) -> tuple[str, ...]:
        return self._names

    def items(self) -> list[tuple[str, int]]:
        return [(name, self._values[i]) for i, name in enumerate(self._names)]

    def as_dict(self) -> dict[str, int]:
        return dict(self.items())

    def __repr__(self) -> str:
        if self._names:
            body = ", ".join(f"{name}={self._values[i]}" for i, name in enumerate(self._names))
        else:
            body = ", ".join(f"${i}={v}" for i, v in enumerate(self._values))
        return f"registers({body})"


class Clip:
    """One media playback, as started by the firmware and captured off the DAC.

    Returned by :meth:`Emulator.wait_for_audio` / :meth:`Emulator.expect_play`.
    :attr:`media` and :attr:`index` are known the instant playback starts; the
    PCM is captured as the emulation runs on, so :attr:`pcm` / :attr:`duration`
    / :meth:`save_wav` reflect whatever has been captured for this playback's
    window so far (bounded by the start of the next playback).
    """

    def __init__(
        self,
        emulator: "Emulator",
        *,
        media: str | int,
        index: int | None,
        kind: str,
        start_clock: int,
    ) -> None:
        self._emu = emulator
        #: The media **name** (when a YAML is loaded) or the raw media index.
        self.media = media
        #: The media-table index the firmware requested (or a system-voice id).
        self.index = index
        #: "media" for a GME media file, "voice" for a system voice prompt.
        self.kind = kind
        self._start = start_clock

    @property
    def _end_clock(self) -> int:
        """Where this clip's captured window ends: the next playback's start, or
        the current machine clock if this is the latest playback."""
        later = [e.start_clock for e in self._emu._audio_events if e.start_clock > self._start]
        return min(later) if later else self._emu.machine.clock

    @property
    def rate(self) -> int:
        """The capture sample rate (Hz)."""
        return self._emu._capture.wav_rate

    @property
    def pcm(self) -> bytes:
        """The captured S16LE stereo PCM of this playback (may grow as it plays)."""
        end = self._end_clock
        return b"".join(
            c.data for c in self._emu._capture.chunks if self._start <= c.clock < end
        )

    @property
    def duration(self) -> float:
        """Length of :attr:`pcm` in seconds."""
        rate = self.rate
        frames = len(self.pcm) // FRAME_BYTES
        return frames / rate if rate else 0.0

    def save_wav(self, path: str | Path) -> None:
        """Write just this clip's PCM as an S16LE stereo WAV."""
        with wave.open(str(path), "wb") as w:
            w.setnchannels(CHANNELS)
            w.setsampwidth(SAMPLE_BYTES)
            w.setframerate(self.rate)
            w.writeframes(self.pcm)

    def __repr__(self) -> str:
        return f"Clip(media={self.media!r}, index={self.index}, kind={self.kind!r})"


class Emulator:
    """A scripted tiptoi pen: a context manager you tap and assert on.

    Construct with the game(s) to mount and (optionally) the tttool YAML for
    symbolic names, then use it as a context manager — :meth:`__enter__` boots
    the pen through the authentic power-on descent into book mode and returns
    ``self``; :meth:`__exit__` shuts it down::

        with Emulator(gme="game.gme", yaml="game.yaml") as pen:
            pen.tap("product")
            ...

    Parameters
    ----------
    firmware:
        Path to the ``.upd`` firmware, or ``None`` to auto-download and cache
        the official 2N "MT" image (:func:`tt_emu.firmware_fetch.ensure_firmware`).
    gme, gmes:
        The ``.gme`` file(s) to place on partition B:. ``gme`` is a single file;
        ``gmes`` an iterable of files. The first game's product code is what
        ``tap("product")`` mounts, and (with a matching YAML) supplies the
        symbolic names.
    yaml:
        The tttool source ``.yaml`` of the (first) game — joins symbolic OID /
        script / register / media names. A sibling ``*.codes.yaml`` supplies the
        script→OID codes. Without it the API degrades to raw indices.
    instructions_per_tick:
        Emulation pacing (default: the session cadence, ~50 MIPS-equivalent,
        which the boot→tap→play chain needs for realistic firmware timing).
    """

    def __init__(
        self,
        firmware: str | Path | None = None,
        *,
        gme: str | Path | None = None,
        gmes: Iterable[str | Path] = (),
        yaml: str | Path | None = None,
        instructions_per_tick: int = SESSION_INSTRUCTIONS_PER_TICK,
        dac_pacing: str = "fast",
    ) -> None:
        self._firmware = firmware
        self._ipt = instructions_per_tick
        self._dac_pacing = dac_pacing

        game_paths: list[Path] = []
        if gme is not None:
            game_paths.append(Path(gme))
        game_paths.extend(Path(g) for g in gmes)
        self._b_files: dict[str, bytes] = {}
        self._product_code: int | None = None
        for path in game_paths:
            data = path.read_bytes()
            self._b_files[path.name] = data
            if self._product_code is None:
                self._product_code = gme_product_code(data)

        # tttool YAML symbols (optional); join media names against the games.
        self._symbols: TttoolSymbols | None = None
        if yaml is not None:
            self._symbols = load_tttool_yaml(yaml)
            for data in self._b_files.values():
                try:
                    scripts = GmeScripts(data)
                except (ValueError, struct.error, IndexError):
                    continue
                if scripts.product == self._symbols.product_id:
                    self._symbols.media_names.update(derive_media_names(self._symbols, scripts))

        # Set on __enter__:
        self._booted: BootedMachine | None = None
        self.machine: Machine = None  # type: ignore[assignment]
        self._capture: AudioCapture = None  # type: ignore[assignment]
        self._debugger: MtDebugger | None = None
        self._audio_events: list[_AudioEvent] = []
        self._transitions: list[Transition] = []
        self._book_tail_at: int | None = None
        self._last_lift = 0
        self._last_play_action: tuple[int, int] | None = None
        self._halted: str | None = None
        self._entered = False

    # --- context management ------------------------------------------------------------

    def __enter__(self) -> "Emulator":
        firmware_path = ensure_firmware(self._firmware)
        firmware = load_upd(str(firmware_path))
        config = MachineConfig(instructions_per_tick=self._ipt, dac_pacing=self._dac_pacing)
        booted = build_machine(firmware, config, b_files=self._b_files or None)
        self._booted = booted
        self.machine = booted.machine
        self._capture = booted.audio.capture

        if fw_mt.recognize(firmware.prog.data):
            self._debugger = MtDebugger(
                self.machine,
                gme_files=list(self._b_files.values()),
                symbols=self._symbols,
            )

        # Read-only observation watchpoints (firmware unmodified): the
        # book-entry tail marker and the media/voice "now playing" watches.
        machine = self.machine
        machine.on_code(BOOK_ENTRY_TAIL_PC, self._on_book_tail)
        machine.on_code(fw_mt.PC_GME_EXEC_COMMAND, self._on_exec_command)
        machine.on_code(fw_mt.PC_PLAY_MEDIA, self._on_play_media)
        machine.on_code(fw_mt.PC_PLAY_VOICE, self._on_play_voice)

        # Power-on descent into book mode; ready for the first (product) tap.
        if not self._run_until(self._ready_for_tap, budget=_BOOT_BUDGET):
            reason = self._halted or f"still {self.state!r} after the boot budget"
            raise ScriptingError(f"pen did not reach book mode ({reason})")
        self._entered = True
        return self

    def __exit__(self, *exc: object) -> None:
        # Nothing to join (single-threaded); mark the machine stopped so a
        # stray later run() is a clear error rather than undefined behaviour.
        if self.machine is not None and self._halted is None:
            self.machine.request_stop("emulator context exited")
            self._halted = "emulator context exited"

    # --- the synchronous stepping primitive --------------------------------------------

    def _run_until(self, predicate: Callable[[], bool], *, budget: int) -> bool:
        """Emulate forward until ``predicate()`` holds or ``budget`` is spent.

        Returns True if the predicate was met, False on the budget timeout.
        Per-chunk bookkeeping (transition log; the media watches fire inside the
        chunk) runs every chunk regardless.
        """
        if self._halted is not None:
            raise ScriptingError(f"machine halted: {self._halted}")
        machine = self.machine
        machine.clear_stop()
        met = False

        def on_chunk(m: Machine) -> None:
            nonlocal met
            if self._debugger is not None:
                transition = self._debugger.poll_transition()
                if transition is not None:
                    self._transitions.append(transition)
            if not met and predicate():
                met = True
                m.request_stop(_SENTINEL)

        result = machine.run(budget, on_chunk=on_chunk)
        if result.reason not in (_SENTINEL, _BUDGET_EXHAUSTED):
            self._halted = result.reason  # power-off / fault / fatal checkpoint
        return met

    def _advance(self, instructions: int) -> None:
        """Run ``instructions`` forward unconditionally (used by :meth:`wait`)."""
        self._run_until(lambda: False, budget=max(1, instructions))

    def _duration_to_instructions(self, duration: str | int) -> int:
        """``"400ms"`` / ``"2s"`` / an int number of 20 ms ticks -> instructions."""
        if isinstance(duration, bool):
            raise TypeError("duration must be a str like '400ms'/'2s' or an int number of ticks")
        if isinstance(duration, int):
            ticks: float = duration
        elif isinstance(duration, str):
            text = duration.strip().lower()
            try:
                if text.endswith("ms"):
                    ms = float(text[:-2])
                elif text.endswith("s"):
                    ms = float(text[:-1]) * 1000.0
                else:
                    raise ValueError
            except ValueError:
                raise ValueError(f"cannot parse duration {duration!r} (use e.g. '400ms', '2s', or an int)") from None
            ticks = ms / _TICK_MS
        else:
            raise TypeError(f"duration must be str or int, not {type(duration).__name__}")
        return max(1, int(ticks * self._ipt))

    # --- watchpoint sinks (observation only) -------------------------------------------

    def _on_book_tail(self, machine: Machine) -> None:
        if self._book_tail_at is None:
            self._book_tail_at = machine.clock

    def _on_exec_command(self, machine: Machine) -> None:
        """Record the just-executed GME action's (opcode, operand) so the next
        ``play_media`` can resolve which playlist entry is being played."""
        uc = machine.uc
        self._last_play_action = (uc.reg_read(UC_ARM_REG_R1), uc.reg_read(UC_ARM_REG_R3))

    def _on_play_media(self, machine: Machine) -> None:
        index = self._resolve_media_index(machine)
        media = self._media_display(index)
        self._audio_events.append(_AudioEvent(machine.clock, index, media, "media"))
        self._last_play_action = None

    def _on_play_voice(self, machine: Machine) -> None:
        voice = machine.uc.reg_read(UC_ARM_REG_R0)
        label = fw_mt.VOICE_NAMES.get(voice, f"voice {voice:#x}")
        self._audio_events.append(_AudioEvent(machine.clock, voice, label, "voice"))

    def _read_playlist(self, machine: Machine) -> tuple[int, ...]:
        length = min(machine.read_u16(fw_mt.PLAYLIST_LEN_ADDR), fw_mt.MAX_PLAYLIST)
        if length <= 0:
            return ()
        raw = machine.read_bytes(fw_mt.PLAYLIST_ADDR, 2 * length)
        return struct.unpack(f"<{length}H", raw)

    def _resolve_media_index(self, machine: Machine) -> int | None:
        """The media-table index the firmware just asked to play.

        Primary: the resident playlist indexed by the just-executed ``Play n``
        action's operand (``firmware-2n-mt.md`` §4.4 "Play n → playlist[n]") —
        the same join the debugger's routing panel renders. Falls back to the
        play-all cursor, then the first playlist entry; None if the playlist is
        empty.
        """
        playlist = self._read_playlist(machine)
        action = self._last_play_action
        if action is not None:
            opcode, operand = action
            if opcode == _OPCODE_PLAY and 0 <= operand < len(playlist):
                return playlist[operand]
        akoid = machine.read_u32(fw_mt.AKOID_PTR)
        if _PTR_MIN <= akoid < _PTR_MAX and machine.read_u8(akoid + fw_mt.AKOID_PLAYALL_MODE):
            cursor = machine.read_u8(akoid + fw_mt.AKOID_PLAYALL_CURSOR)
            if 0 <= cursor < len(playlist):
                return playlist[cursor]
        return playlist[0] if playlist else None

    def _media_display(self, index: int | None) -> str | int:
        """A media index resolved to its YAML name, else the raw index."""
        if index is None:
            return -1
        if self._symbols is not None and index in self._symbols.media_names:
            return self._symbols.media_names[index]
        return index

    # --- OID resolution ----------------------------------------------------------------

    def _resolve_oid(self, oid: int | str) -> int:
        """An int code, ``"product"``, or a symbolic OID/script name -> OID code."""
        if isinstance(oid, bool):
            raise TypeError("oid must be an int code or a str name, not bool")
        if isinstance(oid, int):
            return oid
        if oid == "product":
            if self._product_code is None:
                raise ScriptingError("no game mounted: tap('product') needs a .gme (gme=…)")
            return self._product_code
        if self._symbols is not None:
            code = self._symbols.codes.get(oid)
            if code is not None:
                return code
        try:
            return int(oid, 0)
        except ValueError:
            known = ", ".join(sorted(self._symbols.codes)) if self._symbols else "(no YAML loaded)"
            raise ScriptingError(f"unknown OID name {oid!r}; known scripts: {known}") from None

    def _ready_for_tap(self) -> bool:
        """Book-readiness (shared gate) plus a clean audio boundary + settle gap.

        Beyond :func:`tt_emu.runner.book_ready_for_tap` this also requires the
        audio chain to be idle even after a game is mounted, so a scripted tap
        never lands mid-playback and each :meth:`wait_for_audio` sees a clean new
        clip. The settle gap since the last lift spaces taps as on real HW
        (``nand-image-layout.md`` §7.3.2).
        """
        machine = self.machine
        if machine.clock - self._last_lift < SETTLE_INSTRUCTIONS:
            return False
        if not book_ready_for_tap(machine, self._book_tail_at):
            return False
        if machine.read_u8(AUDIO_FLAGS_ADDR) & AUDIO_CHAIN_ACTIVE:
            return False  # a playback is still in progress -> not a clean boundary
        return machine.read_u16(EVENT_RING_HEAD_ADDR) == machine.read_u16(EVENT_RING_TAIL_ADDR)

    # --- actions -----------------------------------------------------------------------

    def tap(self, oid: int | str) -> None:
        """Tap an OID: press-and-hold through the sensor model until served, then lift.

        ``oid`` is an int OID code, the string ``"product"`` (the mounted game's
        product code), or a symbolic OID/script name resolved via the YAML. The
        tap is gated on book readiness (a tap during the book-entry jingle is
        discarded) and on a clean audio boundary, so it reliably mounts the game
        / runs the script. Returns once the firmware's own capture has latched
        the frame; raises :class:`PenTimeout` if it never does.
        """
        code = self._resolve_oid(oid)
        booted = self._booted
        assert booted is not None
        if not self._run_until(self._ready_for_tap, budget=_READY_BUDGET):
            raise PenTimeout(f"pen not ready to tap {oid!r} within budget (state={self.state!r})")

        mounted_before = self.machine.read_u32(CURRENT_PRODUCT_ADDR)
        audio_before = len(self._audio_events)
        base = booted.oid.gameplay_frames_served
        booted.oid.hold(code)
        latched = self._run_until(
            lambda: booted.oid.gameplay_frames_served > base, budget=_LATCH_BUDGET
        )
        booted.oid.lift()
        self._last_lift = self.machine.clock
        if not latched:
            raise PenTimeout(f"tap {oid!r} (OID {code}) was never latched by the firmware")

        # A product-band tap mounts a game: wait for that reaction (the product
        # id changes, and its welcome playlist begins) so the game is actually
        # mounted when tap() returns. A content tap deliberately does *not* wait
        # for its playback — it returns before the media starts, so the next
        # wait_for_audio() sees the fresh clip (nand-image-layout.md §7.3 tap 2
        # mounts; tap 3 plays).
        if code <= fw_mt.PRODUCT_OID_MAX:
            self._run_until(
                lambda: self.machine.read_u32(CURRENT_PRODUCT_ADDR) != mounted_before
                or len(self._audio_events) > audio_before,
                budget=_LATCH_BUDGET,
            )

    def wait(self, duration: str | int) -> None:
        """Advance emulated time: ``"400ms"`` / ``"2s"`` / an int number of 20 ms ticks."""
        self._advance(self._duration_to_instructions(duration))

    def wait_until(self, predicate: Callable[[], bool], timeout: str | int = "10s") -> None:
        """Run until ``predicate()`` is true; raise :class:`PenTimeout` on timeout.

        Example (the predicate reads a property — it is not an action)::

            pen.wait_until(lambda: pen.registers["eingabe"] == 8)
        """
        if not self._run_until(predicate, budget=self._duration_to_instructions(timeout)):
            raise PenTimeout(f"predicate not satisfied within {timeout!r} (state={self.state!r})")

    def wait_for_audio(self, timeout: str | int = "10s") -> Clip:
        """Run until the firmware starts the next playback; return its :class:`Clip`.

        Raises :class:`PenTimeout` if no playback starts within ``timeout``.
        """
        baseline = len(self._audio_events)
        if not self._run_until(
            lambda: len(self._audio_events) > baseline,
            budget=self._duration_to_instructions(timeout),
        ):
            raise PenTimeout(
                f"no audio playback started within {timeout!r} "
                f"(state={self.state!r}, now_playing={self.now_playing!r})"
            )
        event = self._audio_events[baseline]
        return Clip(
            self, media=event.media, index=event.index, kind=event.kind, start_clock=event.start_clock
        )

    def press(self, button: str, *, hold: bool = False) -> None:
        """Press a physical button: ``"power"`` / ``"vol+"`` / ``"vol-"``.

        ``hold=True`` holds past the ~600 ms threshold (needed for power-off).
        """
        if button not in _BUTTONS:
            raise ScriptingError(f"unknown button {button!r} (use 'power', 'vol+', or 'vol-')")
        assert self._booted is not None
        pin, active = _BUTTONS[button]
        ticks = _BUTTON_HOLD_TICKS if hold else _BUTTON_PRESS_TICKS
        self._booted.gpio.set_input(pin, active)
        self._advance(ticks * self._ipt)
        self._booted.gpio.clear_input(pin)
        self._advance(self._ipt)  # let the release register

    # --- expectations (the -O-safe, message-carrying assert style) ---------------------

    def expect(self, condition: bool, message: str = "") -> None:
        """Assert ``condition`` is truthy; raise :class:`ExpectationError` otherwise.

        Unlike a bare ``assert`` this survives ``python -O`` and carries a
        message. The condition should read a property or a value already bound to
        a variable — never call an action inside it. Example::

            pen.expect(pen.registers["eingabe"] == 8, "eingabe should be 8")
        """
        if not condition:
            raise ExpectationError(message or "expectation failed")

    def expect_play(self, media: str | int, timeout: str | int = "10s") -> Clip:
        """Wait for the next playback and assert it is ``media``; return its :class:`Clip`.

        Fuses :meth:`wait_for_audio` and the media check into one statement.
        ``media`` matches either the resolved media **name** or the raw index.
        Raises :class:`ExpectationError` on a mismatch or on timeout (the message
        names expected vs. actual media and the current state).
        """
        try:
            clip = self.wait_for_audio(timeout=timeout)
        except PenTimeout as exc:
            raise ExpectationError(
                f"expected playback of {media!r} but none started within {timeout!r} "
                f"(state={self.state!r}, now_playing={self.now_playing!r})"
            ) from exc
        if clip.media != media and clip.index != media:
            raise ExpectationError(
                f"expected playback of {media!r} but got {clip.media!r} "
                f"(index {clip.index}, kind {clip.kind}, state={self.state!r})"
            )
        return clip

    def save_wav(self, path: str | Path) -> AudioStats:
        """Write everything captured off the DAC so far as an S16LE stereo WAV."""
        return self._capture.write_wav(path)

    # --- read-only properties (pure, no side effects) ----------------------------------

    def _leaf_chain(self) -> tuple[int, ...]:
        if self._debugger is not None:
            chain = self._debugger.state_chain()
            if chain:
                return chain
        leaf = statechart_leaf(self.machine)
        return (leaf,) if leaf else ()

    def _state_name(self, state: int) -> str:
        if self._debugger is not None:
            return fw_mt.state_name(state)
        return STATE_NAMES.get(state, f"state{state}")

    @property
    def state(self) -> str:
        """The current statechart leaf name (e.g. ``"book"``)."""
        chain = self._leaf_chain()
        return self._state_name(chain[-1]) if chain else "(pre-init)"

    @property
    def state_chain(self) -> tuple[str, ...]:
        """The statechart parent chain as names, root -> leaf."""
        return tuple(self._state_name(s) for s in self._leaf_chain())

    @property
    def registers(self) -> _Registers:
        """The GME ``$``-register file (by ``$``-name with a YAML, else by index)."""
        if self._debugger is None:
            return _Registers((), ())
        values = self._debugger.registers()
        if self._symbols is not None:
            names = tuple(
                self._symbols.register_names[i] if i < len(self._symbols.register_names) else str(i)
                for i in range(len(values))
            )
        else:
            names = ()
        return _Registers(values, names)

    @property
    def mounted(self) -> int | None:
        """The mounted product id, or None if no game is mounted."""
        product = self.machine.read_u32(CURRENT_PRODUCT_ADDR)
        return product or None

    @property
    def now_playing(self) -> str | int | None:
        """The last playback's media name/index, or None if nothing has played."""
        return self._audio_events[-1].media if self._audio_events else None

    @property
    def transitions(self) -> tuple[Transition, ...]:
        """The statechart transition log (poll-detected during the run)."""
        return tuple(self._transitions)
