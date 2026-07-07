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

import wave
from pathlib import Path

import pytest

from tt_emu import Clip, Emulator, ExpectationError

UPD_PATH = Path("/home/jojo/tiptoi/update3202MT.upd")
GAME_DIR = Path("/home/jojo/tiptoi/tiptoi-taschenrechner")
GME_PATH = GAME_DIR / "taschenrechner.gme"
YAML_PATH = GAME_DIR / "taschenrechner.yaml"

#: The OID code assigned to the "acht" (eight) script (taschenrechner.codes.yaml).
ACHT_OID = 4716

pytestmark = pytest.mark.skipif(
    not (UPD_PATH.exists() and GME_PATH.exists() and YAML_PATH.exists()),
    reason="firmware .upd / taschenrechner.gme / .yaml not present",
)


def _valid_wav(path: Path) -> int:
    """Assert ``path`` is a well-formed S16LE stereo WAV; return its frame count."""
    with wave.open(str(path), "rb") as w:
        assert w.getnchannels() == 2
        assert w.getsampwidth() == 2
        assert w.getframerate() in (11025, 16000, 22050, 32000, 44100)
        return w.getnframes()


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
