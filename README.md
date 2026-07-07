# tt-emu

A hardware-level emulator of the **Ravensburger tiptoi® pen** (2nd generation, "MT" /
Chomptech ZC3202N / Anyka AK1050 SoC).

tt-emu boots the pen's **real, unmodified firmware** by emulating the pen's hardware —
CPU, NAND, audio, the OID (optical) sensor, buttons, and so on — and can run the pen's
`.gme` audio-book files either headless (for scripted testing) or through an interactive
terminal UI where you can "tap" OID codes and hear the audio in real time.

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
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"      # installs the package + test deps
pytest                       # run the test suite
python -m tt_emu path/to/firmware.upd   # headless boot
```

tt-emu takes the pen's firmware as an input (like a ROM for a console emulator); it is
not distributed with the tool.

## License

TBD.
