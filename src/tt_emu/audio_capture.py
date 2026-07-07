"""Captured-audio store and WAV writer (``audio-dac-dma.md`` §1/§7).

The DMA model appends every DAC-bound chunk here. The bytes are already final:
signed 16-bit little-endian PCM, always 2 interleaved channels, volume already
applied by the firmware's mixer (§1 "Sample format", §5). Chunks are tagged
with the sample rate active at submit time; per §1, a whole playback session
runs at one rate, so the WAV is written at the **last non-8000 Hz rate seen**
(8000 Hz is only the transient DAC bring-up default).
"""

from __future__ import annotations

import struct
import wave
from dataclasses import dataclass
from pathlib import Path

__all__ = ["AudioCapture", "AudioStats", "CapturedChunk"]

#: S16LE stereo (§1): channels x bytes-per-sample.
CHANNELS = 2
SAMPLE_BYTES = 2
FRAME_BYTES = CHANNELS * SAMPLE_BYTES

#: The transient DAC bring-up rate, ignored when tagging the WAV (§1).
BRINGUP_RATE = 8000
#: GME audio content rate (§1) — the fallback if no rate was ever decoded.
DEFAULT_RATE = 22050


@dataclass(frozen=True)
class CapturedChunk:
    """One DAC DMA submit's worth of samples."""

    clock: int  #: machine clock (emulated instructions) at the submit
    rate: int  #: sample rate active at submit time (Hz)
    data: bytes  #: post-volume S16LE stereo bytes
    audible: bool  #: amp enabled (GPIO16=1) and mute released (GPIO13=0), §1


@dataclass(frozen=True)
class AudioStats:
    """Summary of a capture (the milestone report numbers)."""

    chunks: int
    total_bytes: int
    rate: int
    duration_s: float
    peak: int  #: max |sample| (0..32768)
    nonzero_pct: float  #: % of samples with a nonzero value
    audible_pct: float  #: % of chunks captured with the amp audible


class AudioCapture:
    """Accumulates DAC-bound PCM chunks and writes them out as a WAV file."""

    def __init__(self) -> None:
        self.chunks: list[CapturedChunk] = []

    def append(self, clock: int, rate: int, data: bytes, *, audible: bool = True) -> None:
        self.chunks.append(CapturedChunk(clock, rate, bytes(data), audible))

    @property
    def total_bytes(self) -> int:
        return sum(len(c.data) for c in self.chunks)

    @property
    def wav_rate(self) -> int:
        """The session rate: last non-bring-up rate seen (§1), default 22050."""
        for chunk in reversed(self.chunks):
            if chunk.rate != BRINGUP_RATE:
                return chunk.rate
        return self.chunks[-1].rate if self.chunks else DEFAULT_RATE

    def pcm(self) -> bytes:
        """All captured bytes, concatenated in submit order."""
        return b"".join(c.data for c in self.chunks)

    def stats(self) -> AudioStats:
        pcm = self.pcm()
        n = len(pcm) // SAMPLE_BYTES
        peak = 0
        nonzero = 0
        if n:
            samples = struct.unpack(f"<{n}h", pcm[: n * SAMPLE_BYTES])
            for s in samples:
                if s:
                    nonzero += 1
                    a = -s if s < 0 else s
                    if a > peak:
                        peak = a
        rate = self.wav_rate
        audible = sum(1 for c in self.chunks if c.audible)
        return AudioStats(
            chunks=len(self.chunks),
            total_bytes=len(pcm),
            rate=rate,
            duration_s=(len(pcm) / FRAME_BYTES) / rate if rate else 0.0,
            peak=peak,
            nonzero_pct=100.0 * nonzero / n if n else 0.0,
            audible_pct=100.0 * audible / len(self.chunks) if self.chunks else 0.0,
        )

    def write_wav(self, path: str | Path) -> AudioStats:
        """Write the capture as an S16LE stereo WAV at :attr:`wav_rate` (§1)."""
        stats = self.stats()
        with wave.open(str(path), "wb") as w:
            w.setnchannels(CHANNELS)
            w.setsampwidth(SAMPLE_BYTES)
            w.setframerate(stats.rate)
            w.writeframes(self.pcm())
        return stats
