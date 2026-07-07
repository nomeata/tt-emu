"""Buffered one-sound-at-a-time audio: ring drain, boundary accumulator, player.

None of these tests need sounddevice / a real output device: the accumulator's
boundary logic is exercised through the split-out :meth:`AudioOutput._accumulate_step`
with a fake clock, and the player path is driven with a stubbed stream factory.
"""
from __future__ import annotations

import struct

from tt_emu.audio_capture import FRAME_BYTES
from tt_emu.tui import AudioOutput, AudioRing, _peak_level


def _pcm(frames: int, sample: int = 20000) -> bytes:
    """`frames` of interleaved S16LE stereo at a constant sample value."""
    return struct.pack(f"<{frames * 2}h", *([sample, -sample] * frames))


def test_peak_level() -> None:
    assert _peak_level(b"") == 0.0
    assert _peak_level(b"\x00" * 32) == 0.0
    # A full-scale negative sample clamps to 1.0.
    assert _peak_level(struct.pack("<4h", 100, -32768, 50, 0)) == 1.0
    # A mid-level peak resolves proportionally.
    assert abs(_peak_level(struct.pack("<4h", 8192, 0, -4096, 0)) - 8192 / 32768) < 1e-6


def test_ring_drain() -> None:
    ring = AudioRing()
    # Empty ring drains to nothing.
    assert ring.drain() == b""

    a, b = _pcm(4), _pcm(2, sample=100)
    ring.push(a)
    ring.push(b)
    # drain returns every pushed byte at once, in order, with NO silence padding.
    assert ring.drain() == a + b
    assert ring.fill_bytes == 0
    assert ring.pulled_bytes == len(a) + len(b)
    # A drained ring is empty again.
    assert ring.drain() == b""


def test_ring_drain_and_pull_coexist() -> None:
    ring = AudioRing()
    ring.push(_pcm(4))
    # pull still works (kept intact); after a full pull the ring is empty.
    _, got = ring.pull(4 * FRAME_BYTES)
    assert got == 4 * FRAME_BYTES
    assert ring.drain() == b""


def test_accumulator_flushes_on_pause() -> None:
    """A firmware pause (clock advances >= PAUSE_INSTRUCTIONS with no new PCM)
    enqueues the accumulated sound as one complete buffer."""
    ring = AudioRing()
    clock = {"t": 0}
    out = AudioOutput(ring, clock=lambda: clock["t"])
    buffer = bytearray()
    last = 0

    # Step 1: PCM arrives at clock 1000 -> buffered, not yet enqueued.
    sound = _pcm(8)
    ring.push(sound)
    clock["t"] = 1000
    last = out._accumulate_step(buffer, last)
    assert bytes(buffer) == sound
    assert out._queue.empty()
    assert last == 1000

    # Step 2: no new PCM, clock advances but < PAUSE -> still buffering.
    clock["t"] = 1000 + out.PAUSE_INSTRUCTIONS - 1
    last = out._accumulate_step(buffer, last)
    assert out._queue.empty()
    assert bytes(buffer) == sound

    # Step 3: clock crosses the pause threshold -> the whole sound is enqueued
    # and the buffer is cleared for the next sound.
    clock["t"] = 1000 + out.PAUSE_INSTRUCTIONS
    last = out._accumulate_step(buffer, last)
    assert bytes(buffer) == b""
    assert out._queue.get_nowait() == sound
    assert out._queue.empty()


def test_accumulator_flushes_on_size_cap() -> None:
    """Reaching max_bytes flushes mid-sound so a long track still starts playing,
    even though the firmware has not paused."""
    ring = AudioRing()
    clock = {"t": 0}
    out = AudioOutput(ring, clock=lambda: clock["t"])
    # Small cap so the test stays cheap.
    out.max_bytes = 4 * FRAME_BYTES
    buffer = bytearray()
    last = 0

    # Push more than the cap in one go; the clock keeps advancing (no pause).
    big = _pcm(10)
    ring.push(big)
    clock["t"] = 1
    out._accumulate_step(buffer, last)
    # Flushed on size, buffer cleared — the enqueued buffer is the whole >cap blob.
    assert out._queue.get_nowait() == big
    assert bytes(buffer) == b""


def test_accumulator_idle_when_no_data() -> None:
    """No PCM and an empty buffer: nothing is enqueued regardless of the clock."""
    ring = AudioRing()
    out = AudioOutput(ring, clock=lambda: 10**9)
    buffer = bytearray()
    out._accumulate_step(buffer, 0)
    assert out._queue.empty()
    assert bytes(buffer) == b""


class _FakeStream:
    """Records the slices written; stands in for sd.RawOutputStream in tests."""

    def __init__(self) -> None:
        self.started = False
        self.stopped = False
        self.closed = False
        self.written = bytearray()

    def start(self) -> None:
        self.started = True

    def write(self, data: bytes) -> None:
        self.written += data

    def stop(self) -> None:
        self.stopped = True

    def close(self) -> None:
        self.closed = True


def test_player_writes_complete_buffer() -> None:
    """The player consumes a complete buffer off the queue and writes every byte
    in one blocking write, setting playing/level and opening/closing the stream."""
    ring = AudioRing()
    out = AudioOutput(ring)
    streams: list[_FakeStream] = []

    def factory() -> _FakeStream:
        s = _FakeStream()
        streams.append(s)
        return s

    out._stream_factory = factory
    sound = _pcm(2000)

    out._play_one(sound)
    assert len(streams) == 1
    s = streams[0]
    assert s.started and s.stopped and s.closed
    assert bytes(s.written) == sound  # every byte played, nothing dropped
    assert out.sounds_played == 1
    assert out.state == "playing"
    assert out.level > 0.5


def test_player_pop_from_queue_marks_idle_when_empty() -> None:
    """Enqueue a sound, run one player step's worth of logic: it plays then goes
    idle because the queue is now empty."""
    ring = AudioRing()
    out = AudioOutput(ring)
    out._stream_factory = _FakeStream

    sound = _pcm(50)
    out._enqueue(sound)
    assert not out._queue.empty()

    # Mimic one _play loop body without the thread: get, play, idle-check.
    pcm = out._queue.get_nowait()
    out._play_one(pcm)
    if out._queue.empty():
        out.state = "idle"
        out.level = 0.0
    assert out.state == "idle"
    assert out.sounds_played == 1


def test_player_error_is_recorded_not_raised() -> None:
    """A stream failure is swallowed and recorded in .error; the thread survives."""
    ring = AudioRing()
    out = AudioOutput(ring)

    def boom() -> _FakeStream:
        raise RuntimeError("no output device")

    out._stream_factory = boom
    out._play_one(_pcm(4))  # must not raise
    assert out.error == "no output device"
    assert out.sounds_played == 1


def test_pause_instructions_is_about_02s_of_emu_time() -> None:
    """PAUSE_INSTRUCTIONS is derived from the session cadence, ~0.2 s emulated."""
    from tt_emu.runner import SESSION_INSTRUCTIONS_PER_TICK

    assert AudioOutput.PAUSE_INSTRUCTIONS == int(0.2 * SESSION_INSTRUCTIONS_PER_TICK / 0.020)


def test_close_is_safe_without_start() -> None:
    """close() before start() never raises (no threads to join)."""
    out = AudioOutput(AudioRing())
    out.close()
    assert out.state == "off"
    assert not out.available
