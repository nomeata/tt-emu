"""Firmware-aware debugger tests: recognition, symbols, live readers, TUI panels.

Layers (fastest first):

* pure recognition / name-table checks (no artifacts needed);
* tttool YAML + GME-container symbol parsing and their join, verified against
  the real ``taschenrechner`` sources (register name→index, script codes,
  media names, line matching) — file parsing only, no emulator;
* the Textual debug panels driven through the pilot with a fabricated debug
  snapshot (headless, no emulator, no audio device);
* one live end-to-end run on the real firmware: boot → book → tap product →
  tap content, asserting the hook-free readers (statechart chain, registers,
  OID→script routing, transition log) observe it all.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from _data import firmware_path, game_dir
from _pilot import run_app, wait_for

from tt_emu.firmware import detect, model, mt
from tt_emu.firmware.symbols import (
    GmeScripts,
    derive_media_names,
    load_tttool_yaml,
    parse_min_yaml,
)
from tt_emu.loader import load_upd
from tt_emu.tui import EmulatorSession, EmuSnapshot, TtEmuApp, gme_content_oids

_GAME_DIR = game_dir()
UPD_PATH = firmware_path()
GME_PATH = _GAME_DIR / "taschenrechner.gme" if _GAME_DIR is not None else None
YAML_PATH = _GAME_DIR / "taschenrechner.yaml" if _GAME_DIR is not None else None

needs_upd = pytest.mark.skipif(UPD_PATH is None, reason="firmware .upd not available")
needs_game = pytest.mark.skipif(
    GME_PATH is None or YAML_PATH is None,
    reason="taschenrechner .gme/.yaml not available",
)


# --- §1 recognition ---------------------------------------------------------------------


def test_recognize_rejects_other_images() -> None:
    assert not mt.recognize(b"")
    assert not mt.recognize(b"\x00" * 64)
    assert not mt.recognize(b"N0038MT\x00\x00\x00" + b"20140101" + b"\x00\x00Tiptoi")
    assert detect(b"some other firmware image") is None


@needs_upd
def test_recognize_real_firmware() -> None:
    firmware = load_upd(str(UPD_PATH))
    assert mt.recognize(firmware.prog.data)
    label = detect(firmware.prog.data)
    assert label is not None and "N0038MT" in label


def test_state_name_table_is_complete() -> None:
    # docs/firmware-2n-mt.md §2.3: exactly 70 states, ids 0..69.
    assert set(mt.STATE_NAMES) == set(range(70))
    assert mt.state_name(13) == "book"
    assert mt.state_name(3) == "standby"
    assert mt.state_name(50) == "selection_board_mode"
    assert mt.state_name(200) == "state200"  # graceful for out-of-table ids
    assert "tap" in mt.event_name(0x1060)
    assert "game launch" in mt.event_name(0x1020)


# --- tttool YAML parsing ------------------------------------------------------------------


def test_min_yaml_parser_subset() -> None:
    doc = parse_min_yaml(
        "# comment\n"
        "product-id: 42\n"
        "comment: Hello world\n"
        "scripts:\n"
        "  one:   $a := 1 P(x)\n"
        "  two:\n"
        "  - $a == 1? P(y) # trailing comment\n"
        "  - P(z)\n"
        "  three: $b := 2\n"
    )
    assert doc["product-id"] == "42"
    assert doc["comment"] == "Hello world"
    scripts = doc["scripts"]
    assert isinstance(scripts, dict)
    assert scripts["one"] == "$a := 1 P(x)"
    assert scripts["two"] == ["$a == 1? P(y)", "P(z)"]  # list at the key's indent
    assert scripts["three"] == "$b := 2"


@needs_game
def test_yaml_symbols_taschenrechner() -> None:
    symbols = load_tttool_yaml(YAML_PATH)
    assert symbols.product_id == 42
    assert symbols.comment == "Ein akustischer Taschenrechner"
    # Script names + codes (from the sibling .codes.yaml).
    assert len(symbols.scripts) == 28
    assert symbols.codes["acht"] == 4716
    assert symbols.oid_label(4716) == "acht"
    assert symbols.oid_label(4733) == "sag_zahl"
    assert len(symbols.scripts["auswerten"]) == 5
    # Register names sorted ascending == the assemble-time index order.
    assert symbols.register_names[2] == "eingabe"
    assert symbols.register_names[6] == "zahl"
    assert symbols.register_name(2) == "$eingabe"
    assert symbols.register_name(99) == "$99"  # graceful degrade
    # Source-line join (line order == firmware line order).
    assert symbols.script_source(4718, 1) is not None
    assert "$operator == 1?" in symbols.script_source(4718, 1)


# --- GME container parsing + the YAML↔GME join ------------------------------------------------


@needs_game
def test_gme_scripts_and_register_index_join() -> None:
    gme = GmeScripts(GME_PATH.read_bytes())
    assert (gme.product, gme.first_oid, gme.last_oid) == (42, 4716, 4743)

    # 'acht' (OID 4716): $eingabe *= 10, $eingabe += 8, $zahl := $eingabe,
    # J(sag_zahl). Verifies the binary action encoding AND the sorted-name
    # register rule ($eingabe = 2, $zahl = 6) against a real tttool GME.
    lines = gme.script(4716)
    assert lines is not None and len(lines) == 1
    assert lines[0].conds == ()
    assert lines[0].actions == (
        (2, 0xFFF2, 1, 10),  # Mult($eingabe, 10)
        (2, 0xFFF0, 1, 8),  # Inc($eingabe, 8)
        (6, 0xFFF9, 0, 2),  # Set($zahl, $eingabe) — register operand
        (0, 0xF8FF, 1, 4733),  # Jump(sag_zahl) — operand is the OID code
    )

    # 'auswerten' line 1: [$operator == 1] $wert += $eingabe … ($operator=4, $wert=5).
    lines = gme.script(4718)
    assert lines is not None and len(lines) == 5
    assert lines[1].conds == ((0, 4, 0xFFF9, 1, 1),)
    assert lines[1].actions[0] == (5, 0xFFF0, 0, 2)

    # Media names derived by aligning YAML P(...) args with binary playlists.
    symbols = load_tttool_yaml(YAML_PATH)
    media = derive_media_names(symbols, gme)
    assert media[21] == "null"  # sag_zahl line 0: $zahl == 0? P(null)
    assert media[0] == "acht"
    assert len(media) >= 30


@needs_game
def test_gme_media_table() -> None:
    """The media table: one (file offset, byte size) per media index — the
    join key for play_media's r1/r2 arguments (issue #2)."""
    gme = GmeScripts(GME_PATH.read_bytes())
    table = gme.media_table
    assert len(table) == 38
    # Entries are ascending, in-bounds file ranges.
    size = GME_PATH.stat().st_size
    assert all(0 < off and off + n <= size for off, n in table)
    assert [off for off, _ in table] == sorted(off for off, _ in table)
    # The YAML-derived media names index into this same table.
    symbols = load_tttool_yaml(YAML_PATH)
    media = derive_media_names(symbols, gme)
    assert media and all(0 <= i < len(table) for i in media)


@needs_game
def test_render_and_line_matching() -> None:
    symbols = load_tttool_yaml(YAML_PATH)
    gme = GmeScripts(GME_PATH.read_bytes())
    symbols.media_names.update(derive_media_names(symbols, gme))

    lines = gme.script(4716)
    assert lines is not None
    rendered = [
        mt.render_action(*action, symbols, lines[0].playlist)
        for action in lines[0].actions
    ]
    assert rendered == [
        "Mult($eingabe,10)",
        "Inc($eingabe,8)",
        "Set($zahl,$eingabe)",
        "Jump(sag_zahl)",
    ]
    cond = gme.script(4718)[1].conds[0]
    assert mt.render_condition(cond, symbols, registers=(0, 0, 0, 0, 3)) == "$operator=3 == 1"

    # The resident-line matcher: the parsed (actions, playlist) identify the line.
    debugger = mt.MtDebugger.__new__(mt.MtDebugger)  # match logic needs no machine
    debugger._match_cache = {}
    sag_zahl = gme.script(4733)
    assert sag_zahl is not None
    for expect, line in enumerate(sag_zahl):
        assert (
            debugger._match_line(42, 4733, sag_zahl, line.actions, line.playlist)
            == expect
        )


# --- the TUI debug panels (pilot, headless, fabricated snapshot) --------------------------------


class _FakeDebugSession:
    """SessionControl stand-in publishing a ready debug snapshot."""

    def __init__(self) -> None:
        from tt_emu.tui import AudioRing

        self.product_code: int | None = 42
        self.content_oids = [4716]
        self.symbols: object | None = None
        self.ring = AudioRing()
        self.snapshot = EmuSnapshot(
            power="running",
            leaf=13,
            debug=model.DebugSnapshot(
                ready=True,
                chain=(3, 12, 13),
                chain_names=tuple(mt.state_name(s) for s in (3, 12, 13)),
                last_event=0x1060,
                registers=(0, 0, 8, 0, 0, 0, 8, 0, 0, 0, 0),
                register_names=tuple(
                    f"${n}" for n in (
                        "dreistellig", "einer_return", "eingabe", "neuer_operator",
                        "operator", "wert", "zahl", "zehner", "ziffer",
                        "zweistellig", "zweistellig_return",
                    )
                ),
                product=42,
                product_label="Ein akustischer Taschenrechner",
                gme_handle=3,
                gme_mounted=True,
                gme_path="b:\\taschenrechner.gme",
                book_count=1,
                oid_first=4716,
                oid_last=4743,
                last_oid=4716,
                routing=model.OidRouting(
                    oid=4716,
                    label="acht",
                    kind="content",
                    line_count=1,
                    matched_line=0,
                    actions=("Mult($eingabe,10)", "Inc($eingabe,8)"),
                    source="$eingabe *= 10 $eingabe += 8",
                ),
                tick=1234,
                heartbeat=99,
            ),
        )
        self._transitions = [
            "[   1,000,000] push    standby → book_mount  [depth 0→1]  on 0x1058 open product",
        ]

    def start(self) -> None: ...
    def shutdown(self, timeout: float = 5.0) -> None: ...
    def tap(self, oid: int) -> None: ...
    def press_button(self, name: str, *, hold: bool = False) -> None: ...
    def post_event(self, message: str) -> None: ...

    def drain_events(self) -> list[str]:
        return []

    def drain_transitions(self) -> list[str]:
        out, self._transitions = self._transitions, []
        return out


def test_app_debug_panels_populate_and_toggle() -> None:
    async def scenario() -> None:
        fake = _FakeDebugSession()
        app = TtEmuApp(fake, audio=None)
        async with run_app(app, size=(120, 50)) as pilot:
            # Recognized firmware: the debug row is revealed and populated by
            # the app's periodic refresh — wait for each panel's content to
            # land instead of racing it with a single pause.
            await wait_for(pilot, app, "#debug-panels", ready=lambda w: w.display is True)
            statechart_body = await wait_for(
                pilot, app, "#statechart-body",
                ready=lambda w: "book (13)" in str(w.content),
            )
            statechart = str(statechart_body.content)
            assert "book (13)" in statechart and "standby (3)" in statechart
            assert "active" in statechart  # the leaf is highlighted
            gme_body = await wait_for(
                pilot, app, "#gme-body",
                ready=lambda w: "42 — Ein akustischer Taschenrechner" in str(w.content),
            )
            gme = str(gme_body.content)
            assert "42 — Ein akustischer Taschenrechner" in gme
            assert "$eingabe=8" in gme and "$zahl=8" in gme
            oid_body = await wait_for(
                pilot, app, "#oid-body", ready=lambda w: "acht" in str(w.content)
            )
            oid = str(oid_body.content)
            assert "4716" in oid and "acht" in oid
            assert "line 1/1" in oid
            assert "Inc($eingabe,8)" in oid
            # The transition log received the drained lines.
            transitions = await wait_for(
                pilot, app, "#transitions",
                ready=lambda w: w.display is True
                and any("book_mount" in str(line) for line in w.lines),
            )
            assert transitions.display is True
            assert any("book_mount" in str(line) for line in transitions.lines)
            # 'd' toggles the debug row off (and back on).
            await pilot.press("d")
            await wait_for(pilot, app, "#debug-panels", ready=lambda w: w.display is False)
            await pilot.press("d")
            await wait_for(pilot, app, "#debug-panels", ready=lambda w: w.display is True)

    asyncio.run(scenario())


@needs_game
def test_app_tap_list_named_from_yaml() -> None:
    """A joined tttool YAML gives every tap row its symbolic name."""

    async def scenario() -> None:
        from test_tui import _FakeSession

        fake = _FakeSession()
        fake.symbols = load_tttool_yaml(YAML_PATH)
        fake.content_oids = gme_content_oids(GME_PATH.read_bytes(), limit=None)
        app = TtEmuApp(fake, audio=None)
        async with run_app(app, size=(120, 50)) as pilot:
            tap_list = await wait_for(
                pilot, app, "#tap-list", ready=lambda w: w.option_count > 3
            )
            prompts = [
                str(tap_list.get_option_at_index(i).prompt)
                for i in range(tap_list.option_count)
            ]
            # Product is pinned first; content OIDs carry their YAML names.
            assert "Product" in prompts[0]
            joined = "\n".join(prompts)
            assert "acht" in joined and "sag_zahl" in joined
            # By-OID order (the default): 4716 'acht' precedes 4733 'sag_zahl'.
            assert joined.index("acht") < joined.index("sag_zahl")
            # Selecting a named row taps that OID.
            tap_list.focus()
            await pilot.pause()
            tap_list.highlighted = tap_list.get_option_index("tap-4716")
            await pilot.press("enter")
            assert fake.taps == [4716]

    asyncio.run(scenario())


def test_app_without_debug_stays_generic() -> None:
    """Unrecognized firmware (debug=None): the debug row never shows."""

    async def scenario() -> None:
        from test_tui import _FakeSession

        fake = _FakeSession()
        app = TtEmuApp(fake, audio=None)
        async with run_app(app, size=(100, 40)) as pilot:
            # Wait until the layout is mounted (a single pause can race the
            # mount); the debug row must then be hidden and stay hidden.
            debug_panels = await wait_for(pilot, app, "#debug-panels")
            transitions = await wait_for(pilot, app, "#transitions")
            assert debug_panels.display is False
            assert transitions.display is False
            await pilot.press("d")  # toggling without a debugger is a no-op + log line
            assert app.query_one("#debug-panels").display is False

    asyncio.run(scenario())


# --- live end-to-end: the hook-free readers on the real firmware -------------------------------


@needs_upd
@needs_game
def test_live_debugger_boot_book_tap() -> None:
    """Boot the real image, mount the game, tap 'acht'; the readers see it all.

    Everything asserted here is read hook-free from emulator RAM (plus the
    documented read-only PC watchpoints for the executed-action trace).
    """
    # Deterministic pacing: tests want reproducible runs (realtime is the
    # interactive default).
    session = EmulatorSession(
        UPD_PATH, [GME_PATH], yaml_path=YAML_PATH, pacing="deterministic"
    )
    assert session.symbols is not None and session.symbols.product_id == 42
    session.start()

    transitions: list[str] = []
    seen_actions: set[str] = set()

    def wait_for(what: str, cond, timeout: float) -> EmuSnapshot:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            snap = session.snapshot
            transitions.extend(session.drain_transitions())
            debug = snap.debug
            if debug is not None:
                seen_actions.update(debug.recent_actions)
            if cond(snap):
                return snap
            assert session.running, f"emulator died waiting for {what}: {snap.power}"
            time.sleep(0.02)
        pytest.fail(f"timeout waiting for {what}; last power={session.snapshot.power}")

    try:
        # Firmware recognized; the GPIO11 power-on descent reaches book(13).
        snap = wait_for(
            "book mode",
            lambda s: s.debug is not None and s.debug.ready and 13 in s.debug.chain,
            timeout=240.0,
        )
        assert snap.firmware_label and "N0038MT" in snap.firmware_label
        assert snap.debug is not None
        assert snap.debug.chain_names[-1] == "book"
        assert snap.debug.chain[0] == 3  # bottom of the stack: standby

        # Book(13) is entered before the power-on welcome voice finishes; a tap during
        # that book-entry jingle is discarded by the firmware (§7.3.2). The TUI does not
        # gate taps (an early tap is honestly ignored, as on hardware), so wait for the
        # welcome to drain — the pen has finished talking — before tapping, the way a
        # person would.
        wait_for("power-on welcome drained", lambda s: s.audio_settled, timeout=60.0)

        # Tap the product OID: the GME mounts, the register file loads.
        session.tap(42)
        snap = wait_for(
            "product mount + registers",
            lambda s: s.debug is not None
            and s.debug.product == 42
            and len(s.debug.registers) == 11,
            timeout=180.0,
        )
        debug = snap.debug
        assert debug is not None
        assert debug.gme_mounted and debug.gme_handle != -1
        assert debug.product_label == "Ein akustischer Taschenrechner"
        assert (debug.oid_first, debug.oid_last) == (4716, 4743)
        assert debug.book_count == 1
        assert "GME" in debug.gme_path.upper()
        assert debug.register_names[2] == "$eingabe"

        # Wait for the mount's welcome to finish before the content tap (same reason:
        # the gate-less TUI would otherwise inject it into the just-mounted game's
        # welcome, where the firmware discards it).
        wait_for("mount welcome drained", lambda s: s.audio_settled, timeout=60.0)

        # Tap 'acht' (4716): $eingabe *= 10; $eingabe += 8 -> the register file
        # shows 8, and the OID routing names the tapped script.
        session.tap(4716)
        snap = wait_for(
            "content tap effect ($eingabe == 8)",
            lambda s: s.debug is not None
            and len(s.debug.registers) == 11
            and s.debug.registers[2] == 8,
            timeout=180.0,
        )
        debug = snap.debug
        assert debug is not None
        # last_oid is the classifier's last decoded tap; the 'acht' script
        # Jumps to sag_zahl (4733), so by snapshot time it may already read the
        # jump target — accept either (the routing below pins the tap itself).
        assert debug.last_oid in (4716, 4733)
        # The routing panel is populated with a valid content routing. (With
        # coarse session chunks the interpreter races the whole jump chain
        # 4716→sag_zahl→dreistellig within a single chunk, so the snapshot shows
        # wherever it came to rest; the tap itself is pinned by the exec trace
        # below, and the OID→line matcher has its own unit test.)
        assert debug.routing is not None
        assert debug.routing.kind == "content" and debug.routing.label

        # Accumulate the PC-watch action trace until acht's action appears.
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline and "Inc($eingabe,8)" not in seen_actions:
            snap = session.snapshot
            transitions.extend(session.drain_transitions())
            if snap.debug is not None:
                seen_actions.update(snap.debug.recent_actions)
            time.sleep(0.02)
        # The debugger decoded the tapped 'acht' script symbolically.
        assert "Inc($eingabe,8)" in seen_actions, seen_actions

        # The transition log recorded the descent into book mode.
        transitions.extend(session.drain_transitions())
        assert any("book" in line for line in transitions), transitions
    finally:
        session.shutdown(timeout=30.0)
    assert not session.running
