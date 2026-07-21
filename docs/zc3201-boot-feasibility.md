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

## What the lab reference does and does **not** give us (key correction)

The `firmware-re` lab's ZC3201 support (`tools/ttemu/zc3201_emu.py`, `gme_engine_zc3201.py`,
`gme_test.py`, 15/15 on both firmwares) is **not a boot-from-reset model**. It is a
**direct-call + VFS-hook harness**: it loads the images flat, stubs *every* MMIO register to
`0`, hooks `fs_open`/`fs_read`/`fs_seek` (fixed handle 1 over one host file) and the two
voice-play functions, then **calls identified firmware functions directly** with pre-seeded
globals. It never runs the mask ROM, never runs the event pump, never models NAND / IRQ /
timer / OID sensor / audio DMA. `FINDINGS.md` is explicit that ZC3201 boot-from-reset has the
**same** mask-ROM + runtime-registered-dispatch-table dependency as the 2N and is *"not
easier."*

So the reference is an excellent **behavioural spec** for the parts it exercises — and it
hands us, byte-exact, the addresses tt-emu will eventually need for those parts (recorded on
`FirmwareProfile.symbols` for ZC3201):

* the **GME-interpreter twins** (`gme_parse_header` `0x0804572c`, `gme_oid_to_playscript`
  `0x08045358`, `gme_parse_actions` `0x08044ec8`, `gme_exec_command` `0x080446e4`, …) — the
  shared scripted-GME engine, the same interpreter the 2N runs;
* the **HAL entry points** (`fs_open` `0x080040ec`, `fs_read` `0x080041c4`, `fs_seek`
  `0x080041d4`, `voice_play_sample` `0x08097068`, `voice_load_and_play` `0x0809716c`,
  `play_chomp_voice` `0x08097374`, `game_play_oid_voice` `0x08054730`);
* the **app-context globals** (`gb_app_context` `0x0800779c`, `p_pMeGame_slot` `0x081d8854`,
  the gme file-handle pointer `0x080d20a0`, chomp handle `0x080d28fc`);
* statechart landmarks by name only (`state_init_power_on` `0x08030e48`, `state_stdb_standby`
  `0x08036454`, `akoid_open_check` `0x080219a0`, `event_post` `0x08001544`).

What it does **not** give us — and what an authentic tt-emu boot needs — has no reference yet:

* the **pre-init boot entry** MT uses (`app_init_main`-equivalent) and the exact **seed state**
  the mask ROM / early boot would leave (the ZC3201 QHsm frame base, the AO descriptor-table
  registration, the NAND geometry struct location, driver-state bytes);
* the **SoC MMIO register map** for this pen (SysCon/clock, IntC/timer, GPIO, battery ADC) —
  the lab stubs all of it to `0`, so none of the concrete addresses/semantics exist;
* the **NAND/NFC controller** registers + geometry (the lab serves a VFS, never NAND);
* the **OID sensor** capture-state address (the MT `bit_count` `0x08008c09` equivalent) and the
  GPIO pin wiring; the **audio DAC/DMA** registers.

## Status (this branch)

Landed as reusable, MT-unregressed infrastructure — the dependency-ordered plan's **step 1**,
plus the fetch and the substrate:

* **Firmware-target abstraction** — `tt_emu/firmware_profile.py`: a `FirmwareProfile` selects
  the load layout (PROG/nandboot addresses, entry), the download URL + pinned SHA-256, the boot
  generation, and (for ZC3201) the reference addresses above. `loader.py` detects it
  (`Firmware.profile`) byte-exact from the PROG fingerprint + nandboot generation magic; the MT
  module constants are now thin aliases of the MT profile, so `boot.py` / `mmu_boot.py` are
  unchanged.
* **ZC3201 firmware fetch** — `firmware_fetch.ensure_firmware(..., profile=ZC3201)` fetches and
  SHA-verifies the ZC3201 container into its own cache file, exactly like the MT one
  (`03c12f41…a4a6`, `update%20encrypt%20normal%20freq.upd`).
* **From-entry substrate** — `boot.build_bare_machine(fw, profile=ZC3201)` loads PROG identity
  at `0x08000000` + nandboot at `0x07ff8000` and seeds the CPU at `state_init_power_on` through
  tt-emu's **real** `Machine` (no peripherals, no MMU → every MMIO read is `0`, matching the lab
  model). It is **hook-free**: it only loads + runs. Reproduces the probe exactly — 131 distinct
  PCs, span `0x08000a04..0x0809886c`, returns to the entry sentinel with no unmapped access.
* **Tests** — `tests/test_firmware_profile.py` (registry, byte-exact detection, real-image
  detection, the from-entry init-leaf run) and ZC3201 fetch coverage in
  `tests/test_firmware_fetch.py`. `tests/_data.py` resolves the ZC3201 container
  (`$TT_EMU_FIRMWARE_ZC3201` / local / SHA-verified download).

The success criterion — *all gme-based tests passing on both firmwares* — is **not met**: those
tests require booting to book mode and running the interpreter through the unmodified firmware
(taps via the OID sensor, the `.gme` served through NAND, audio via the DAC DMA), which is
blocked on the remaining boot RE below.

## Leg 2 update — the real boot entry, the crt0 seed, and reaching storage init

The **true pre-init boot task was identified** and the firmware now runs authentically from it
through tt-emu's real `Machine` into storage init — steps 1 and (the critical part of) 2 of the
plan. Concretely:

* **Corrected the boot entry.** `state_init_power_on` `0x08030e48` is **not** the boot task — it
  is the INIT-*state leaf handler* (it plays the power-on chomp voice `0x19`, opens files) that
  the event pump dispatches; it has no xrefs. The real chain (all previously unnamed `FUN_*` in
  the ZC3201 decomp, twins of the MT `boot_task_main`/`app_init_main`):
  * **`boot_task_main` `0x080238bc`** — ROM entry (no PROG callers). SoC bring-up, then calls
    `app_init_main`, then the infinite **event-pump loop** dispatching statechart events via
    `sm_dispatch_hierarchy` `0x080016a8` (fetch event → map → call current-state handler at
    `obj+0xc`). The pen's event ring is at `+0x180/+0x182` masked `&0x1f` (not the MT's
    `+0xc0/+0xc2` `&0xf`).
  * **`app_init_main` `0x080236c0`** — subsystem inits, then `fs_storage_mount_init`
    `0x080250e0` (**hangs forever via empty `while(true)` on mount failure**), sets the initial
    statechart state byte, installs the timer/event objects. App-context ≈ `0x0800779c`.
  * `prog_entry` in `firmware_profile.ZC3201` is now `0x080238bc`; the recovered addresses are in
    `ZC3201.symbols` (`boot_task_main`, `app_init_main`, `fs_storage_mount_init`,
    `sm_dispatch_hierarchy`, `mtd_extra_bitmap`, `irq_mask_push/pop`, …).
* **Solved the from-entry seed-state blocker (step 2).** The ZC3201 boot ROM / C-runtime zeroes
  low working RAM (`.bss`) before jumping to `boot_task_main`; entering there skips it, so the
  region keeps the PROG image's stale bytes. The first casualty is the HAL **IRQ-nesting struct**
  at `0x08007d7c` (depth byte `0x08007d8c`): `irq_mask_push` `0x07ffdb00` reads it, and a
  non-zero depth trips its 4-deep overflow guard → `b 0x07ffdb14` (hang) on the *first* critical
  section. The fix — the ZC3201 analogue of the MT `§5.6` seeds — is to zero the `.bss` window
  crt0 clears: `ZC3201.bss_seed = (0x08007000, 0x2000)` (verified **no PROG code executes** in
  that window). This alone takes the run from **88 → ~800 distinct PROG PCs**.
* **The SoC core peripherals are reused verbatim.** ZC3201 is the same Anyka family; the MT
  `SysCon`/`IntcTimer`/`GpioBlock`/`BatteryAdc` at the identical `0x040000xx` addresses work
  unchanged (chip-ID gate passes, clock latches self-clear, battery reads healthy).
* **New substrate + coverage.** `boot.build_zc3201_machine(fw)` assembles the real machine
  (reused core peripherals, no MMU, the crt0 seed, entry at the boot task).
  `scripts/zc3201_realmachine_probe.py` and `tests/test_firmware_profile.py::
  test_zc3201_boots_through_app_init_to_storage` exercise it: the unmodified firmware runs
  `boot_task_main` → `app_init_main` → subsystem inits → **MTD/storage init**
  (`mtd_extra_bitmap` `0x08022a8c`), ~800 distinct PROG PCs, span `0x08000070..0x080c2af8`,
  **no hang**. MT suite unregressed.

## Resume pointer

The firmware now boots authentically to **storage/MTD init and there spins in calibrated
storage-probe delay loops** (HAL udelay `0x07ffb1c0`) waiting on flash hardware that is not
modelled — it has **not** yet reached `fs_storage_mount_init`'s success path, the event pump,
standby, or book mode. Next actionable step is **step 3 (NAND/NFC + image)**:

1. **Model the ZC3201 NAND/NFC controller and provide a NAND image** so `fs_storage_mount_init`
   `0x080250e0` mounts instead of spinning/hanging. Start from `build_zc3201_machine`; trace the
   storage init from `app_init_main` `0x080236c0` → the MTD/NFTL layer (`mtd_*` names in
   `ZC3201/input/names.csv`: `mtd_extra_bitmap` `0x08022a8c`, `mtd_open_maptbl` `0x08027f50`,
   `nand_disk_mount` `0x0802b45c`) and find which MMIO register block + status/ready bit the
   probe delay loop polls (disassemble the caller of udelay `0x07ffb1a4`). Cross-check the NAND
   geometry against the MT `NandImage`/`NfcController` — likely re-pointable, as the core
   peripherals were. This unblocks `app_init_main` returning and the event pump starting.
2. Once the pump runs: drive the descent power-on → standby → (cover/product-OID tap) → book via
   the modelled OID sensor + GPIO (re-point the MT `OidSensor`), and serve the `.gme` through the
   NAND/FS. The generic scripted-GME interpreter is a proven twin (`ZC3201.symbols` gme_* + the
   `firmware-re` lab), so once book mode is reached the interpreter path should run.
3. Add a ZC3201 branch of the `firmware.mt` debugger (the ZC3201 AO struct / event-ring geometry
   above, register-file `0x081d8328`, the `gme_exec_command`/`voice_*` watchpoints from
   `FirmwareProfile.symbols`) so `Emulator`/`runner` observe book mode, taps and plays — then
   parametrise `tests/test_scripting.py` etc. over both firmwares.

Everything up to `build_zc3201_machine` reaching storage init is done and green; work resumes at
item 1 (the ZC3201 NAND/NFC model + image).
