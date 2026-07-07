# `minimal_mb.gme` — a minimal embedded main-binary GME (load-path fixture)

`minimal_mb.gme` is a **main-binary GME**: unlike a play-script GME (e.g.
`taschenrechner.gme`, which the firmware's GME *interpreter* runs), this one
carries a native ARM blob at header offset `0xA8` that the firmware **loads and
executes**. It is the fixture for `tests/test_main_binary.py`, which proves the
emulator drives that path end-to-end, hook-free (see `docs/firmware-2n-mt.md`
§8 "Main-binary GMEs").

## What the blob does (`main.c`)

Linked to run at the firmware's binary load address **0x08132000** and entered
with `r0 = &system_api`. It leaves a word trail at a fixed in-region address
(`0x08141F00`) and calls two `system_api` slots:

| MARK[i] @0x08141F00 | value | proves |
|---|---|---|
| 0 | `0xDEADBEEF` | the embedded code executed at the load address |
| 1 | `is_audio_playing()` return (0/1) | a `system_api` call reached the firmware and returned |
| 2 | `0x5A5A0001` | control returned from that call |
| 3 | `0x5A5A0002` | control returned from `play_sound` (the audio slot) |

## How it was built (not run at test time — the `.gme` is committed)

Built with an ARM cross-compiler (`arm-none-eabi-gcc`, `-Dbuild_for_2N`), linked
at the firmware's binary load address `0x08132000`, `objcopy`'d to a raw `.bin`,
and wrapped by a small GME packer that:

- synthesises a firmware-valid header — magic `0x238B` @0x08, XOR key `0x39`
  @0x1C, product id 42 @0x14 — plus a media table @0x04;
- injects the blob into the **main-binary table @0xA8**; and
- sets the header **`0xA4` flag to 1** — the byte the firmware reads
  (`*0x081DA088`) to decide "this product has a separate main binary → launch it".

`main.c` and `2N.bin` are kept here for provenance; only `minimal_mb.gme` and
`start.wav` are load-bearing for the test (no cross-compiler needed to run it).
Adding a self-contained main-binary builder to this repo's `tests/firmware/`
toolchain (so binary GMEs can be built in-repo) is a natural follow-up.
