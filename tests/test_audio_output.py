"""The sounddevice output callback: raw byte buffers + a stdlib peak meter."""
from __future__ import annotations

import struct

from tt_emu.audio_capture import FRAME_BYTES
from tt_emu.tui import AudioOutput, AudioRing, _peak_level


def test_peak_level() -> None:
    assert _peak_level(b"") == 0.0
    assert _peak_level(b"\x00" * 32) == 0.0
    # A full-scale negative sample clamps to 1.0.
    assert _peak_level(struct.pack("<4h", 100, -32768, 50, 0)) == 1.0
    # A mid-level peak resolves proportionally.
    assert abs(_peak_level(struct.pack("<4h", 8192, 0, -4096, 0)) - 8192 / 32768) < 1e-6


class _CallbackStop(Exception):
    """Stand-in for ``sounddevice.CallbackStop`` (no sounddevice in CI)."""


def test_audio_callback_plays_then_stops_on_drain() -> None:
    ring = AudioRing()
    out = AudioOutput(ring)  # no start() -> no output device needed
    frames = 8
    nbytes = frames * FRAME_BYTES

    # A full block of real PCM: the callback copies the interleaved S16LE bytes
    # straight through, reports a nonzero level, and does NOT raise.
    pcm = struct.pack(f"<{frames * 2}h", *([20000, -20000] * frames))
    assert len(pcm) == nbytes
    ring.push(pcm)
    buf = bytearray(nbytes)
    out._callback(buf, frames, None, None)
    assert out.state == "playing"
    assert bytes(buf) == pcm
    assert out.level > 0.5

    # Ring drained: the callback fills the missing tail with silence and raises
    # the stop sentinel so PortAudio stops the stream cleanly (no underrun).
    out._callback_stop = _CallbackStop
    ring.push(pcm[: nbytes // 2])  # only half a block available
    buf2 = bytearray(b"\xff" * nbytes)
    try:
        out._callback(buf2, frames, None, None)
    except _CallbackStop:
        pass
    else:  # pragma: no cover — assertion below reports the failure
        raise AssertionError("drained callback did not raise the stop sentinel")
    assert bytes(buf2) == pcm[: nbytes // 2] + bytes(nbytes // 2)  # real + silence
    assert out.state == "idle"
    assert out.level == 0.0
    assert out.underrun_blocks == 1


class _FakeStream:
    """Minimal RawOutputStream stand-in: records start() calls, exposes active."""

    def __init__(self) -> None:
        self.active = False
        self.starts = 0

    def start(self) -> None:
        self.starts += 1
        self.active = True


def test_monitor_starts_stream_only_when_prebuffered() -> None:
    ring = AudioRing()
    out = AudioOutput(ring)
    stream = _FakeStream()
    out._stream = stream

    # Below the prebuffer: the monitor's start decision is a no-op.
    assert ring.fill_bytes < out._prebuffer_bytes
    out._run_monitor_step()
    assert stream.starts == 0
    assert not stream.active

    # Fill past the prebuffer: the monitor starts the (stopped) stream once.
    ring.push(b"\x00" * (out._prebuffer_bytes + FRAME_BYTES))
    out._run_monitor_step()
    assert stream.starts == 1
    assert stream.active
    assert out.state == "playing"

    # Already active: it must not start it again.
    out._run_monitor_step()
    assert stream.starts == 1
