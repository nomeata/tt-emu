"""End-to-end milestone test: boot → book (FLAG.bin resume) → tap → play → WAV.

Drives the full session of ``nand-image-layout.md`` §7 on the real firmware and
a real ``.gme``: provision ``B:/FLAG.bin`` (§7.3.1 resume descent), let the
unmodified firmware auto-descend into book(13), tap the product OID (the GME
mounts via the booklist probe, §7.3 tap 2), tap a content OID (the script runs,
media decodes), and assert the DAC DMA capture contains real, non-silent audio.

Skipped unless both artifacts are present. This is the slowest test in the
suite (hundreds of millions of emulated instructions — the OGG decode runs on
the emulated CPU).
"""

from __future__ import annotations

import wave
from pathlib import Path

import pytest

from tt_emu.runner import STATE_BOOK, gme_product_code, run_session

UPD_PATH = Path("/home/jojo/tiptoi/update3202MT.upd")
GME_PATH = Path("/home/jojo/tiptoi/tiptoi-taschenrechner/taschenrechner.gme")

#: A content OID inside the taschenrechner GME's script range.
CONTENT_OID = 4716

pytestmark = pytest.mark.skipif(
    not (UPD_PATH.exists() and GME_PATH.exists()),
    reason="firmware .upd / taschenrechner.gme not present",
)


def test_full_session_boot_book_tap_play_wav(tmp_path: Path) -> None:
    gme = GME_PATH.read_bytes()
    product = gme_product_code(gme)
    wav_path = tmp_path / "session.wav"

    report = run_session(
        str(UPD_PATH),
        [product, CONTENT_OID],
        wav_path=wav_path,
        b_files={GME_PATH.name: gme},
    )

    # The session ran to completion, not into a stall or the budget.
    assert report.stall is None, report.format_log()
    assert report.result.reason == "session complete", report.format_log()

    # Boot health: mount + discovery (nand-image-layout.md §7.1/§7.2).
    assert report.mount_ok
    assert report.booklist_head != 0

    # The FLAG.bin resume route: splash set the resume byte, the statechart
    # descended into book(13) autonomously (§7.3.1).
    assert report.resume_byte == 1
    leaves = [state for _, state in report.state_chain]
    assert STATE_BOOK in leaves, leaves

    # The product tap mounted the GME (§7.3 tap 2: header parse stores the
    # product id) and both taps were decoded by the firmware's own capture.
    assert report.mounted_product == product
    assert report.taps_fired == 2

    # Real audio reached the DAC: non-silent, sensible rate and duration.
    stats = report.audio_stats
    assert stats is not None, report.format_log()
    assert stats.chunks > 0
    assert stats.rate in (11025, 16000, 22050, 32000, 44100)
    assert stats.duration_s > 0.2
    assert stats.peak > 1000, stats
    assert stats.nonzero_pct > 10.0, stats

    # The WAV was written and round-trips.
    with wave.open(str(wav_path), "rb") as w:
        assert w.getnchannels() == 2
        assert w.getsampwidth() == 2
        assert w.getframerate() == stats.rate
        assert w.getnframes() * 4 == stats.total_bytes
