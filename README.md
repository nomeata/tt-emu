# tt-emu

A hardware-level emulator of the **Ravensburger tiptoi® pen** (2nd generation, "MT" /
Chomptech ZC3202N / Anyka AK1050 SoC).

tt-emu boots the pen's **real, unmodified firmware** by emulating the pen's hardware —
CPU, NAND, audio, the OID (optical) sensor, buttons, and so on — and can run the pen's
`.gme` audio-book files either headless (for scripted testing) or through an interactive
terminal UI where you can "tap" OID codes and hear the audio in real time.

![The tt-emu TUI running the taschenrechner game with the firmware-aware debugger open —
live statechart, GME interpreter registers, and OID→script-line routing with symbolic
names joined from a tttool YAML.](docs/tui-screenshot.svg)

<sup>Regenerate this screenshot with `python scripts/screenshot.py`.</sup>

## Status

Early / work in progress. Built clean-room from a distilled set of hardware-interface
documents (see [`docs/`](docs/)); the emulator itself is implemented against *only* those
documents.

## Approach

1. **`docs/`** — self-contained, implementation-oriented documentation of each independent
   hardware component. These are written to be sufficient *on their own* to implement the
   emulator, without reference to any prior reverse-engineering work.
2. **`src/tt_emu/`** — the emulator, implemented from the docs.
3. **`tests/`** — a headless test suite, including tiny purpose-built firmware/GME blobs
   cross-compiled in-repo (a header + Makefile, no external toolchain checkout required).

## Stack

- Python ≥ 3.10, cross-platform (Linux / macOS / Windows)
- [Unicorn Engine](https://www.unicorn-engine.org/) — ARMv5 (ARM926EJ-S) CPU emulation
- [Textual](https://textual.textualize.io/) — the interactive TUI
- [sounddevice](https://python-sounddevice.readthedocs.io/) — real-time audio output
- NumPy

## Layout

| path | contents |
|------|----------|
| `docs/` | hardware-interface documentation (start at [`docs/index.md`](docs/index.md)) |
| `src/tt_emu/` | the emulator implementation |
| `tests/` | test suite + in-repo test-firmware build |
| `PLAN.md` | roadmap / build plan |

## Development

```sh
# Install the tt-emu / tt-emu-tui commands. uv (https://docs.astral.sh/uv/) is the
# easiest — nothing to clone; pipx or pip work too:
uv tool install tt-emu          # or: pipx install tt-emu  /  pip install tt-emu

# ...or from a source checkout, for development:
python -m venv .venv && . .venv/bin/activate   # or: uv venv
pip install -e ".[dev]"                        # or: uv pip install -e ".[dev]"
pytest                                         # the test suite

# The firmware argument is OPTIONAL for the headless command: omit it and tt-emu
# downloads + caches the official update3202MT.upd (verified against a pinned
# SHA-256; reused offline afterwards). Pass a path to use your own .upd.
tt-emu                        # headless boot (firmware auto-downloads; or pass a .upd path)

# interactive TUI: boot the pen, tap OID codes, hear the audio live
tt-emu-tui --game path/to/game.gme               # firmware auto-downloads too
python -m tt_emu.tui --game path/to/game.gme     # equivalent

# with the game's tttool source: symbolic names in the debugger panels
tt-emu-tui --game game.gme --yaml book.yaml
```

In the TUI, tap the game's **product code** button first (the game mounts), then any
content code. Audio plays through sounddevice at 22050 Hz; since the emulator runs
slower than the real pen (~a quarter of real time), playback pauses to rebuffer —
that's an emulation-speed limit, not a bug. Without a working audio device the TUI
runs silent and keeps capturing.

### The GME debugger

When the loaded image is a **recognized firmware build** (currently the 2N "MT"
`N0038MT / 20131009` image of `Update3202MT.upd`, identified by a byte-exact
fingerprint — see [`docs/firmware-2n-mt.md`](docs/firmware-2n-mt.md)), the TUI adds
live debugger panels on top of the generic view (`d` toggles them):

- **Statechart** — the firmware's live QHsm state hierarchy (all 70 states named),
  active leaf highlighted;
- **Transitions** — a clock-stamped log of every state change (push/pop/sibling),
  annotated with the event that caused it;
- **GME interpreter** — mounted product and file, the `$`-register file with live
  values, playlist/media state, armed GME timers, pending deferred jumps;
- **OID → script** — the last tapped OID, which script line it routed to, and that
  line's conditions (with live register values), actions, and playlist, plus a trace
  of the actions the interpreter actually executed.

All of it is **hook-free**: the firmware runs unmodified, and the debugger only polls
emulator RAM (plus the documented read-only PC watchpoints for the executed-action
trace and "now playing"). Unrecognized firmware simply keeps the generic panels.

With `--yaml book.yaml` (the tttool source of the loaded `.gme`, plus its sibling
`book.codes.yaml` for the script→OID codes), the panels use **symbolic names**:
registers as `$eingabe` instead of `$2`, taps as `4716 "acht" → line 1/1`, media by
name, and the matched YAML source line — all joined to the live state by product id,
OID code, and script-line index. Without a YAML everything works with raw numbers.

tt-emu takes the pen's firmware as an input (like a ROM for a console emulator); it is
not distributed with the tool.

## License

MIT — see [LICENSE](LICENSE). (The same license as tttool.)
