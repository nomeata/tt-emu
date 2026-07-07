# tt-emu

A hardware-level emulator of the **Ravensburger tiptoi® pen** (2nd generation, "MT" /
Chomptech ZC3202N / Anyka AK1050 SoC).

tt-emu boots the pen's **real, unmodified firmware** by emulating the pen's hardware —
CPU, NAND, audio, the OID (optical) sensor, buttons, and so on — and runs the pen's
`.gme` audio-book files either headless (for scripted testing) or through an interactive
terminal UI where you "tap" OID codes and hear the audio in real time.

![The tt-emu TUI with the firmware-aware debugger open — live statechart, GME interpreter
registers, and OID→script-line routing with symbolic names joined from a tttool
YAML.](docs/tui-screenshot.svg)

> **Status:** early / work in progress — but it boots the real firmware and plays both
> script-based and embedded-binary `.gme` games end to end, with a live firmware-aware
> debugger.

## Installation

tt-emu is a Python package (3.10+, cross-platform: Linux / macOS / Windows) that installs
two commands, `tt-emu` (headless) and `tt-emu-tui` (interactive). Install it straight from
GitHub with [uv](https://docs.astral.sh/uv/) — no clone, no PyPI:

```sh
uv tool install git+https://github.com/nomeata/tt-emu
```

Or run it once without installing, using `uvx`:

```sh
uvx --from git+https://github.com/nomeata/tt-emu tt-emu-tui --game game.gme
```

pipx and pip take the same git URL if you prefer them (`pipx install
git+https://github.com/nomeata/tt-emu`). To hack on tt-emu itself, clone it and see
[BUILDING.md](BUILDING.md).

Interactive audio needs a working sound device; without one the tools still run and capture
audio to a file.

## Usage

You need the pen's firmware, but you don't have to hunt for it: **omit the firmware
argument and tt-emu downloads the official `update3202MT.upd`** from Ravensburger's
servers, verifies it against a pinned SHA-256, and caches it (reused offline afterwards).
Pass a path to use your own `.upd` instead. (Games — `.gme` files — you supply yourself.)

```sh
# headless: boot, tap a game's product code + a content code, capture the audio to a WAV
tt-emu --game game.gme --tap product --tap 4716 --wav out.wav

# interactive TUI: tap OID codes and hear the audio live
tt-emu-tui --game game.gme

# ...with the game's tttool source, for symbolic names in the debugger
tt-emu-tui --game game.gme --yaml book.yaml
```

In the TUI, tap the game's **product code** button first (the game mounts), then any
content code. Audio plays at 22050 Hz; the emulator runs several times slower than the
real pen, so live playback may pause to rebuffer — an emulation-speed limit, not a bug.

### The GME debugger

When the loaded firmware is a **recognized build** (currently the 2N "MT" `N0038MT /
20131009` image, identified by a byte-exact fingerprint), the TUI adds live debugger
panels on top of the generic view — press **`d`** to toggle them:

- **Statechart** — the firmware's live QHsm state hierarchy (all 70 states named), active
  leaf highlighted;
- **Transitions** — a clock-stamped log of every state change (push/pop/sibling),
  annotated with the event that caused it;
- **GME interpreter** — the mounted product and file, the `$`-register file with live
  values, playlist/media state, armed GME timers, pending deferred jumps;
- **OID → script** — the last tapped OID, which script line it routed to, and that line's
  conditions (with live register values), actions, and playlist, plus a trace of the
  actions the interpreter actually executed.

It's all **hook-free**: the firmware runs unmodified and the debugger only polls emulator
RAM (plus documented read-only watchpoints for the executed-action trace). Unrecognized
firmware simply keeps the generic panels.

With `--yaml book.yaml` (the tttool source of the loaded `.gme`, plus its sibling
`book.codes.yaml` for the script→OID codes), the panels show **symbolic names**: registers
as `$eingabe` instead of `$2`, taps as `4716 "acht" → line 1/1`, media by name, and the
matched YAML source line. Without a YAML everything works with raw numbers.

## Building & contributing

tt-emu is built clean-room from a distilled set of hardware-interface documents in
[`docs/`](docs/). For the architecture, how to build and test from source, and how the
docs and emulator relate, see **[BUILDING.md](BUILDING.md)**.

## License

MIT — see [LICENSE](LICENSE). (The same license as tttool.)
