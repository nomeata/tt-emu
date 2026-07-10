"""End-to-end test of the synchronous scripting API (:mod:`tt_emu.emulator`).

Drives the real firmware + the taschenrechner ``.gme`` through the documented
scripting surface: boot into book mode, mount the game, tap a digit, and assert
on the media the firmware plays and on the live ``$``-register file. Exercises
*both* styles — the ``expect_play`` / ``expect`` assertions and the
``wait_for_audio() -> Clip`` primitive.

Skipped unless the firmware ``.upd`` and the ``.gme`` (+ its YAML) are present.
This is a slow test: a full boot → mount → tap → play chain on the emulated CPU.
"""

from __future__ import annotations

import os
import wave
from pathlib import Path

import pytest
from _data import firmware_path, game_dir

from tt_emu import Clip, Emulator, ExpectationError

UPD_PATH = firmware_path()
GAME_DIR = game_dir()
GME_PATH = GAME_DIR / "taschenrechner.gme" if GAME_DIR is not None else None
YAML_PATH = GAME_DIR / "taschenrechner.yaml" if GAME_DIR is not None else None

#: The OID code assigned to the "acht" (eight) script (taschenrechner.codes.yaml).
ACHT_OID = 4716

pytestmark = pytest.mark.skipif(
    UPD_PATH is None or GME_PATH is None or YAML_PATH is None,
    reason="firmware .upd / taschenrechner game not available",
)


def _valid_wav(path: Path) -> int:
    """Assert ``path`` is a well-formed S16LE stereo WAV; return its frame count."""
    with wave.open(str(path), "rb") as w:
        assert w.getnchannels() == 2
        assert w.getsampwidth() == 2
        assert w.getframerate() in (11025, 16000, 22050, 32000, 44100)
        return w.getnframes()


@pytest.mark.xfail(
    reason="The idle-restart audio bug is fixed (the 'acht' digit now decodes and plays — "
    "clip.pcm > 0). This end-to-end test now gets further and fails on a separate multi-tap "
    "sequencing issue: after the second digit tap, expect_play('neun') catches the prior "
    "'acht' clip (the new playback's media detection races with the previous one's tail). "
    "See docs/audio-dac-dma.md §8.",
    strict=False,
)
def test_scripting_end_to_end(tmp_path: Path) -> None:
    session_wav = tmp_path / "session.wav"
    clip_wav = tmp_path / "acht.wav"

    with Emulator(firmware=str(UPD_PATH), gme=GME_PATH, yaml=YAML_PATH) as pen:
        # Booted into book mode via the authentic power-on descent; nothing
        # mounted yet.
        assert pen.state == "book"
        assert pen.mounted is None

        # A symbolic OID name resolves to the same code as the int (YAML join).
        assert pen._resolve_oid("acht") == ACHT_OID
        assert pen._resolve_oid(ACHT_OID) == ACHT_OID

        # Mount the game with the product tap.
        pen.tap("product")
        assert pen.mounted == 42  # taschenrechner product-id

        # Tap the digit "8" by its int OID code; the firmware plays it.
        pen.tap(ACHT_OID)

        # The wait_for_audio() -> Clip primitive.
        clip = pen.wait_for_audio(timeout="20s")
        assert isinstance(clip, Clip)
        assert clip.media == "acht"
        assert clip.index is not None
        assert pen.now_playing == "acht"

        # Live register state and statechart leaf (property reads only).
        assert pen.registers["eingabe"] == 8
        assert pen.registers["$eingabe"] == 8  # leading $ tolerated
        assert pen.registers.get("no_such_reg") is None
        assert pen.state == "book"

        # Let the digit finish decoding, then the clip has real PCM.
        pen.wait("500ms")
        assert len(clip.pcm) > 0
        clip.save_wav(clip_wav)
        assert _valid_wav(clip_wav) > 0

        # The expect_play / expect assertion style: tap "9" -> the number is now
        # 89, spoken "neunundachtzig", whose first media is "neun".
        pen.tap("neun")
        neun_clip = pen.expect_play("neun", timeout="20s")
        assert neun_clip.kind == "media"
        pen.expect(pen.registers["eingabe"] == 89, "eingabe should be 89 after 8 then 9")

        # A wrong expectation raises ExpectationError (with context).
        with pytest.raises(ExpectationError):
            pen.expect(pen.registers["eingabe"] == 0, "deliberately wrong")

        # save_wav writes everything captured so far.
        stats = pen.save_wav(session_wav)
        assert stats.total_bytes > 0

    # The session WAV is valid and non-empty.
    assert _valid_wav(session_wav) > 0
    assert session_wav.stat().st_size > 44  # more than just the WAV header


#: OID codes for a run of digit scripts (taschenrechner.codes.yaml), used to
#: probe the "repeated media playback" pattern.
# --- The "media playback from an idle chain is silent" bug (see docs §8) ----------------
#
# Isolated by experiment: the failing variable is *idle time*, not tap position or media.
# A content tap within ~100M instructions of the mount plays real audio; a tap after
# ~100M (~2s) of idle is silent (restarting the audio chain from idle is broken). It is
# pacing-independent. A second, more complex GME (WWW Bauernhof) crashes on mount instead.


def _real_chunks(pen, since: int) -> int:
    """Non-silent captured DAC chunks since index ``since``."""
    return sum(1 for ch in pen._capture.chunks[since:] if len(set(ch.data[:64])) > 4)


def _raw_tap(pen, code: int, *, latch: int = 25_000_000, play: int = 160_000_000) -> int:
    """Inject a raw OID tap (bypassing the gated :meth:`Emulator.tap`, so it survives a
    silent playback's stall) and return the non-silent DAC chunks it produced."""
    before = len(pen._capture.chunks)
    pen._booted.oid.hold(code)
    pen._advance(latch)
    pen._booted.oid.lift()
    pen._advance(play)
    return _real_chunks(pen, before)


def _second_gme() -> Path | None:
    """A second, more complex GME to cross-check the bug, or ``None``. Resolved from
    ``$TT_EMU_GME2`` or a local ``WWW Bauernhof.gme`` (neither shipped)."""
    env = os.environ.get("TT_EMU_GME2")
    if env and Path(env).exists():
        return Path(env)
    local = Path("/home/jojo/tiptoi/gmes/WWW Bauernhof.gme")
    return local if local.exists() else None


def test_tap_immediately_after_mount_plays(tmp_path: Path) -> None:
    """A content tap taken *immediately* after the mount plays real audio.

    The working half of the idle-restart bug: while the audio chain is still active
    (no long idle since the mount/welcome), a fresh raw tap decodes and plays.
    """
    with Emulator(firmware=str(UPD_PATH), gme=GME_PATH, yaml=YAML_PATH) as pen:
        pen.tap("product")
        assert _raw_tap(pen, ACHT_OID) > 3


def test_tap_after_idle_produces_audio(tmp_path: Path) -> None:
    """After ~2s idle since the mount, a content tap still plays real audio.

    Regression guard for the idle-restart bug (docs/audio-dac-dma.md §8): the fix was to
    stop pinning the work/data window resident and let the firmware's own page table +
    demand-pager own it, so the audio player object (which the firmware maps into the
    demand-paged pool) is no longer corrupted by a pinned-vs-paged conflict on eviction.
    Before the fix a tap after the audio chain went idle produced only silence.
    """
    with Emulator(firmware=str(UPD_PATH), gme=GME_PATH, yaml=YAML_PATH) as pen:
        pen.tap("product")
        pen._advance(120_000_000)  # ~2s idle -> the audio chain goes idle
        assert _raw_tap(pen, ACHT_OID) > 3, "tap after idle produced no audio"


@pytest.mark.skipif(_second_gme() is None, reason="second (Bauernhof) .gme not available")
@pytest.mark.xfail(
    reason="A second, more complex GME (WWW Bauernhof, product-id 1) crashes on the "
    "product-mount/tap path — the firmware jumps to a UTF-16 string mistaken for a "
    "pointer (media-path corruption). See docs/audio-dac-dma.md §8.",
    strict=False,
)
def test_second_gme_mounts_and_plays(tmp_path: Path) -> None:
    """Mounting a second GME and tapping a content OID should play audio (it crashes)."""
    with Emulator(firmware=str(UPD_PATH), gme=_second_gme(), dac_pacing="fast") as pen:
        pen.tap(1)  # product-id 1 = the mount OID
        assert pen.mounted == 1
        assert _raw_tap(pen, 1401) > 3
