"""TUI plumbing tests: audio ring/queue, live-capture listener, app construction.

Everything here is headless-safe: the Textual app is driven through its test
pilot (no real terminal) and no audio device is required — :class:`AudioOutput`
must *degrade*, never crash, when PortAudio or an output device is missing.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from _data import firmware_path, game_dir
from _pilot import run_app, wait_for

from tt_emu.audio_capture import AudioCapture
from tt_emu.runner import gme_product_code
from tt_emu.tui import (
    AudioOutput,
    AudioRing,
    EmulatorSession,
    EmuSnapshot,
    TtEmuApp,
    gme_content_oids,
)

_GAME_DIR = game_dir()
UPD_PATH = firmware_path()
GME_PATH = _GAME_DIR / "taschenrechner.gme" if _GAME_DIR is not None else None


# --- audio ring / queue plumbing -----------------------------------------------------


def test_audio_ring_roundtrip_and_silence_padding() -> None:
    ring = AudioRing()
    ring.push(b"\x01\x02\x03\x04")
    ring.push(b"\x05\x06")
    data, got = ring.pull(4)
    assert (data, got) == (b"\x01\x02\x03\x04", 4)
    # Short read: real bytes first, then silence padding; got reports the truth.
    data, got = ring.pull(6)
    assert data == b"\x05\x06" + b"\x00" * 4
    assert got == 2
    # Empty ring: pure silence, got == 0 (the underrun signal).
    data, got = ring.pull(8)
    assert data == b"\x00" * 8
    assert got == 0
    assert ring.pushed_bytes == 6
    assert ring.pulled_bytes == 6


def test_audio_ring_is_bounded() -> None:
    ring = AudioRing(max_seconds=0.001, rate=1000)  # 1 frame = 4 bytes cap
    ring.push(b"a" * 4)
    ring.push(b"b" * 4)  # evicts the first chunk
    assert ring.dropped_bytes == 4
    data, got = ring.pull(4)
    assert (data, got) == (b"b" * 4, 4)


def test_capture_listener_receives_live_chunks() -> None:
    capture = AudioCapture()
    seen: list[bytes] = []
    capture.listener = lambda chunk: seen.append(chunk.data)
    capture.append(100, 22050, b"\x11\x22\x33\x44")
    capture.append(200, 22050, b"\x55\x66\x77\x88", audible=False)
    assert seen == [b"\x11\x22\x33\x44", b"\x55\x66\x77\x88"]
    assert len(capture.chunks) == 2  # capture behaviour unchanged


def test_capture_listener_feeds_the_ring_only_when_audible() -> None:
    session = _FakeSession()
    capture = AudioCapture()
    # Wire the same listener EmulatorSession uses (bound method semantics).
    capture.listener = EmulatorSession._on_audio_chunk.__get__(session)
    capture.append(1, 22050, b"\x01\x02\x03\x04", audible=True)
    capture.append(2, 22050, b"\x05\x06\x07\x08", audible=False)  # muted: not played
    data, got = session.ring.pull(8)
    assert got == 4
    assert data.startswith(b"\x01\x02\x03\x04")


def test_audio_output_degrades_without_a_device() -> None:
    ring = AudioRing()
    out = AudioOutput(ring)
    ok = out.start()  # must never raise, whatever the host audio situation
    assert ok == out.available
    if not out.available:
        assert out.error  # the reason is reported to the UI
        assert out.state == "off"
    out.close()
    assert not out.available


# --- GME quick-tap parsing ------------------------------------------------------------


def test_gme_content_oids_rejects_garbage() -> None:
    assert gme_content_oids(b"\x00" * 64) == []
    assert gme_content_oids(b"\xff" * 8) == []


@pytest.mark.skipif(GME_PATH is None, reason="taschenrechner.gme not available")
def test_gme_content_oids_taschenrechner() -> None:
    data = GME_PATH.read_bytes()
    oids = gme_content_oids(data, limit=3)
    assert oids == [4716, 4717, 4718]
    assert gme_product_code(data) == 42


# --- the Textual app (pilot harness, no real terminal) ---------------------------------


class _FakeSession:
    """A SessionControl stand-in: records control calls, no emulator needed."""

    def __init__(self) -> None:
        self.product_code: int | None = 42
        self.content_oids = [4716, 4717]
        self.symbols: object | None = None  # no tttool YAML in the fake
        self.ring = AudioRing()
        self.snapshot = EmuSnapshot()
        self.taps: list[int] = []
        self.buttons: list[tuple[str, bool]] = []
        self.started = False
        self.stopped = False
        self._events: list[str] = []

    def start(self) -> None:
        self.started = True

    def shutdown(self, timeout: float = 5.0) -> None:
        self.stopped = True

    def tap(self, oid: int) -> None:
        if not 0 <= oid <= 0x3FFFF:
            raise ValueError(f"OID {oid} out of range")
        self.taps.append(oid)

    def press_button(self, name: str, *, hold: bool = False) -> None:
        self.buttons.append((name, hold))

    def drain_events(self) -> list[str]:
        out, self._events = self._events, []
        return out

    def drain_transitions(self) -> list[str]:
        out, self._transitions = getattr(self, "_transitions", []), []
        return out

    def post_event(self, message: str) -> None:
        self._events.append(message)


def test_app_builds_and_injects_taps_via_pilot() -> None:
    async def scenario() -> None:
        fake = _FakeSession()
        app = TtEmuApp(fake, audio=None)
        async with run_app(app, size=(100, 40)) as pilot:
            assert fake.started  # the emulation "thread" was started on mount
            # Wait for the tap panel to be mounted before driving it.
            await wait_for(pilot, app, "#oid-input")
            # Type an OID into the tap input and submit it.
            await pilot.press("t")  # binding: focus the tap input
            await pilot.press(*"4716", "enter")
            assert fake.taps == [4716]
            # The scrollable tap list holds a row per OID; selecting the product
            # row (Enter on the highlighted option) taps it.
            tap_list = await wait_for(
                pilot, app, "#tap-list", ready=lambda w: w.option_count >= 2
            )
            tap_list.focus()
            await pilot.pause()
            tap_list.highlighted = tap_list.get_option_index("tap-42")
            await pilot.press("enter")
            assert fake.taps == [4716, 42]
            # Out-of-range input is rejected gracefully (no crash).
            await pilot.press("t")
            await pilot.press(*"999999", "enter")
            assert fake.taps == [4716, 42]
            # Physical buttons: power is a hold, volume a short press.
            await pilot.click("#btn-power")
            await pilot.click("#btn-vol-up")
            assert fake.buttons == [("power", True), ("vol+", False)]
        assert fake.stopped  # shutdown ran on unmount

    asyncio.run(scenario())


def test_app_tap_list_sort_and_lru() -> None:
    async def scenario() -> None:
        fake = _FakeSession()
        fake.content_oids = [4718, 4716, 4717]  # deliberately out of order
        app = TtEmuApp(fake, audio=None)
        async with run_app(app, size=(100, 40)) as pilot:
            tap_list = await wait_for(
                pilot, app, "#tap-list", ready=lambda w: w.option_count == 4
            )

            def codes() -> list[str]:
                return [
                    tap_list.get_option_at_index(i).id or ""
                    for i in range(tap_list.option_count)
                ]

            # Default sort = by OID: product pinned first, then ascending OIDs.
            assert codes() == ["tap-42", "tap-4716", "tap-4717", "tap-4718"]
            assert "sort: OID" in str(app.query_one("#tap-panel").border_title)
            # Tap 4718 from the list (records recency), then cycle to 'recent'.
            tap_list.focus()
            await pilot.pause()
            tap_list.highlighted = tap_list.get_option_index("tap-4718")
            await pilot.press("enter")
            assert fake.taps == [4718]
            await pilot.press("s")  # OID -> name
            await pilot.press("s")  # name -> recent
            assert "sort: recent" in str(app.query_one("#tap-panel").border_title)
            # LRU: the just-tapped 4718 jumps to the top (after the pinned product).
            assert codes()[:2] == ["tap-42", "tap-4718"]

    asyncio.run(scenario())


def test_app_runs_without_audio_output() -> None:
    async def scenario() -> None:
        fake = _FakeSession()
        out = AudioOutput(fake.ring)  # on a machine with no device this degrades
        app = TtEmuApp(fake, audio=out)
        async with run_app(app, size=(100, 40)) as pilot:
            # Wait until a refresh has actually rendered both panels.
            await wait_for(
                pilot, app, "#state-body", ready=lambda w: str(w.content).strip() != ""
            )
            await wait_for(
                pilot, app, "#audio-body", ready=lambda w: str(w.content).strip() != ""
            )
        out.close()

    asyncio.run(scenario())


# --- the real emulation thread (needs the firmware; boot only briefly) ------------------


@pytest.mark.skipif(
    UPD_PATH is None or GME_PATH is None,
    reason="firmware .upd / taschenrechner.gme not available",
)
def test_emulator_session_thread_boots_and_stops() -> None:
    session = EmulatorSession(UPD_PATH, [GME_PATH])
    assert session.product_code == 42
    assert session.content_oids[:1] == [4716]
    session.tap(4716)  # queued commands are safe before the thread runs
    session.start()
    deadline = time.monotonic() + 60.0
    while session.snapshot.clock < 2_000_000:
        assert time.monotonic() < deadline, session.snapshot
        assert session.running or session.snapshot.power.startswith("stopped"), (
            session.snapshot
        )
        time.sleep(0.05)
    session.shutdown(timeout=20.0)
    assert not session.running
    assert session.snapshot.clock >= 2_000_000
    events = session.drain_events()
    assert any("checkpoint" in e for e in events), events
