# tt-emu

A hardware-level emulator of the **Ravensburger tiptoi® pen** (2nd generation, "MT" /
Chomptech ZC3202N / Anyka AK1050 SoC).

tt-emu boots the pen's **real, unmodified firmware** by emulating the pen's hardware —
CPU, NAND, audio, the OID (optical) sensor, buttons, and so on — and runs the pen's
`.gme` audio-book files either headless (for scripted testing) or through an interactive
terminal UI where you "tap" OID codes and hear the audio in real time.

The **1st-generation ZC3201 pen** (`v0136 / 120117`) is supported too: its unmodified
firmware boots and plays GMEs through the same scripting API (discover the `.gme` → mount
it on a product-OID tap → play a content OID's media). It is recognized automatically from
the `.upd` you pass. The ZC3201 DAC PCM decode is not modelled, so on that firmware a
played clip carries the media *identity* (index / offset+size) rather than PCM bytes.

![The tt-emu TUI with the firmware-aware debugger open — live statechart, GME interpreter
registers, and OID→script-line routing with symbolic names joined from a tttool
YAML.](docs/tui-screenshot.svg)

## Status

This is a AI generated tool, and severely overengineered and large. Maybe it is useful or
fun to play with.

I do not plan to spend a lot of time maintaining or extending this. Issue reports or feature
requests are welcome, but will likely not result in quick action.

Code contributions are not welcome. I did not write this code, so I don’t want to review
changes to this code. Trivial PRs may be fine. Contributions are best made in the form of 
precise, detailed error reports or careful feature descriptions that I can feed to an AI.

## Installation

tt-emu is a Python package (3.10+, cross-platform: Linux / macOS / Windows). It installs one
command, `tt-emu` — the interactive TUI by default, or the scripted flow with `--headless`.
Install it straight from GitHub with [uv](https://docs.astral.sh/uv/) — no clone, no PyPI:

```sh
uv tool install git+https://github.com/nomeata/tt-emu
```

Or run it once without installing, using `uvx`:

```sh
uvx --from git+https://github.com/nomeata/tt-emu tt-emu --gme game.gme
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
# interactive TUI (the default): tap OID codes and hear the audio live
tt-emu --gme game.gme

# ...with the game's tttool source, for symbolic names in the debugger
tt-emu --gme game.gme --yaml book.yaml

# headless (scripted): boot, tap the product + a content code, capture audio to a WAV
tt-emu --headless --gme game.gme --tap product --tap 4716 --wav out.wav
```

In the TUI, tap the game's **product code** first (the game mounts), then any content
code. Audio plays at 22050 Hz. The TUI runs with **realtime pacing** by default: the pen
boots in a few seconds and runs on its real timeline, with each sound produced ahead of
real time and played from a small buffered lead. Pass `--pacing deterministic` for
count-paced, bit-for-bit reproducible runs instead (roughly 10–20× slower than the pen —
the right mode when you need to reproduce an exact run). `--dac-pacing faithful`
additionally paces the emulated DAC to the pen's real audio timeline, for testing
timing-sensitive game behaviour; the captured audio is identical either way.

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

### Scripting (Python API)

For automation and testing of GMEs, drive the pen from Python. `tt_emu.Emulator` is a
small, synchronous (no threads, no callbacks) context manager: you script taps and button
presses and assert on the audio the firmware plays. It boots the real firmware into book
mode on entry, mounts your `.gme`, and — with the game's tttool YAML — resolves OIDs and
media by name.

```python
from tt_emu import Emulator

with Emulator(gme="taschenrechner.gme", yaml="taschenrechner.yaml") as pen:
    pen.tap("product")                 # mount the game (its product code)
    pen.tap("acht")                    # tap an OID by symbolic name (or int code)
    pen.expect_play("acht")            # run until it plays; assert the media name
    pen.expect(pen.registers["eingabe"] == 8, "eingabe should be 8")
    pen.wait("400ms")
    pen.tap("neun")                    # the calculator now holds 89 ("neunundachtzig")
    pen.expect_play("neun")            # spoken first; expect_play returns its Clip
    pen.save_wav("session.wav")
```

Actions each advance the emulation and are their own statement — `tap(oid)` (int code,
`"product"`, or a YAML script/OID name), `wait("400ms" | "2s" | ticks)`, `press("power" |
"vol+" | "vol-")`, and the audio waits below. Assertions use the `-O`-safe helpers rather
than bare `assert` (which `python -O` strips): `expect_play(media)` fuses "wait for the
next playback" with "check its media" and returns the `Clip`, and `expect(cond, msg)`
checks any condition with a clear message. The `wait_for_audio() -> Clip` primitive and the
read-only properties — `state`, `state_chain`, `registers` (by `$`-name), `mounted`,
`now_playing`, `transitions` — remain available for inspection and flexible pytest asserts.
A returned `Clip` carries `.media` / `.index`, the captured `.pcm`, its `.duration`, and
`.save_wav(path)`.

## Building & contributing

tt-emu is built clean-room from a distilled set of hardware-interface documents in
[`docs/`](docs/). For the architecture, how to build and test from source, and how the
docs and emulator relate, see **[BUILDING.md](BUILDING.md)**.

## License

MIT — see [LICENSE](LICENSE). (The same license as tttool.)
