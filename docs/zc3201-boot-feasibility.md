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

## Leg 3 — the load base was wrong (0x08008000, not 0x08000000), + the NFC re-point

The stall in "storage-probe delays" was a symptom, not the wall. Tracing the actual spin
(HAL udelay `0x07ffb1a4`, which busy-loops `((clock>>20)·µs>>4)·3` iterations) showed the
CPU-frequency global `0x08006fac` holding a **garbage 3.75 GHz** value, which then made the
clock-set helper (`0x080000e8`) spin forever in its shift-search. The garbage came from a
CPU-frequency `.rodata` **table** the clock code reads through a **baked absolute pointer
`0x080251b8`** — and at load base `0x08000000` that pointer lands inside
`fs_storage_mount_init`'s **code**, not on the table (whose bytes are at file offset
`0x1d1b8`).

**Root cause: the ZC3201 PROG runtime link base is `0x08008000`, not `0x08000000`.** This is
the *same wrong-base mistake the 2N-MT project made* before it was corrected to `0x08009000`.
At `0x08000000` the **code** runs (it is PC-relative, hence the earlier "clean 800-PC boot"),
but every absolute **data** pointer is `0x8000` too low, so dispatches quietly misfire. Proof
(the reveng project's own "does this pointer resolve to a function prologue?" metric, extended
to include `0x08008000` as a candidate): absolute code-pointer tables in the image resolve to
valid ARM prologues **423 : 40** for `0x08008000` over `0x08000000` (the MT base `0x08009000`
gets 3). Independently reproduced by the coordinator: a sharp peak at `0x08008000` (28.8%) vs
`0x08000000` (2.3%), the same method recovering MT's known-correct `0x08009000` (19.1%). The
`0x080251b8` pointer + the table's file offset `0x1d1b8` force `base = 0x080251b8 − 0x1d1b8 =
0x08008000` exactly; there and only there do the freq table (`0x080251b8`) and
`fs_storage_mount_init`'s code (`0x0802d0e0`) stop overlapping. **This is not scatter-loading**
— there is no copy-table; it is purely the flat load address, exactly like MT's `0x08009000`.

Implemented (this leg), all built on the settled from-entry + reused-MT-peripheral substrate:

* **Load base fixed to `0x08008000`.** `firmware_profile.ZC3201`: `prog_load = 0x08008000`,
  `nandboot_alias = 0x08000000` (the nandboot HAL is mapped at `0x07ff8000` *and* aliased at
  `0x08000000` exactly like MT, so PROG's `bl 0x0800xxxx` HAL veneers — `= 0x07ffxxxx+0x8000`
  — resolve), `prog_entry = 0x0802b8bc` (`boot_task_main`, reveng `0x080238bc` + `0x8000`).
  Every PROG address in `symbols` is now the reveng value **+ `0x8000`** via the `_z()` helper;
  HAL/nandboot `0x07ffxxxx` targets are unshifted. `bss_seed = (0x08006fe4, 0x101c)` — the
  low-RAM HAL globals *below* PROG (with the correct base PROG no longer clobbers this window;
  the HAL IRQ-nesting depth byte lives here). `boot.build_zc3201_machine` maps the alias and
  applies the seed.
* **Clock wall cleared.** At the correct base the freq table reads `0x00b71b00` (12 MHz) for
  its domain, the CPU-frequency global settles at 96 MHz, udelay is short, and the boot runs
  through clock setup into MTD/storage init and **the NAND/NFC command-list sequencer**.
* **MT storage trio re-pointed verbatim.** ZC3201's NFC (`0x0404a000`) and ECC (`0x0405b000`)
  are at the *identical* MT addresses (same Anyka family): the HAL NAND-ready poll reads NFC
  `+0x158` bit31 and stages command-list micro-ops at `+0x100`, exactly the sequencer the MT
  `NfcController` implements. `build_zc3201_machine` now adds `NfcController` + `EccEngine` +
  `L2NandBuffer` serving a `NandImage` (blank by default). The firmware drives it (READ-ID,
  status, page reads — 32 NAND reads observed).

## Resume pointer

The unmodified firmware now boots at the **correct base `0x08008000`** through clock setup and
into the **NFC sequencer**, then hits the next wall: with a **blank NAND** the mount reads
`0xFF` pages and the MT NFC model **deposits them into its SRAM staging window `0x08006800`,
which for ZC3201 collides with the nandboot alias's *code*** (the HAL lives up to offset
`~0x6fe4`; the GPIO leaf is at `0x07ffe954` = alias `0x08006954`). The clobbered code then
faults (invalid instruction at `0x08006960`). Two coupled next steps:

1. **Find the ZC3201 NAND data-staging SRAM window** (the MT `SRAM_WINDOW = 0x08006800` is
   wrong here — it is nandboot code) and make `NfcController`'s window a per-instance parameter.
   Disassemble the ZC3201 nandboot NAND page-read leaf (the bulk-read path that memcpys from the
   staging SRAM after a `0x119` data micro-op) to recover the address; candidate low-SRAM
   literals in nandboot include `0x08005000/0x08005800/0x08005b00`. This removes the crash.
2. **Build a valid ZC3201 NAND image** so `fs_storage_mount_init` `0x0802d0e0` mounts instead of
   erroring. ZC3201 uses `MtdLib_Base_1_0_10` (older than MT's `NFTL_V1_2_11`) — trace the mount
   from `app_init_main` `0x0802b6c0` → MTD/NFTL layer (`mtd_extra_bitmap` `0x0802aa8c`,
   `mtd_open_maptbl` `0x0802ff50`, `nand_disk_mount` `0x0803345c` — all reveng + `0x8000`) and
   cross-check geometry against the MT `NandImage`/`build_nand_image` template. Then drive the
   pump → standby → (product-OID tap) → book and serve the `.gme` (re-point the MT `OidSensor`),
   and parametrise `tests/test_scripting.py` over both firmwares.

**Address convention going forward:** the `tt-firmware-reveng` ZC3201 DB is now **re-based to
`0x08008000`** (`names.csv`/`correspondences.tsv`/`firmware.json loader_base` all at the true
base), so its addresses are **runtime addresses directly**. The `firmware_profile.ZC3201.symbols`
values were entered as the *old* (`0x08000000`-base) reveng values lifted with `_z()` (+`0x8000`);
they equal the re-based names.csv runtime addresses, and are the source of truth here.

## Leg 4 — the data-global fix, the NAND-staging SRAM window, and the FS chip-ID gate

Three coupled corrections, each verified against the image, took the from-entry boot from
"reaches the NFC sequencer then crashes on a blank NAND" to **3224 distinct PROG PCs, deep into
the FAT directory layer** — clearing every seed/signature wall between clock setup and the point
where real filesystem *content* is first needed.

1. **Data globals are absolute literals — do NOT `+0x8000` them.** Leg 3's `_z()` lift is correct
   for *code* pointers (PC-relative code linked at `0x08008000`) but wrong for **data** globals,
   which are baked as absolute literal words already encoding the final RAM address. Verified by
   counting 4-aligned literal occurrences in `ZC3201/data/PROG.bin`: `gb_app_context 0x0800779c`
   (106×; the shifted `0x0800f79c`: 0×), `p_pMeGame_slot 0x081d8854` (2×; shifted 0×),
   `gme_file_handle_ptr 0x080d20a0` (74×), `chomp_handle_ptr 0x080d28fc` (1×). All four are now
   unshifted in `firmware_profile.ZC3201.symbols`. (Also fixed a nibble typo in `fs_open/read/seek`
   — they were `0x080480ec/81c4/81d4`; the re-based `names.csv` gives `0x0800c0ec/c1c4/c1d4`.)

2. **The NAND data-staging SRAM window is `0x08005800`, not the MT `0x08006800`.** The L2 buffer
   block is fixed hardware SRAM at a *generation-specific* base; MT stages into buffer-4
   `0x08006800`, but ZC3201's nandboot alias code extends to `~0x08006fe4`, so depositing read
   data at `0x08006800` clobbered a HAL leaf → fault at `0x08006960`. Disassembling the ZC3201
   nandboot NAND data-transfer leaf (three functions at `0x08002320/0x08002a00/0x08002cd8`, each
   pairing the ECC-engine literal with a `memcpy(dst, 0x08005800, 512)`) recovers **buffer-4 =
   `0x08005800`** (buffer-0 base `0x08005000` + 4·`0x200`, same stride as MT). `NfcController`'s
   window is now a per-instance parameter (`profile.nand_sram_window`); MT keeps `0x08006800`.
   This alone advanced the boot ~800 → **2307 PCs**, past the crash, through `fs_storage_mount_init`
   into `fs_open`.

3. **The FS library version gate reads the SoC chip-ID — ZC3201 answers "1923", not MT's "1090".**
   With the SRAM window fixed, the mount reached `fw_version_ref` (`0x0802c880`) / its twin
   `mtd_helper_eb24` (`0x080b6b24`): each copies a library vtable into a fixed descriptor
   (FAT lib at `0x08007c14`, MtdLib at `0x081d9ad8`), then gates on a **chip signature** — case 4
   of the switch reads `*0x04000000` (SysCon REG_CHIP_ID) and compares it to `0x33323931` ("1923").
   tt-emu's `SysCon` returned the MT constant `0x30393031` ("1090"), so the gate **failed and
   memset the descriptor to zero**, nulling its allocator vtable; a later FAT tagged-malloc
   (`string_fn_e674` `0x080b6674`, `(**(code**)(desc+0x18))(...)`) then called the null pointer and
   ran off into the zero page (fault at `0x00010000`). `SysCon.chip_id` is now per-instance
   (`profile.soc_chip_id`; ZC3201 = `0x33323931`). This is authentic hardware modeling (the real
   ZC3201 SoC returns "1923" there), not a hook. Boot advanced **2307 → 3224 PCs**;
   `mtd_extra_bitmap` now runs 45× and `fs_open` 3×.

### Leg 4 resume pointer

The from-entry ZC3201 boot now runs `boot_task_main` → `app_init_main` → clock → **MTD init →
FAT mount → the FAT directory layer**, with `SysCon(chip_id=0x33323931)`, the storage trio at MT
addresses, and `NfcController(sram_window=0x08005800)`. Next wall: an out-of-bounds byte read at
`0x0844000c` (past the 4-MiB RAM top `0x08400000`) from the nandboot **strcmp** leaf
(`0x0800320c`; the fault is its `ldrbne r3,[r1,r2]` at `0x08003228`). One of the two strcmp
operands is a garbage pointer (`~0x08440000`) — the FAT layer is comparing a path/filename against
**uninitialized blank-NAND metadata**. This is the first point that needs real filesystem
*content*, so the remaining step is unchanged and now unblocked:

* **Build a valid `MtdLib_Base_1_0_10` NAND image** so the mount reads real map-table + FAT +
  directory data instead of `0xFF`. Trace `fs_storage_mount_init` `0x0802d0e0` → `mtd_helper59`
  `0x0802d408` (returns the fixed MTD device object; sets its op vtable at +0x28..+0x44) →
  the map-table read `(*(dev+0x2c))(dev,0,0,num_blocks-1,buf,…)` which expects `==1` for a valid
  map (blank NAND returns ≠1 → mount silently returns 0 "empty" and the later directory scan walks
  garbage). ZC3201's MtdLib layout differs from the MT `NFTL_V1_2_11`; cross-check geometry against
  the MT `NandImage`/`build_nand_image` template and the reveng `mtd_open_maptbl` `0x0802ff50` /
  `nand_disk_mount` `0x0803345c`. Then drive the pump → standby → (product-OID tap) → book, serve
  the `.gme`, add a ZC3201 branch of the `firmware.mt` debugger, and parametrise
  `tests/test_scripting.py` over both firmwares.

## Leg 5 — the reusable producer-provisioning seam + the exact mount requirement

The unblock is a valid NAND image; the chosen path is to run the firmware's **own factory
`producer.bin`** (shipped in the `.upd`, `Firmware.producer`) against a Python NAND model and
capture the exact `MtdLib`/`FatLib` bytes it writes — guaranteed-correct where a hand-built
image is guesswork, because the producer and firmware share the `MtdLib` library (the producer's
`MtdLib59:%d` log == the firmware mount's `mtd_helper59` `0x0802d408`).

* **`tt_emu/nand_provision.py` — the standard, profile-driven, cached provisioning seam.** It
  loads `producer.bin` under Unicorn, hooks only the producer's libc + OS-vtable NAND leaves (it
  is a provisioning *tool*, not the firmware under test — the same seam the MT reference image was
  built with, `firmware-re/tools/ttrun_producer.py`), drives the factory format sequence (cmd 5
  init → 7 ASA → 9 partition → 0x22 write-maps → 10 mkfs), and returns a `NandImage` the real
  `NfcController` serves to the **unmodified** firmware. Addresses live in
  `FirmwareProfile.producer` (`ProducerProfile`) so MT and ZC3201 share one path; results cache on
  disk keyed by producer+firmware content. **Validated on MT**: the port reproduces `firmware-re`'s
  reference `producer_nand.img` **byte-for-byte** (448/448 pages) and lays down the ASA + block-0
  metadata magics. `build_zc3201_machine(fw, provision=True)` uses it. ZC3201's `ProducerProfile`
  is pending its `producer.bin` RE (`docs/zc3201-producer-addresses.md`).

* **The exact mount requirement (traced this leg).** `fs_storage_mount_init` `0x0802d0e0`:
  `mtd_helper59` builds the fixed MTD device `0x08007d94` and installs its op vtable —
  `dev+0x2c` (the map-table read) = **`0x0800c208`** (`FUN_0800c208`), which reads the device's
  **last page** (`row = *(dev+0x14)·0 + (*(dev+0x14)-1)`) via the NAND read primitive
  `func_0x080028b0` and returns `1` when the read succeeds; the mount then reads an FS-info header
  from that buffer (`iVar7 = *(buf+4)` sector count, `uVar1 = *(buf+8)` shift) to size the disk
  objects. **Empirically (instrumented probe): on a blank NAND `0x0800c208` is never reached** —
  mount bails *before* the map read, because `dev+0x14`/`dev+0x1c` (geometry) and the alloc
  `FUN_0802b350(*(dev+0x1c))` depend on the earlier `MtdLib` inits (`mtd_extra_bitmap` `0x0802aa8c`,
  `mtd_MapTblInit` `0x08035318`) having read **real on-NAND map/zone metadata** (they issue the 32
  NAND reads observed, all returning `0xFF`). Mount then returns 0, the boot proceeds *unmounted*,
  and `fs_open` walks a garbage FAT → the nandboot `strcmp` leaf `0x0800320c` faults on an OOB
  path pointer at `0x0844000c` (`ldrbne` at `0x08003228`). So the fix is unchanged and now precise:
  a producer-formatted image whose `MtdLib` metadata makes those inits populate the device geometry
  and whose FAT area carries a real directory. (Note: as on MT, the producer's cmd-10 FAT mkfs may
  not run without the full FsLib/Medium device-I/O stack; the FAT partitions may need `build_fat16`
  placed into the producer-formatted MtdLib layout — a hybrid mirroring MT's `build_nand_image`.)

### Leg 5 — the ZC3201 producer is architecturally different from MT (RE'd)

`docs/zc3201-producer-addresses.md` is the full ZC3201 `producer.bin` RE. The load-bearing
finding: **the ZC3201 producer is NOT the MT/OLD structural twin the plan assumed** — it shares
only the FS/MtdLib/FatLib library lineage; the producer app layer is an older, heavily-stripped
build (33 strings, BURN 2.3.51, `MtdLib_Base_1.0.8` banner). The MT-shaped
:class:`ProducerProfile` (two dispatchers + a cmd-ctx global + a flat OS/HAL NAND vtable at
`+0x20..+0x38` + the 6 metadata magics) **does not fit it**. Concretely (all Proven):

* **One** ring dispatcher `0x08003664` (jump table `0x080036c4`, 29 entries), **no** burn/media
  split and **no** command-context global — the command is `packet[0..3]` of a 12-byte packet in a
  ring (write-idx `0x08015610`, read-idx `0x08015614`, base `0x08024fa0`); commands are `1..0x1d`,
  there is **no cmd 0x22**. Entry `0x08000000`→ main `0x08003430`; USB loop (harness stop)
  `0x080034a4`; `printf 0x080003dc`, `malloc 0x080004d4`, `free 0x080004e0`; `gb_RAMBuffer`
  `0x08015710`.
* **No flat NAND vtable.** NAND read/write/erase are dispatched **indirectly** through the
  MtdLib/medium object method pointers (`ldr pc,[ip,#0x48]` at `0x080125cc`), so the MT vtable hook
  seam does not transfer and the exact NAND leaves were **not statically pinnable** — they need
  **dynamic tracing** (bands: nandmtd `0x0800e078..0x08010848`, nandflash `0x08012670+`).
* Command→worker edges (Proven): cmd 5 init `0x08002da0`/`0x08002eb4`; cmd 7 **and** cmd 10 →
  the central **format worker `0x0800849c`** (iterates the full chip from geometry global
  `0x0801569c`); cmd 9 `0x0800a1bc` (asa config). Only **two** metadata magics exist
  (`0x11235813` blank, `0x12345678` boot) — the map/bin-info/zone-table scheme is **absent**.

Consequence: the reusable :mod:`tt_emu.nand_provision` seam (validated byte-exact on MT) needs a
**ZC3201 harness variant** — a ring/single-dispatch driver plus NAND hooks placed at the nandmtd
/nandflash bands, with the exact leaves recovered by dynamic tracing (a
`scripts/zc3201_prod_trace.py`-style run: startup → cmd 5 → cmd 7, logging the PCs that touch the
`0x0404xxxx`/`0x04010000` NAND-DMA MMIO). The current `ProducerProfile` fields are MT-shaped; a
ZC3201 variant should carry `dispatch`, `ring_*`, `usb_loop`, `format_worker`, and the traced
NAND-leaf addresses instead of the MT vtable slots.

### Leg 6 — the chip-detect wall is SOLVED; the real format protocol is driven

`scripts/zc3201_producer_probe.py` now drives the **real factory command protocol**, not cmd 5 in
isolation, and the "gNand init fails chip-detect" wall from Leg 5 is cleared. Full detail (Proven,
byte/disasm-cited) is in `docs/zc3201-producer-addresses.md` §9; the load-bearing findings:

* **The format is a 3-command host sequence**, recovered from the workers' own debug strings:
  **cmd 2 `transc_get_chip_id`** (worker `0x08001260`) → **cmd 3 `transc_set_chip_param`**
  (`0x08001430`) → **cmd 5 `transc_format`** (`0x08001660`). Driving cmd 5 alone (as Leg 5 did)
  fails because the geometry has not been loaded yet — that was the whole "gNand init fails". (§4's
  semantic labels were mis-assigned; these are Proven from the strings.)
* **The ring packet's `arg0` (`pkt[4..7]`) is a POINTER** to the data buffer (the dispatcher
  forwards `r0 = arg0`). cmd 3's buffer is a **287-byte chip-param blob**; cmd 3 memcpy's it into
  `0x0801f751`, reads the physical chip-ID over **NFC `0x0404a150`** (the ZC3201 NAND HAL uses BOTH
  the `0x0404a000` and `0x04070000` bands), and — when the returned ID matches the blob's `[0:3]`
  expected-ID — prints **`find chip=1`** and builds the gNand device descriptor `*0x08024b50`.
* **The NAND method-vtable leaves are pinned STATICALLY** (Leg 5 expected these to need dynamic
  tracing): `desc[+0x28]=0x08005c1c +0x2c=0x08005b14 +0x34=0x08005be0 +0x38=0x08005d44
  +0x3c=0x08005db0 +0x40=0x08005a04 +0x44=0x08005af8 +0x30=0x08005cd8` — the read/write/erase/
  readspare leaves the capture harness hooks. The descriptor↔blob geometry decode is in §9.

**Leg 6 resume pointer.** With `cmd 2 → cmd 3 → cmd 5` and a matching chip-ID, the gNand init
`0x08002eb4` **no longer errors** — it allocs against real geometry, then builds the FatLib/MtdLib
**medium** (`0x0800cd78` + `0x08012a88`) and calls a still-**null** method pointer → fetch fault at
PC `0x10000`, **caller LR `0x0801271c`** (nandflash band). Two coupled next steps, both now precise:
1. **Get the real geometry blob.** The placeholder blob (`scripts/zc3201_producer_probe.py`) yields
   a *scrambled* geometry (`page size=1`, `planes=64`, `plane size=512`) because the exact 16-bit
   sub-field encoding needs the pen's true `flash_ic.ini` values — the real ZC3201 NAND chip-ID and
   its page/block byte sizes. Source candidates: the Windows host tool's `flash_ic.ini`, or derive
   from the firmware's own accepted chip-ID (`tt_emu/peripherals/nand.py` `NAND_READ_ID =
   0x9551D3EC`, Samsung K9GAG08U0M) and standard geometry. A correct blob makes the medium's method
   table initialise fully (no null slot).
2. **Capture the format.** Once the medium builds, disassemble/hook the null-method caller at
   `0x0801271c` (nandflash band) and the vtable leaves `0x08005b14/0x08005c1c/0x08005be0/...`, drive
   cmd 5's format worker `0x0800849c`, and capture the NAND writes into a `WritableNand` — the
   ZC3201 variant of `tt_emu.nand_provision` (ring-dispatch driver, hooks at the static leaves).
   Then convert to a `NandImage` the real `NfcController` serves and reach `fs_storage_mount_init`
   `0x0802d0e0` mounting A:/B:.

### Leg 7 — the MtdLib-init wall is SOLVED; `transc_format` completes

Leg 6's "still-null method → PC `0x10000` fault" is **fully cracked** (Proven, byte/disasm-cited in
`docs/zc3201-producer-addresses.md` §10). It was not the geometry alone — it was a **SoC-signature
check**. `scripts/zc3201_producer_probe.py` now drives cmd 2 → cmd 3 → cmd 5 to completion.

Three ingredients:

1. **The cmd-3 blob IS the `.upd`'s own `flash_ic` descriptor** at `update.upd[0x200:0x240]` — the
   Samsung **K9F5608** row (chip-ID `EC 75 A5 BD`; page 512, spare 16, 32 pages/block, 2048 blocks,
   planeblocks 1024, col-cycles 1, row-cycles 2, custom 1). NOT a hand-built struct and NOT the
   K9GAG08U0M the Leg-6 note guessed — ZC3201 is a **512-byte-page** chip, MT's `NAND_READ_ID =
   0x9551D3EC` is the *MT* chip. The critical field is `columnaddrcycle==1` (`blob[0xf]`), which
   selects the small-page decode path; the Leg-6 placeholder took the large-page path → the
   scrambled `page size=1, planes=64` geometry.
2. **Physical chip-ID `0xBDA575EC`** on NFC `0x0404a150` (must equal `blob[0:4]`).
3. **SoC chip-ID `0x33323931` ("1923") at `0x04000000`** — the real wall. `mtd_set_pool`
   `0x08012a88` writes the pool method table to global `0x08027090`, then a SoC-signature check
   `0x08012a0c` reads `*0x04000000`; on mismatch it `memset(pool,0,0x18)`-zeroes the table, so the
   `ldr pc,[sb]` at `0x08012718` fetch-faults. Returning the SoC chip-ID passes the check.

Result: cmd 5 `transc_format` returns `RET r0=1` and MtdLib inits the partition —
`MtdLib - NandPart:…,BCnt=2048,PCnt=2,LPCnt=2,BPerP=1024,PgPerB=32,BytPSec=512`.

**Leg 7 resume pointer.** `transc_format` (worker `0x08001660`) only *initialises* the MtdLib
partition — it fires **none** of the 8 static NAND leaves (verified 0 hits during cmd 5). The actual
full-chip erase + FS-metadata + FAT write is other commands: `transc_erase` cmd 4 (`0x080015f8`), the
full-chip iterate worker `0x0800849c` (cmd 7/10), the block/boot writers `0x0800cf14`/`0x08009cb4`
(cmd 11). These need the **host protocol's real packet-argument layout** — cmd 4's "erase start/end"
came through as `0/0` with the naive ring-word mapping, so `arg0` points at a small args struct, not
two inline words. Next steps, precise:
1. **Reconstruct the host format-packet sequence** (the Windows tool's driver): the exact `arg0`
   struct each of cmd 4 / 7 / 10 / 11 expects. Disassemble the workers `0x080015f8` / `0x0800849c` /
   `0x0800cf14` for their arg reads.
2. **Capture** — hook the 8 static leaves (`0x08005c1c` read / `0x08005b14` write / `0x08005be0`
   readspare / `0x08005d44` / `0x08005db0` / `0x08005a04` / `0x08005af8` / `0x08005cd8`), determine
   their calling convention (block/page/data/tag regs, likely different from MT), drive the format,
   and capture into a `WritableNand` — the ZC3201 **ring-dispatch** variant of `tt_emu.nand_provision`
   (a new `run_producer_zc3201`, since the MT `run_producer` is burn/media-dispatcher-shaped).
3. **Serve it** — convert to a `NandImage` with **512-byte-page + 16-byte-spare** fidelity (the
   `NfcController` decode is tuned to MT's 4-KiB pages; ZC3201's small-page + 1-col/2-row addressing
   differs) and give ZC3201 its **K9F5608** read-ID (profile-driven, so MT keeps K9GAG08U0M — see
   `tt_emu/peripherals/nand.py` `read_id` param + `firmware_profile.ZC3201.nand_read_id`). Reach
   `fs_storage_mount_init` `0x0802d0e0` mounting A:/B:, then book mode + GME play, then parametrize
   `tests/test_scripting.py` over both firmwares.

### Leg 8 — the write protocol is mapped; erase captured; the file-upload command is the next wall

Leg 7's resume pointer assumed cmd 4/7/10/11 all funnelled writes through the 8 static
gNand leaves. Driving them under the new `scripts/zc3201_producer_capture.py` (reusing
Leg 7's cmd 2→3→5 recipe) **corrects the command semantics** and pins the real write
seam (full detail, byte/disasm-cited + live run, in `docs/zc3201-producer-addresses.md`
§11):

* **cmd 4 = `transc_erase`** — arg0 → an **8-byte** `{u32 start, u32 end}` struct (memcpy
  8, not two ring words). It fires the gNand **erase leaf `desc+0x38` = `0x08005d44`** as
  `leaf(dev, 0, block)` across the whole chip. **PROVEN captured**: cmd 4 with
  `arg0 → {0,2048}` fires the leaf 2043× (1019 distinct blocks) → the full-chip erase
  lands in a `WritableNand`.
* **cmd 7 = `transc_data`** (worker `0x08001aa8` → `0x0800849c`) — arg0 → a **0x18-byte**
  `{u32 dataLen; u32 ?; char name[16]}` descriptor. It **looks up `name`** in the
  producer's **file-records table** (global `0x08020888`, count `0x0802088c`, 0x24-byte
  records `0x08020898`; search = strcmp `0x08007b40` via iterator `0x08007bc8`) and streams
  the matching file to NAND through the **medium object `0x08027060`** (write via
  `[+8]`), which reaches the gNand **write leaf `desc+0x2c` = `0x08005b14`**.
* **cmd 10 = `transc_update_self`** (not an iterate/format).

**The wall (next leg), now precise.** With an *empty* file-records table cmd 7 finds
nothing and writes nothing (verified `count@0x0802088c == 0`, 0 write leaves). The file
records **and their data** must be UPLOADED by *another* host command before `transc_data`
can commit them. Reverse that upload command — it fills `0x08020888`/`0x08020898` and the
data source the iterator `0x08007bc8` drains — then cmd 7 fires the write leaf and the
writes (MtdLib metadata + FAT + file content) capture into the `WritableNand`. Then:
convert with **512 B page + 16 B spare** fidelity (give ZC3201 its K9F5608 read-ID via the
profile), serve through `NfcController`, reach `fs_storage_mount_init` `0x0802d0e0`
mounting A:/B:, drive pump → standby → OID tap → book → GME play, and parametrise
`tests/test_scripting.py` over both firmwares. The BurnTool plan
(`BurnTool*/config_researcher.txt`) names the files/geometry: `PROG.bin`→NAND `0x0`,
voice→`A:VOIMG` udisk, one FAT partition (type 2), `fs start 0xcc0000`, 64 reserve blocks,
32 MiB K9F5608 (2048×32×512).

The success criterion — *all gme-based tests passing on both firmwares* — remains **not
met**: MT is green (150 passed), ZC3201 has no gme test yet (blocked on mount, which is
blocked on the producer write above). No product code changed this leg (only the capture
script + these docs), so MT is unregressed.

### Leg 9 — the producer write-path model is corrected: cmd 7 does not write; the seam is the USB streaming ring

Leg 8's plan (seed the file-records table + drive cmd 4/7 → capture the file write)
was tested directly with `scripts/zc3201_seed_experiment.py` and the underlying model
proved **wrong**. Full byte/disasm/live-run detail is in
`docs/zc3201-producer-addresses.md` §12. Load-bearing corrections (all Proven):

* **Seeding works for the lookup, not the write.** With `count@0x0802088c=1` and one
  0x24-byte record (`name[16]@+0x14="PROG"`), cmd 7 `transc_data`'s strcmp iterator
  `0x08007bc8` now finds the record and cmd 7 Acks `r0=1` — but fires the gNand write
  leaf `0x08005b14` **0 times** (and every static leaf 0×). cmd 7 only *declares* a
  file; it never puts content on NAND.
* **`0x08027060` is the library-services vtable** (malloc/free/memset/memcpy/…/printf),
  **not a NAND medium** — §11.4's "write via `[0x08027060+8]`" was `memset`.
* **cmd 26 `download_end_data` is a lost-packet finalize, not a flush** (worker
  `0x08002320`→`0x08000a74` just resets the packet counters); driven after cmd 7 it
  prints `Lost packet: 0 the length: 256` and Acks **0** (fail), 0 write leaves.
* **The full 29-command map is now pinned** (§12.1): the two commands that write NAND
  are **cmd 6 `transc_nandboot`** (PROG.bin→boot area) and the **file-content stream**.
* **The real write seam is a producer/consumer DMA ring**: receive-setup `0x080009b8`
  arms `expected@0x080155e8[0]` + the transfer object; the ring `0x08023b20` holds
  0x1000-byte buffers (state word `[+0xc]`: 0 free / 2 filled / 3 drained) that the USB
  bulk-OUT DMA fills and the consumer (`0x08000928`/`0x08000934`) drains through the
  FatLib medium → write leaf `0x08005b14`; `0x080155e8[8]` accumulates received length.

**Leg 9 resume pointer.** The corrected shortcut: seed the file-records table (needed
for cmd 7's lookup) **and** feed the streaming ring directly. After cmd 7 declares
`{name,len}`, write the file bytes into the `0x08023b20` ring buffers, mark each
`state=2`, bump `0x080155e8[8]` to `len`, and run the consumer `0x08000928`/`0x08000934`
(or the USB-IRQ handler that calls it) until write leaf `0x08005b14` fires; then cmd 26
Acks 1. For the boot image, drive cmd 6 `transc_nandboot` with PROG.bin staged the same
way. Capture into `WritableNand` (leaf hooks already in place) and promote to
`run_producer_zc3201` in `tt_emu.nand_provision`. THEN the downstream chain is unchanged:
convert with 512 B page + 16 B spare fidelity, serve through `NfcController`
(`sram_window=0x08005800`, K9F5608 read-ID), mount A:/B: at `fs_storage_mount_init`
`0x0802d0e0`, drive pump → OID tap → book → GME play, parametrise `tests/test_scripting.py`
over both firmwares. Probes/helpers this leg: `scripts/zc3201_seed_experiment.py` (open
end = feed the ring), `scripts/zc_dis.py`, `scripts/zc_lit.py`. MT green (150 passed);
no product code changed (scripts + docs only).

## Leg 10 — THE PIVOT: hand-build the NAND image; the mount is unblocked into the map-table build

Legs 5-9 chased the factory `producer.bin` to *write* a mountable image; §12 proved that path
ends in a USB-DMA streaming ring (no static write seam). This leg **pivots**: hand-build the
ZC3201 NAND image the same way MT's tt-emu feeds its gme tests (`build_nand_image`), driven by
the firmware's own mount path as the acceptance spec. Two RE sweeps (NFC bus protocol + MtdLib
mount format) plus instrumented iteration took the mount from "bails before the map read" (the
4-leg wall) to **deep inside the map-table build**.

### What landed (all MT-unregressed; 150 MT + new small-page tests green)

* **Small-page `NfcController` (`tt_emu/peripherals/nand.py`, profile-driven).** ZC3201's Samsung
  **K9F5608** is 512-B page + 16-B OOB, 32 pages/block, 2048 blocks (16-KiB erase block, 32 MiB).
  The NFC framing is byte-identical to MT (cmd `0x00`/`0x30`, 2 col + 2 row cycles), but a read is
  a **single 528-byte transfer** (512 main + 16 OOB together). Recovered from the nandboot read
  primitive `0x080028b0` + spare primitive `0x08002bac` + `update.upd[0x200]` `flash_ic`:
  * `decode_byte_offset_smallpage`: NFC **row = absolute page = 32·block + page**, flat offset =
    `row·512 + col`, spare/OOB tag keyed by `row`;
  * `_data_read_smallpage` deposits 512 data at the L2 window + the 16-B OOB right after it
    (`window+512`); `_data_program`/`l2_strobe` commit both; erase block = **`row>>5`** (not `>>8`).
  * Selected by `FirmwareProfile.nand_small_page` (+ `nand_page_size`); MT's large-page decode is
    byte-for-byte untouched. Tests: `tests/test_nand.py::test_smallpage_*`.
* **Device-geometry seed (the key unblock).** `dev = 0x08007d94` (`DAT_0802d480`) holds the MtdLib
  geometry the mount reads (`+0x14`=32 pages/block, `+0x1c`=512 page bytes, `+8·+0x10`=2048 blocks,
  `+0xc`=2 planes, `+0x10`=1024 planeblocks, `+8`=2, `+4`=`0x10000000`, `+0`=1). It is populated by
  the **skipped nandboot chip-detect** (READ-ID → `flash_ic` decode, the producer's `0x080056d4`
  twin) — and it sits **inside** the `bss_seed` window that gets zeroed, so it read all-zero and the
  mount bailed before the map read. `FirmwareProfile.nand_dev_geometry` now seeds it *after*
  `bss_seed` (the ZC3201 analogue of MT's §5.6 `NAND_GEOMETRY`), in `boot.build_zc3201_machine`.
* **`tt_emu/nand_image_zc3201.py::build_zc3201_nand_image`** — the small-page MtdLib image: the
  FS-info **superblock** at block 0 page 31 (`+4`=reserve 64, `+8`=shift 9, `+0xa`=2 partitions,
  entries id 0=A: id 1=B:), FAT16 A:(SYSTEM)/B:(tiptoi) volumes (`build_fat16`, spc=1), placed
  identity-mapped in the map region `[reserve,2048)` with per-page OOB tags `0x12560000 | logical`.
  Wired as the **default** NAND for `build_zc3201_machine(profile.nand_small_page)`.

### Where the mount is now (`scripts/zc3201_mount_probe.py`)

The unmodified firmware runs `boot_task_main → app_init_main → fs_storage_mount_init 0x0802d0e0`:
`map_read` (`FUN_0800c208`, reads the superblock at block 0 page 31 → **reserve=64 ✓**) →
`whole_disk_map_build` (`FUN_0802f0c0`) → `map_table_build` (`FUN_0802cbd8`) → **960 spare-tag
scans** (`FUN_0802edb8`), then **hangs at `0x0802d208`** — the infinite loop taken when
`FUN_0802cbd8` returns NULL (the map-table build fails).

### Leg 10 resume pointer — the precise remaining wall

`FUN_0802cbd8 → FUN_0802ea54` returns NULL. Instrumentation shows **the readspare wrapper
`dev+0x34` (`0x08030224`) is never entered** across all 960 `FUN_0802edb8` scans: the classifier
gates the readspare loop on `dev+0x40` (`FUN_08030108`, the per-block **bad-block bitmap** check),
which returns *nonzero* (block "bad") for every page — so no OOB tag is ever read and the map stays
empty. Root cause candidates, in priority order:

1. **Bad-block bitmap index vs geometry.** `FUN_08030108` indexes the 257-byte (2048-bit, per-block)
   bitmap with `uVar3 = dev+0xc·dev+0x10·block + page = 2048·block + page` — which overflows the
   bitmap for any block ≥ 1 (and even block 0's bits must read "good"). Either the seeded `dev+0xc`/
   `dev+0x10` are wrong for this code path, or the bitmap must be **populated** first: on the first
   call `FUN_08030108` runs `func_0x080006fc(0, bitmap, 2048)` (a nandboot leaf that fills the
   bad-block bitmap from NAND) then sets its state byte to 2. Trace `func_0x080006fc` — it likely
   reads each block's factory bad-block marker (spare byte) through the same small-page path and, on
   the blank/mis-served OOB, marks blocks bad. **Fix so `FUN_08030108` returns 0 (good)** and the
   readspare runs.
2. **The OOB "surface" for the spare read.** Once the readspare runs: `func_0x08002bac` reads the
   4-byte tag from **`window[0]`** *after* a strobe (`0x080030d8` → set **GPIO_OUT0 bit 3** via
   `0x08006954`) that surfaces the OOB into the window head — but the emulator deposits the OOB at
   `window+512`. Model the surface: on the GPIO-pin-3 rising edge, copy the last-read 16-B OOB to
   `window[0]` (a `gpio.watch_output(3, …) → nfc.surface_spare()` hook was prototyped and reverted
   pending #1; the mechanism is proven). The classifier wants `tag & 0xFFFF0000 == 0x12560000`
   (low 16 = logical page) for live pages, `0x12345678` for system, `0xFFFFFFFF`→blank.
3. **Reserve-zone map metadata.** The map scan reads the reserve zone (blocks 0-63); the hand-built
   image has only the superblock there. It may need the persistent logical→physical **map-table**
   chain (0x12345678-tagged pages) the producer writes.

**Fastest disambiguation (agent-recommended):** capture ONE producer-formatted image
(`scripts/zc3201_producer_capture.py`, Legs 8/9 — feed the USB ring per §12) and diff block 0 page
31 + a data page's OOB + the reserve zone against this spec to pin the exact `dev` field values, the
OOB byte offset of the `0x1256` tag, and the reserve-zone map format. Then: mount A:/B: → drive
pump → standby → product/cover-OID tap → book → GME play (re-point `OidSensor`, add a ZC3201
`firmware.mt`-style debugger), and parametrise `tests/test_scripting.py` over both firmwares.

**Not yet met:** the success criterion (all gme tests on both firmwares) needs the mount to
complete first. This leg unblocked the 4-leg geometry wall and pinned the exact next failure.

## Leg 11 — the bad-block wall is SOLVED (on-media BBT); the tag source is CORRECTED (page data, not OOB)

Leg 10's candidate #1 (bad-block bitmap) is fully cracked and fixed **authentically** in the
NAND image; candidates #2/#3 are re-characterised with a load-bearing correction. All findings
below are Proven (byte/disasm-cited + live-run instrumented; probes
`scripts/zc3201_badblock_probe.py`, `scripts/zc3201_diag_forcegood.py`, plus scratch traces).

### Candidate #1 — SOLVED: the bad-block table is READ FROM NAND, not computed

`FUN_08030108` `0x08030108` (`dev+0x40`) is the per-page bad-block check the map scan
(`FUN_0802edb8`) gates its readspare loop on. On its first call it **populates a manager
bitmap** (`0x081d97d8` = `DAT_08030418`; state byte `+0`, bitmap ptr `+4`, initial state **1**,
bitmap heap-alloced `0x80fa200`) via the nandboot leaf `func_0x080006fc` `0x080006fc`, then reads
one bit per (block,page). `func_0x080006fc(start, buf, len)` is **not** a scanner — it is a
**read-range primitive**: `dst = buf + 512·i`, it reads absolute pages via `0x80028b0` from
`addr = 32·flash_ic[0] + flash_ic[2] + start/4096 + block`, where `flash_ic` = the descriptor at
**`0x08007c3c`** (`DAT_08000858`). In the from-entry boot that descriptor sits **inside the zeroed
`bss_seed` window** (the skipped nandboot chip-detect would fill it), so `flash_ic[0]=[2]=0` and the
read address collapses to **device pages 0..3**; the manager bitmap is then exactly **page-0 bytes
`[0:256]`** (2048 blocks / 8). A blank `0xFF` there marks *every* block bad → the readspare never
runs → `map_table_build` (`FUN_0802cbd8→FUN_0802ea54`) returns NULL → hang `0x0802d208`.

**Fix (landed, product):** `build_zc3201_nand_image` lays a zeroed BBT across **block-0 pages 0..3**
(`nand_image_zc3201.BBT_PAGES`). With it the *real* bad-block check returns "good" and the readspare
runs (Proven: `readspare_leaf` `0x08030224` 50144× with the real check, vs 0 before). The nandboot
scratch allocator `0x8000a14` (used by `func_0x080006fc`) returns a correctly-zeroed high-heap buffer
(`0x081dd000`; its fixed-slot pool descriptor at `~0x80074e0` is bss-zeroed → falls through to the
`0x081dd060+` heap search), so the only missing ingredient was the on-media BBT.

### Candidate #2 — CORRECTED: the readspare reads its tag from window[0] = **page data[0:4]**, not OOB

With the BBT fixed, the map scan runs `FUN_0802edb8` 960× but still fails: it never matches a
`0x1256` tag. Traced the readspare end-to-end (`FUN_08030224` `0x08030224` → `func_0x08002bac`
`0x08002bac`): the low-level read issues a normal small-page read (micro-ops `0x64/0x62/0x119`, our
`NfcController` deposits into the window), waits ready (`0x80030f8`, NFC `+0x158` bit31), **strobes
GPIO-out bit 3** (`0x080030d8` → `0x8006954` sets `*0x04000080` bit 3; fires 56324×), then reads the
4-byte tag from **`*0x08005800`** (`ldr r0,[sl]; str r0,[r7]`, `sl = DAT_08002508 = 0x08005800` =
the NAND SRAM staging window head) — i.e. **window[0]**. `func_0x08002bac` **returns 0 (success)**
every time (no poll timeout; ~3.7 ready-polls/read). So the mechanism works — the read is NOT a
distinct spare command (no `0x50`), and window[0] is **page data[0:4]**, the *same* path the
superblock read (`FUN_0800c208`) uses successfully (it read `reserve=64` correctly). **Therefore the
map-table `0x1256` tags must live in the mapped page's DATA[0:4], not in the 16-byte OOB** — a
correction to Leg 10's "OOB tag" model (`build_zc3201_nand_image` currently writes them via
`set_tag`/OOB; the firmware never reads OOB on this path). The GPIO-3 strobe is a hardware detail
(spare/ready latch), not a separate surface the emulator must model — window[0] already carries what
the firmware reads.

### The precise remaining wall (Leg 11 resume pointer) — candidate #3: the reserve-zone map format

The scan reads **reserve-zone pages** (observed row 127 = block 3 page 31, col 0) and gets
`0xFFFFFFFF` (blank) because the hand-built image lays nothing there. The MtdLib map-table is a
**logical→physical translation table stored in the reserve zone** (blocks 0..63), as tagged pages
whose **data[0:4]** carries `0x1256_0000 | logical` (live), `0x12345678` (system/reserved), or
`0xFFFFFFFF` (blank); `FUN_0802edb8`'s classifier and `FUN_0802ea54`'s per-partition builder
(`local_48`/`local_68` bounds, `piVar6[10] + n·0x18` partition records) consume it. Next steps,
precise and now unblocked:

1. **RE the map-table on-media format** from `FUN_0802ea54` `0x0802ea54` + `FUN_0802e5e0`
   `0x0802e5e0` + the readspare's physical-page computation in `FUN_08030224` (`page = 32·param_3
   + divmod(dev[0x18], col)`), and lay it into `build_zc3201_nand_image`: put the `0x1256|logical`
   tags in page **data[0:4]** (not OOB), in the reserve/map zone the scan actually reads. The
   fastest disambiguation is still to capture ONE producer-formatted image (Legs 8/9 USB ring) and
   diff the reserve zone + a data page's first 4 bytes against this spec.
2. Then A:/B: mount → drive pump → standby → product/cover-OID tap → book → GME play (re-point
   `OidSensor`, add a ZC3201 `firmware.mt`-style debugger), and parametrise `tests/test_scripting.py`
   over both firmwares.

**State now:** the unmodified firmware boots `boot_task_main → app_init_main →
fs_storage_mount_init 0x0802d0e0`: map_read (reserve=64 ✓) → whole_disk_map_build →
map_table_build → **bad-block check passes (BBT)** → **readspare runs (960 scans × ~52 reads)** →
still fails to match a map tag because the reserve-zone map-table pages are absent (candidate #3).
MT unregressed. Product change this leg: the BBT in `nand_image_zc3201.py` only.

## Leg 12 — the map-table scan is SOLVED (OOB spare-surface + page-28 per-block tags); the paged map-table load is the new wall

Leg 11's candidate #2 ("data[0:4]") was itself the artifact of an incomplete emulator: the
readspare tag genuinely lives in the page's **OOB**, surfaced into the NAND window by a GPIO
strobe the emulator wasn't modelling. Modelling it authentically + laying the tags at the right
page cracked the whole map-table scan. All Proven (disasm + the firmware's own MtdLib printf
diagnostics, hooked at logf `0x08008a48` = `*(0x081d9ad8+0x10)`; probes
`scripts/zc3201_scan_struct.py`, `scripts/zc3201_oob_offset.py`, scratch traces).

### The spare-surface mechanism (candidate #2, corrected + fixed authentically)

The `MtdLib` readspare leaf `FUN_08030224` `0x08030224` (`dev+0x34`) → nandboot `func_0x08002bac`
`0x08002bac` does: a normal small-page read (cmd 0x00→0x30, 523-byte data op) → `memcpy` the 512
**main** bytes out → wait ready → **strobe GPIO output bit 3** (`0x080030d8` → `0x08006954` sets
`*0x04000080` bit 3) → `ldr [0x08005800]` (window head) = the 4-byte tag. So the tag is the page's
**OOB[0:4]**, presented at the window head by the strobe — *not* page data. Crucially the GPIO bit
is set **once** and **never cleared** (it latches high; ~11 000 later GPIO writes all keep it high),
so it is *not* an edge or level signal we can key on — a GPIO-level watch would fire the surface on
every unrelated GPIO write and clobber the window during plain data reads. So the model keys the
hardware effect on the **strobe leaf's PC** (`0x080030d8`), whose sole purpose is this spare surface
and which runs only in the readspare path:
* `NfcController.surface_spare()` copies the last small-page read's retained 16-B OOB to the window
  head (`tt_emu/peripherals/nand.py`; `_last_oob` kept per read);
* `FirmwareProfile.nand_spare_surface_strobe = 0x080030d8`; `boot.build_zc3201_machine` does
  `machine.on_code(strobe, nfc.surface_spare)`, small-page only, so **MT is byte-for-byte
  untouched**.

### The per-block map-table format (candidate #3, RE'd + laid down)

The scan reads a **per-block tag page**, not reserve-zone pages. For partition scan-index `B`,
the readspare computes `page = dev[0x14]·B + (param_4 ÷ dev[0x18])` with `param_4 = dev[0x14]−4 =
28`, `dev[0x18] = 1` → **`page = 32·B + 28`** (col 0), of **physical block `128 + 2·B`** (=
`planes·reserve + planes·B + plane`; plane 0 = even blocks from `2·reserve`, planes=2, reserve=64).
Its **OOB[0:4]** carries `0x12560000 | logical` (live), `0x12345678` (`DAT_0802f0a0` = a free spare
block → joins the free-block ring `FUN_0802e574`), or `0xFFFFFFFF` (blank → treated bad). Per plane
a partition spans `1024 − 64 = 960` usable blocks (`hi`), top **9** free spares → `lo = 951` valid.
`nand_image_zc3201.build_zc3201_nand_image` now lays these at page 28's OOB for both planes
(`_lay_map_tags`, `_phys_block`); FAT volumes are placed on the mapped physical blocks.

**Result (verified against the firmware's own diagnostics):** the map-table scan is **fully
consistent** — `FUN_0802edb8` classifies 951 valid + 9 free with **zero** bad; the consistency
gate `mtd_helper_fe2c` (`0x08037e2c`) returns 1 with `iVar9=951 (valid)`, `iVar11=0 (bad)`,
`rec+6=0`, `rec+6+iVar9 == lo`. The readspare storm **collapses 50144 → 960** reads (one clean read
per block). The MtdLib initialises the partition (`MtdLib - NandPart:…,BCnt=1984,PCnt=2,…`) and the
FatLib version prints.

### Leg 12 resume pointer — the paged map-table load (`mtd_MapTblInit`)

The mount now fails one level deeper: `FUN_0802ea54`'s per-partition builder `FUN_0802e5e0` returns
**false** because **`mtd_MapTblInit` `0x08035318` returns 0** (→ `map_table_build FUN_0802cbd8`
NULL → the same hang `0x0802d208`). Root cause is Proven: `mtd_MapTblInit` gates on
`record[+0x16]` (= `lo` = **951**); since `951 > 0x101` (257) it takes the **large-partition
paged-map branch**, which calls `mtd_helper_d244` `0x08035244` to pull a free block from the ring
and then **reads an on-NAND paged map-table** back from it via `dev+0x28` (`0x08030310`) +
`nandmtd_fn`. On the hand-built image the ring yields nothing / the paged map isn't present
("MtdLib15:0" logs), so `mtd_helper_d244` returns ≥ `hi` and `mtd_MapTblInit` bails (`return 0`).

**The firmware WRITES its own map table at mount — the wall is the small-page NAND WRITE
round-trip, not a missing on-media structure.** The `MtdLib31` diagnostic (capture with
`scripts/zc3201_mtdlog.py`) shows the large-partition branch **writing** the paged map to each free
spare block and **verifying** it by read-back:
`MtdLib31:Off:0,Wrt:0x10000,Read:0x12345678,P:0,F:951,Pg:0` → `MtdLib - MarkBadBlk:P:0,F:951`, for
every free block `F` (951..959), until the ring empties (`MtdLib15:0`) and `mtd_MapTblInit` returns
0. It writes `0x10000` but reads back **`0x12345678`** — the pre-placed free-spare **OOB** sentinel.
Traced (`scripts/…` scratch): the spare-surface strobe `0x080030d8` fires **repeatedly inside
`mtd_MapTblInit`** with `_last_oob = 0x12345678`, i.e. the verify's read-back is served the **stale
pre-placed OOB tag** instead of the `0x10000` just written. Root cause: the emulator's small-page
**write** path does not round-trip here — the firmware's map-table write (via `dev+0x30` spare-write
`0x080301b8` → nandboot `func_0x08002228`, and/or the data program) is **not committing to the
`NandImage` tag/data store**, so the readspare read-back surfaces the old sentinel. Next steps,
precise:

1. **Model the small-page NAND WRITE round-trip so `mtd_MapTblInit` can write+verify its map.**
   Disassemble the spare-write leaf `dev+0x30 = 0x080301b8` → nandboot `func_0x08002228` (the twin
   of the readspare `func_0x08002bac`, but programming a page/OOB), and confirm `NfcController`'s
   small-page program path (`_data_program`/`l2_strobe`, and any spare-only program) commits the
   written bytes to the same tag store `surface_spare` later reads. The firmware then builds its own
   paged map at mount — **no hand-built paged map needed** (the pre-placed `0x12345678` free
   sentinels are exactly the free pool it writes into). Verify with `zc3201_mtdlog.py`:
   `MtdLib31 Read` should become `0x10000` and `mtd_MapTblInit` return 1.
   **MT-oracle note:** MT's tt-emu image is read-only at runtime (its NFTL derives log2phy at init,
   `nftl_build_log2phy 0x08047510`, no stored paged map — correspondences.tsv: `mtd_MapTblInit` has
   *no confident MT twin*), so the small-page *write* path has never been exercised end-to-end;
   this is the first mount that writes NAND. This is the last storage wall before A:/B: mount.
2. Then A:/B: mount → drive pump → standby → product/cover-OID tap → book → GME play (re-point
   `OidSensor`, add a ZC3201 `firmware.mt`-style debugger), and parametrise `tests/test_scripting.py`
   over both firmwares.

**State now:** the unmodified firmware boots to `fs_storage_mount_init 0x0802d0e0` and runs the
**complete, consistent map-table scan** (BBT ✓, spare-surface ✓, 951 valid + 9 free, readspare
collapsed 50144→960); it then reaches `mtd_MapTblInit 0x08035318`, which **writes its own paged map
table** and fails only because the small-page NAND **write** round-trip is unmodelled (the verify
read-back surfaces the stale pre-placed OOB sentinel; `MtdLib31 Read:0x12345678` ≠ `Wrt:0x10000` →
`MarkBadBlk` → `MtdLib15:0` → `return 0`). MT unregressed (159 passed). Product changes this leg:
`FirmwareProfile.nand_spare_surface_strobe`, `NfcController.surface_spare` + `_last_oob`, the
`boot.build_zc3201_machine` strobe-PC wiring, and the page-28 per-block map-tag layout
(`_lay_map_tags`/`_phys_block`, live `0x12560000|logical` + free `0x12345678`) in
`nand_image_zc3201.py`. New tests: `test_smallpage_surface_spare_puts_oob_at_window_head`,
`test_build_zc3201_nand_image_map_tags`. Probes: `scripts/zc3201_mtdlog.py` (firmware's own MtdLib
diagnostics via logf `0x08008a48`), `scripts/zc3201_scan_struct.py`, `scripts/zc3201_oob_offset.py`,
`scripts/zc3201_scan_trace.py`.

## Leg 13 — the small-page WRITE round-trips, BOTH partitions MtdLib-mount; wall is now a FatLib divide-by-zero (disk-map `+0x2c` = 0)

Two independent fixes cracked the last two storage walls; the mount now clears the entire
**MtdLib** layer (both partitions `InitPlane succeed`) and dies one layer up, inside **FatLib**.
All Proven against the firmware's own diagnostics (`scripts/zc3201_mtdlog.py`) + register-level
probes (scratch `scripts/…` under the session scratchpad).

### Fix 1 — the small-page NAND WRITE round-trip (`NfcController.l2_strobe`, small-page)

`mtd_MapTblInit 0x08035318` writes its paged map table to a free spare and verifies it by
read-back (`nandmtd_fn 0x080350ac` → `dev+0x2c` readpage). The nandboot write leaf
`func_0x08002228` (via `dev+0x30` `FUN_080301b8`, and `dev+0x28` `FUN_08030310` which *also*
writes) streams a page as a **window FIFO**: memcpy the 512 main bytes into the window
(`0x08005800`), poll the drain (`L2 BUF_STATUS`), then push a **single 4-byte spare word** (the OOB
map tag, `DAT_080351e0 = 0x12345678`) to the **window head** — the same FIFO port the 512 main just
drained through — then flush buffer 4. The emulator read `window[0:512]` at flush, so the spare word
clobbered `main[0:4]` (`MtdLib31 Read:0x12345678 ≠ Wrt:0x10000`) and the OOB (`window[512:528]`) was
never written. Fix: take the main data from the **poll-captured `_prog_staged`** (captured before the
spare clobber, exactly like the large-page capture-at-poll) and read the 4-byte OOB tag from the
window head. `SMALLPAGE_OOB_TAG = 4`. With this the verify passes and `mtd_MapTblInit` returns 1.
(The pre-placed `0x12345678` free-spare tags are the pool it writes into — no hand-built paged map
needed, as predicted.)

### Fix 2 — the two-partition physical-block map (`nand_image_zc3201._phys_block`, partition-aware)

With the write fixed, plane 0 built its map but plane 1 scanned every block blank. The plane is a
pure **even/odd block interleave of one chip** (probed: the readspare `plane` arg is always 0, the GO
word `0x0800209c` always sets plane bit 10 — `0x40000200 | (1<<(plane+10))` — there is no second
die/chip-select). The firmware's own scan (`FUN_0802edb8` → readspare `page = 32·blk + 28`) reads,
per **partition** (`FUN_0802e5e0` param_2, not a hardware plane):

* **partition 0 (A:)**: logical `L` at physical block **`128 + 2·L`** (even, reserve 64 withheld);
  `hi = 960`, `lo = 951` valid (+9 free spares).
* **partition 1 (B:)**: logical `L` at physical block **`1 + 2·L`** (odd, *no* reserve);
  `hi = 1024`, `lo = 1015` valid (+9 free spares).

(`hi`/`lo` read live from `arg2[0x14]`/`[0x16]`.) `_phys_block`/`_lay_map_tags`/`_place_volume` are
now partition-aware; volumes are clamped to `lo`. Result: **`InitPlane succeed: P=0,V=951` and
`P=1,V=1015`** — the full MtdLib mount, both partitions, is consistent. A:'s FAT16 boot sector reads
back correctly at logical 0 (`row 4096`, `eb 3c 90 "TEMU1.0" … 512 B/sector`).

### Leg 14 resume pointer — FatLib disk-map `+0x2c = 0` → divide-by-zero abort

Past the MtdLib mount, `fs_storage_mount_init` builds the FatLib volumes and immediately aborts.
The abort is the **ARM semihosting SWI** `svc 0x123456` (the ARM-state semihosting number; the
emulator only special-cases the Thumb `0xAB`, so it logs "ignored" and wrongly falls through — the
downstream `0x034d034c` deref in the `FUN_080b193c` cleanup is just the fallout of not honouring the
abort). `FUN_080c9b20` issues it with **R0 = 0x18 = SYS_EXIT** — a fatal abort, *not* a catchable
throw (it returns cleanly). Origin (Proven): `FUN_0800cec4` (the FatLib disk read) →
`FUN_0800c974` (global-block → (partition, block) translation) hits a **compiler divide-by-zero
guard**: it divides by `param_1[0xb]` (**disk-map object `0x80fa420`, offset `0x2c`**) which is
**0** (also `[0x24] = 0`). So the whole-disk map object's geometry field at `+0x2c` was never
populated during mount.

**Next steps, precise:**
1. Find what sets disk-map `+0x2c` (the divisor in `FUN_0800c974`'s block translation) and why it is
   0 on our image — start at the disk-map constructor `FUN_0802f0c0` and `mtd_helper59 0x0802d408`
   (installs the dev vtable + `+0x2c` = readpage `0x0800c208`; the *disk-map* `+0x2c` is a different
   struct — the whole-disk object built over the two partitions). It is almost certainly a
   superblock-derived geometry (sectors/cluster, or total logical blocks) that our
   `_superblock_payload` leaves 0. Capture it live: hook `FUN_0800c974 0x0800c974`, dump `param_1`
   (`0x80fa420`) fields `[0..0x30]`, and back-trace which mount function should store `+0x2c`.
2. Optionally make the emulator honour ARM-state semihosting `svc 0x123456` (at minimum SYS_EXIT
   0x18 → stop with a clear "firmware SYS_EXIT/abort" reason instead of "ignored" + fall-through), so
   future FatLib aborts surface cleanly rather than as a garbage deref. Keep it generation-agnostic
   (MT never hits it during boot — 158 green).
3. Then the FAT mount should complete → confirm the firmware opens the test `.gme` on `B:/`, then
   drive pump → standby → OID tap → book → GME play, mirroring the MT `firmware.mt` debugger path
   via the FirmwareProfile (correspondences.tsv), and parametrise `tests/test_scripting.py` over both
   firmwares.

**State now:** the unmodified firmware boots through the **complete MtdLib mount** — both partitions
`InitPlane succeed` (A: 951 valid, B: 1015 valid, 0 bad), the small-page write round-trips, A:'s FAT16
boot sector reads back — and dies in **FatLib** at a disk-map divide-by-zero (`disk_map[+0x2c] = 0`,
surfaced as semihosting `SYS_EXIT`). MT unregressed (158 passed). Product changes this leg:
`NfcController.l2_strobe` small-page main-from-`_prog_staged` + `SMALLPAGE_OOB_TAG`; partition-aware
`_phys_block`/`_partition_geom`/`_lay_map_tags`/`_place_volume` + volume clamp in
`nand_image_zc3201.py`. Tests updated: `test_smallpage_nfc_program_round_trip` (real FIFO protocol),
`test_build_zc3201_nand_image_map_tags` (both partitions' geometry).
