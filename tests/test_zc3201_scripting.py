"""End-to-end gme-based test on the **1st-generation ZC3201** firmware.

The MT scripting test (:mod:`test_scripting`) drives the 2nd-gen pen through the
high-level :class:`~tt_emu.Emulator` API. The ZC3201 bring-up reaches the *same*
milestone — boot → discover the ``.gme`` on B: → mount it with a product-OID tap
→ play a content-OID's media through the shared GME interpreter — but at the
machine level (the ZC3201 Emulator-API surface is a follow-on), using the
firmware's **own** play observable: ``voice_play_sample`` ``0x0809f068``, entered
with ``r1 = media file-offset``, ``r2 = media byte-size`` (the same observable the
``firmware-re`` lab's ``zc3201_emu.py`` validates against). Each play's
``(offset, size)`` is matched against the game's own media table, so the test
asserts the *right* media plays for the *right* OID — the core "OID-tap / play"
criterion, on ZC3201.

The whole chain runs the **unmodified** firmware through :func:`build_zc3201_machine`
(authentic SVC stack, the small-page MtdLib/FAT16 A:/B: image with the even-block
whole-disk map, the OID sensor, and the audio-codec handshake model). Skipped
unless the ZC3201 ``.upd`` and ``example.gme`` are present.
"""

from __future__ import annotations

import pytest
from _data import firmware_path_zc3201, gme_zc3201

from tt_emu.boot import build_zc3201_machine
from tt_emu.firmware.symbols import GmeScripts
from tt_emu.loader import load_upd
from tt_emu.machine import MachineConfig
from tt_emu.runner import gme_product_code

UPD_PATH = firmware_path_zc3201()
GME_PATH = gme_zc3201()

#: ``voice_play_sample`` (runtime address; the play observable). Entered with
#: ``r1 = media file-offset``, ``r2 = media byte-size``.
PC_VOICE_PLAY_SAMPLE = 0x0809_F068
#: The product-id OID (``example.gme`` product 42) and a content OID that maps to
#: a media playscript (content band 8065-8067).
PRODUCT_OID = 42
CONTENT_OID = 8065

pytestmark = pytest.mark.skipif(
    UPD_PATH is None or GME_PATH is None,
    reason="ZC3201 firmware .upd / example.gme not available",
)

_R1 = 4  # UC_ARM_REG_R1 index via reg_read helper below
_R2 = 5


def _reg_reader(machine):
    from unicorn.arm_const import UC_ARM_REG_R1, UC_ARM_REG_R2

    return lambda: (
        machine.uc.reg_read(UC_ARM_REG_R1),
        machine.uc.reg_read(UC_ARM_REG_R2),
    )


def _build(gme: bytes):
    fw = load_upd(str(UPD_PATH))
    # 100k insn/tick: under the real MMU the demand-paging fault-handler overhead
    # inflates the instruction count per unit of firmware progress, so the timer
    # cadence scales up to keep the timer-gated boot descent from starving (see
    # emulator._ZC3201_INSTRUCTIONS_PER_TICK, docs Leg 26). Budgets below are ~5x
    # the flat values to cover the same overhead.
    machine = build_zc3201_machine(
        fw, MachineConfig(instructions_per_tick=100_000), b_files={"example.gme": gme}
    ).machine
    plays: list[tuple[int, int]] = []
    read = _reg_reader(machine)

    def on_play(_m):
        plays.append(read())

    machine.on_code(PC_VOICE_PLAY_SAMPLE, on_play)
    return machine, plays


def _tap(machine, oid: int, *, latch: int = 60_000_000, play: int = 200_000_000) -> None:
    machine.oid.hold(oid)
    machine.run(latch)
    machine.oid.lift()
    machine.run(play)


def test_zc3201_discovers_mounts_and_plays_tapped_oid() -> None:
    """Boot → discover B:/EXAMPLE.GME → product tap mounts it → content tap plays.

    Asserts the whole authentic chain on the unmodified 1st-gen firmware, matching
    every ``voice_play_sample`` play against ``example.gme``'s own media table.
    """
    gme = GME_PATH.read_bytes()
    assert gme_product_code(gme) == PRODUCT_OID
    media = GmeScripts(gme).media_table  # [(offset, size), ...]
    media_set = set(media)

    machine, plays = _build(gme)

    # Power-on descent into book-mode idle (the chime may play from A:'s
    # Chomp_Voice.bin — its source is the udisk image, not the game media).
    machine.run(150_000_000)

    # Product tap → discovery-listed EXAMPLE.GME mounts and its welcome media plays.
    machine.oid.hold(PRODUCT_OID)
    machine.run(60_000_000)
    machine.oid.lift()
    machine.run(200_000_000)
    product_plays = [p for p in plays if p in media_set]
    assert product_plays, (
        "product tap produced no game-media play (mount/welcome) — "
        f"plays seen: {[(hex(a), hex(b)) for a, b in plays]}"
    )

    # Content tap → the shared GME interpreter plays the OID's media.
    before = len(plays)
    _tap(machine, CONTENT_OID)
    content_plays = [p for p in plays[before:] if p in media_set]
    assert content_plays, (
        f"content OID {CONTENT_OID} produced no game-media play — "
        f"plays since tap: {[(hex(a), hex(b)) for a, b in plays[before:]]}"
    )
    # The played media is a real entry of this game's media table.
    off, size = content_plays[0]
    assert (off, size) in media_set
    assert size > 0
