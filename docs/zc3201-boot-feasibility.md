# Booting the 1st-generation (ZC3201) firmware — feasibility & plan

tt-emu models the **2nd-generation "MT" pen** (ZC3202N / AK1050) and boots its real
firmware authentically: it seeds the exact state the skipped early-boot stages would have
produced, hands off to PROG through the firmware's own MMU, and demand-pages
(`docs/memory-map-and-boot.md` §5, `tt_emu/boot.py`, `tt_emu/mmu_boot.py`). This note
records what it takes to extend that to the **1st-generation ZC3201** firmware, and the
current state of the work.

## Ground rule: no hooks

tt-emu's defining principle is that it runs the firmware **unmodified** — it models
hardware, it does not hook or patch firmware behaviour. A ZC3201 emulator already exists
elsewhere (the `firmware-re` lab's `ttemu/zc3201_emu.py`), but it is a **direct-call + VFS
-hook harness**: it stubs MMIO, hooks `fs_open`/`fs_read`/`fs_seek`, and calls identified
functions directly. That is exactly the shortcut tt-emu avoids, so it **cannot be ported in**;
ZC3201 support here must be a real hardware/boot profile.

## What the probe shows (good news)

`scripts/zc3201_boot_probe.py` loads the real ZC3201 PROG + nandboot with tt-emu's own
ANYKA106 loader and runs from the `state_init_power_on` entry, observing only:

```
loaded update.upd: build v0136, boot gen ANYKANB0, PROG 0x100000, nandboot 0x6fe4
entry 0x08030e48: returned to LR sentinel after 132 instructions
no unmapped access — identity-mapped PROG runs without a demand-paging MMU
PROG execution span: 0x08000a04..0x0809886c (131 distinct addresses)
```

Two things matter here:

1. **ZC3201's PROG is identity-mapped at `0x08000000` (1 MiB).** The MT's PROG lives at
   `0x08009000` and uses non-identity frames, which is *why* the MT boot needs the whole
   `mmu_boot.py` demand-pager (run nandboot `init2`, service aborts, back a romboot store).
   ZC3201 shows **no unmapped access** running from entry — its image fits in identity RAM,
   so it very likely needs **no demand-paging MMU at all**. That removes the single most
   complex piece of the MT bring-up.
2. The entry state runs cleanly and returns (it is the QP statechart's init leaf, which
   returns to let the event pump drive) — no immediate wall.

So an authentic ZC3201 boot looks **more tractable than the MT's was**, not less.

## What a full ZC3201 target still needs

This is a multi-step bring-up, on the order of the original MT boot work — not a one-shot.
The pieces, in dependency order:

1. **Firmware target abstraction.** Generalise the 2N-MT constants that are currently
   module-level in `loader.py` / `firmware_fetch.py` / `boot.py` / `machine.py` into a
   selectable profile: PROG load address (`0x08000000` vs `0x08009000`), the firmware URL +
   pinned SHA-256, the memory map, and the boot generation. The loader's ANYKA106 parse is
   already generic (it parses the ZC3201 `.upd` correctly today). ZC3201 firmware:
   `https://cdn.ravensburger.de/db/firmware/update%20encrypt%20normal%20freq.upd`,
   sha256 `03c12f41b6bc9ab78ee206c2bdfdbe45eb9ad7c3ab8337e7a8661555aae4a4a6`.
2. **From-entry seed state.** The MT boot seeds the handful of values the mask ROM / early
   boot would have set (NAND geometry struct, a few driver-state bytes — `boot.py` §5.6).
   ZC3201 needs its own equivalents. These are recoverable from the `firmware-re` ZC3201
   FINDINGS and by observing what the lab hook-harness had to supply.
3. **Peripheral models at ZC3201 addresses.** Re-point (not re-invent) tt-emu's NAND/NFC,
   OID-sensor, timer/IRQ and audio models to ZC3201's register addresses and its NAND
   geometry. The 1st-gen pen has **no recording / audio-player** hardware, so those models
   are simply absent for ZC3201.
4. **Drive the statechart pump + serve storage.** Reach book mode by running the event pump
   (as the MT path does) and serve a built NAND image / VFS holding the `.gme` under test —
   authentically, through the NAND controller, not via `fs_*` hooks.
5. **Parametrise the gme-based tests over both firmwares.** Once ZC3201 boots to book mode
   and runs the shared GME interpreter (the interpreter twins are catalogued in
   `tt-firmware-reveng/correspondences.tsv`), run the GME/OID-tap/play tests against both
   targets. Tests for MT-only features (recording, audio player) stay MT-only, marked with a
   reason.

## Status

Not yet a working boot: this branch adds the probe and this plan, and confirms the loader
already handles ZC3201 and that no MMU is required. The success criterion — *all gme-based
tests passing on both firmwares* — depends on steps 2–5 above, which is a focused bring-up
effort rather than a quick change. The `firmware-re` ZC3201 analysis and the lab hook-harness
are the reference for the seed state and peripheral addresses.
