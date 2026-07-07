# Building tt-emu

tt-emu is built **clean-room**: a distilled set of hardware-interface documents is written
first, and the emulator is implemented against *only* those documents — never against prior
reverse-engineering material. This keeps the docs honest (they have to be sufficient on
their own) and keeps the emulator faithful to the hardware rather than to any one firmware.

## Approach

1. **`docs/`** — self-contained, implementation-oriented documentation of each independent
   hardware component (memory map & boot, NAND + NFC, the OID sensor, audio DAC/DMA, GPIO,
   the ZC90B auth chip, …), written to be sufficient on their own to implement the emulator.
   Start at [`docs/index.md`](docs/index.md).
2. **`src/tt_emu/`** — the emulator, implemented from the docs: a Unicorn ARM926 core, a
   memory-map + MMIO/peripheral framework, per-peripheral models, the boot recipe, headless
   scripted operation, and the Textual TUI.
3. **`tests/`** — a headless test suite, including tiny purpose-built firmware/GME blobs
   cross-compiled in-repo (a header + Makefile + linker script; only `arm-none-eabi-gcc` is
   required, no external toolchain checkout).

## Stack

- Python ≥ 3.10, cross-platform (Linux / macOS / Windows)
- [Unicorn Engine](https://www.unicorn-engine.org/) — ARMv5 (ARM926EJ-S) CPU emulation
- [Textual](https://textual.textualize.io/) — the interactive TUI
- [sounddevice](https://python-sounddevice.readthedocs.io/) — real-time audio output
- NumPy

## Repository layout

| path | contents |
|------|----------|
| `docs/` | hardware-interface documentation (start at [`docs/index.md`](docs/index.md)) |
| `docs/firmware-2n-mt.md` | the specific firmware build the debugger understands |
| `src/tt_emu/` | the emulator implementation |
| `tests/` | test suite + the in-repo test-firmware toolchain (`tests/firmware/`) |
| `scripts/` | tooling — e.g. `screenshot.py`, which regenerates the README screenshot |
| `PLAN.md` | roadmap / build plan |

## From a source checkout

```sh
git clone … && cd tt-emu
python -m venv .venv && . .venv/bin/activate   # or: uv venv
pip install -e ".[dev]"                        # or: uv pip install -e ".[dev]"
pytest                                         # the test suite (ruff + mypy are dev deps too)
```

## The test-firmware toolchain

`tests/firmware/` is a small, self-contained bare-metal ARM test harness: a `tt_test.h` SoC
MMIO header, a linker script, and `.c` blobs that each assert one peripheral contract
(chip-ID / boot constants, GPIO, timer IRQ, NAND, DAC DMA, power-off). They are compiled by
`arm-none-eabi-gcc` and run headless against the emulator's peripheral models, so the tests
exercise the hardware models the same way the real firmware does. The committed `.gme`
fixture for the embedded-binary load path lives under `tests/data/`.

## Regenerating the screenshot

```sh
python scripts/screenshot.py       # writes docs/tui-screenshot.svg
```

It drives the TUI headlessly (Textual's test pilot) to a populated debugger view and
exports the SVG.

## The firmware is an input

tt-emu takes the pen's firmware as an input, like a ROM for a console emulator; it is not
distributed with the tool. By default it downloads the official `update3202MT.upd` and
verifies it against a pinned SHA-256 (see `src/tt_emu/firmware_fetch.py`); an explicit
`--firmware`/path argument bypasses the download.
