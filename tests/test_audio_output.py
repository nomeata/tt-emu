"""Live audio output: ring pull/silence-padding + the continuous writer block.

None of these tests need sounddevice / a real output device: the writer's
per-block logic is exercised through the split-out :meth:`AudioOutput._write_block`
with a stub stream that records what it is handed.
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


def test_ring_pull_silence_pads() -> None:
    ring = AudioRing()
    # Empty ring: the pull is pure silence, got == 0.
    data, got = ring.pull(4 * FRAME_BYTES)
    assert got == 0
    assert data == b"\x00" * (4 * FRAME_BYTES)

    a = _pcm(2)
    ring.push(a)
    # A short ring: the real bytes, silence-padded up to the requested size.
    data, got = ring.pull(4 * FRAME_BYTES)
    assert got == len(a)
    assert data[: len(a)] == a
    assert data[len(a) :] == b"\x00" * (4 * FRAME_BYTES - len(a))


def test_ring_pull_spans_pushes_in_order() -> None:
    ring = AudioRing()
    a, b = _pcm(2, 100), _pcm(2, 200)
    ring.push(a)
    ring.push(b)
    data, got = ring.pull(4 * FRAME_BYTES)
    assert got == len(a) + len(b)
    assert data == a + b  # consumed in push order, no gaps


class _FakeStream:
    """Records the blocks written; stands in for sd.RawOutputStream in tests."""

    def __init__(self) -> None:
        self.written = bytearray()

    def start(self) -> None: ...
    def stop(self) -> None: ...
    def close(self) -> None: ...

    def write(self, data: bytes) -> None:
        self.written += data


def test_write_block_plays_ring_data() -> None:
    """A block of real PCM is written whole; state=playing, meter + counter set."""
    ring = AudioRing()
    out = AudioOutput(ring)
    sound = _pcm(out.BLOCK_FRAMES)  # exactly one block
    ring.push(sound)
    stream = _FakeStream()

    out._write_block(stream)
    assert bytes(stream.written) == sound  # every byte played, nothing dropped
    assert out.state == "playing"
    assert out.level > 0.5
    assert out.played_bytes == len(sound)


def test_write_block_writes_silence_when_ring_empty() -> None:
    """An empty ring still yields a full block — of silence; idle, nothing played."""
    ring = AudioRing()
    out = AudioOutput(ring)
    stream = _FakeStream()

    out._write_block(stream)
    assert bytes(stream.written) == b"\x00" * out._block_bytes  # full block, all silence
    assert out.state == "idle"
    assert out.level == 0.0
    assert out.played_bytes == 0


def test_write_block_partial_is_padded_to_a_full_block() -> None:
    """Less than a block buffered: the real bytes then silence, always a full
    block (so the device never underruns); only real bytes count as played."""
    ring = AudioRing()
    out = AudioOutput(ring)
    half = _pcm(out.BLOCK_FRAMES // 2)
    ring.push(half)
    stream = _FakeStream()

    out._write_block(stream)
    assert len(stream.written) == out._block_bytes
    assert bytes(stream.written[: len(half)]) == half
    assert bytes(stream.written[len(half) :]) == b"\x00" * (out._block_bytes - len(half))
    assert out.state == "playing"
    assert out.played_bytes == len(half)


def test_backlog_seconds_tracks_ring_fill() -> None:
    """The panel's buffered/latency figure is just the ring's fill in seconds."""
    ring = AudioRing()
    out = AudioOutput(ring)
    assert out.backlog_seconds == 0.0
    ring.push(_pcm(out.rate))  # one second of audio
    assert abs(out.backlog_seconds - 1.0) < 1e-6


def test_run_records_stream_open_error() -> None:
    """A failure opening the device is recorded and the thread returns — no raise."""
    out = AudioOutput(AudioRing())
    out.available = True

    def boom() -> _FakeStream:
        raise RuntimeError("no output device")

    out._stream_factory = boom
    out._run()  # must not raise
    assert out.error == "no output device"
    assert not out.available
    assert out.state == "off"


def test_close_is_safe_without_start() -> None:
    """close() before start() never raises (no writer thread to join)."""
    out = AudioOutput(AudioRing())
    out.close()
    assert out.state == "off"
    assert not out.available
