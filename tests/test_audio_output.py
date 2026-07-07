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


def test_audio_callback_copies_pcm() -> None:
    ring = AudioRing()
    out = AudioOutput(ring)  # no start() -> no output device needed
    frames = 8
    nbytes = frames * FRAME_BYTES

    # Prebuffer not yet met: the callback writes silence and stays idle.
    buf = bytearray(b"\xff" * nbytes)
    out._callback(buf, frames, None, None)
    assert bytes(buf) == bytes(nbytes)
    assert out.state in ("idle", "buffering")

    # Fill past the prebuffer: the next callback copies the interleaved S16LE
    # PCM straight through and reports a nonzero level.
    pcm = struct.pack(f"<{frames * 2}h", *([20000, -20000] * frames))
    assert len(pcm) == nbytes
    ring.push(pcm * (out._prebuffer_bytes // nbytes + 2))
    buf2 = bytearray(nbytes)
    out._callback(buf2, frames, None, None)
    assert out.state == "playing"
    assert bytes(buf2) == pcm
    assert out.level > 0.5
