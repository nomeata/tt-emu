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

### Leg 14 resume pointer — a page read overruns/overlaps the MtdLib manager → divide-by-zero abort

Past the MtdLib mount, `fs_storage_mount_init` builds the FatLib volumes and immediately aborts.
The abort is the **ARM semihosting SWI** `svc 0x123456` (the ARM-state semihosting number; the
emulator only special-cases the Thumb `0xAB`, so it logs "ignored" and wrongly falls through — the
downstream `0x034d034c` deref in the `FUN_080b193c` cleanup is just the fallout of not honouring the
abort). `FUN_080c9b20` issues it with **R0 = 0x18 = SYS_EXIT** — a fatal abort, *not* a catchable
throw (it returns cleanly). Origin (Proven): `FUN_0800cec4` (FatLib disk read) → `FUN_0800c974`
(global-block → (partition, block) translation) hits a **compiler divide-by-zero guard** dividing by
`param_1[0xb]` = the **MtdLib manager object `0x80fa420`, offset `0x2c`** (= its pages-per-block, and
its first-call value is the correct **`0x20` = 32**, matching `dev+0x14`).

**The manager gets clobbered to 0.** Watching `[0x80fa44c]` (= manager `+0x2c`): it is initialised to
`0x20` (by `0x0802eb78`), then **overwritten by a 512-byte page read** — `func_0x080028b0`
(readpage, `lr=0x8002a08`) memcpys the NAND window `0x08005800` into a **buffer at `0x80fa400`**,
whose 512-byte span (`0x80fa400..0x80fa600`) **overlaps the manager at `0x80fa420`** (buffer + 0x20).
The read page's byte at offset **0x4c** lands on the manager's `+0x2c`; the *first* such read carries
`0x20` there (harmless), but the read just before the abort carries **0** → the next `FUN_0800c974`
divides by 0 → SYS_EXIT. So the real bug is a **heap collision**: a FatLib sector buffer
(`0x80fa400`) and the live MtdLib manager (`0x80fa420`) occupy overlapping heap — a stale/reused
allocation (likely use-after-free of the manager, or the emulator's heap layout differs from
hardware because some skipped-init heap bound/seed is missing).

**Next steps, precise:**
1. Find why the FatLib sector buffer `0x80fa400` and the MtdLib manager `0x80fa420` overlap. Hook the
   allocator (the `puVar1[0]`/`DAT_…[0]` malloc slot used across these libs — e.g. `FUN_0802f0c0`
   allocates `0x60` via `(*(code*)*puVar1)(0x60,…)`) and log every alloc/free of the region around
   `0x80fa400`: is the manager freed before the FatLib buffer is allocated (use-after-free), or does
   the heap arena simply run them into each other? Compare the arena base/limit the firmware uses
   against what our from-entry boot leaves — the C-runtime/heap-init the ZC3201 boot skips may leave
   a heap-bounds global at 0 so `malloc` hands out overlapping/low addresses (analogous to the
   `bss_seed`/geometry seeds we already add in `build_zc3201_machine`). Seed it if so.
2. Optionally make the emulator honour ARM-state semihosting `svc 0x123456` (at least SYS_EXIT
   `0x18` → stop with a clear "firmware SYS_EXIT/abort" reason instead of "ignored" + fall-through),
   generation-agnostic (MT never hits it during boot — 158 green). This turns future FatLib aborts
   into clean stops instead of garbage derefs.
3. Then the FAT mount should complete → confirm the firmware opens the test `.gme` on `B:/`, then
   drive pump → standby → OID tap → book → GME play, mirroring the MT `firmware.mt` debugger path
   via the FirmwareProfile (correspondences.tsv), and parametrise `tests/test_scripting.py` over both
   firmwares.

Probes for this leg live in the session scratchpad (`probe_mtdlog`/`probe_seq`/`probe_watch`/
`probe_mcpy` patterns): hook `FUN_0800c974 0x0800c974` (divisor object), watch `[manager+0x2c]`
writes, and trace the readpage memcpy (`0x0800339c`, `lr=0x8002a08`) dst/src to see the overlap.

**State now:** the unmodified firmware boots through the **complete MtdLib mount** — both partitions
`InitPlane succeed` (A: 951 valid, B: 1015 valid, 0 bad), the small-page write round-trips, A:'s FAT16
boot sector reads back — and dies in **FatLib** at a divide-by-zero: a 512-byte page read into buffer
`0x80fa400` overlaps the live MtdLib manager `0x80fa420`, zeroing its `+0x2c` pages-per-block divisor
(heap collision; surfaced as semihosting `SYS_EXIT`). MT unregressed (158 passed). Product changes this leg:
`NfcController.l2_strobe` small-page main-from-`_prog_staged` + `SMALLPAGE_OOB_TAG`; partition-aware
`_phys_block`/`_partition_geom`/`_lay_map_tags`/`_place_volume` + volume clamp in
`nand_image_zc3201.py`. Tests updated: `test_smallpage_nfc_program_round_trip` (real FIFO protocol),
`test_build_zc3201_nand_image_map_tags` (both partitions' geometry).

## Leg 15 — the FatLib "heap collision" was a NAND read-count overrun; the FatLib mount COMPLETES; new wall = nandboot `codepage` file-index lookup

Leg 14's "heap collision" hypothesis (a use-after-free / missing heap bound handing out
overlapping allocations) was **wrong** — the allocator is fine. Instrumenting it
(`scripts/zc3201_heap_probe.py`: the services vtable `0x081d9ad8` slot 0 = `malloc`
`0x080a0a88`, slot 1 = `free`, slot 4 = `logf`) shows a plain **upward bump allocator**
handing out *contiguous, non-overlapping* blocks: the whole-disk-map object
(`FUN_0802f0c0`, `malloc(0x60)` = `0x80fa3c0`) then the MtdLib manager (`FUN_0802ea54`,
`malloc(0x28c)` = `0x80fa420`) — adjacent, no overlap. The real bug is a **read that
overruns its buffer** (all Proven, disasm + live-run instrumented):

* **The clobber is a memcpy** `0x0800339c` (`src = 0x08005800` the L2 NAND window, `n =
  0x200`, `dst = 0x80fa400`) fired from the nandboot bulk page-read `func_0x080028b0`
  (`lr = 0x08002a08`). The read primitive assembles a page as `dst = buf + i·512` for
  `i in range(count)`.
* **The buffer is `0x80fa000`** — the map-read scratch the mount `fs_storage_mount_init`
  `0x0802d0e0` allocates as `iVar5 = FUN_0802b350(dev+0x1c)` (**512 bytes**, `dev+0x1c` =
  page bytes), stashes in the disk-info global `*(0x081d97d8+8)`, reads page 31 into via
  `dev+0x2c` = `FUN_0800c208`, then **frees** — after which the whole-disk-map (`0x80fa3c0`)
  and manager (`0x80fa420`) are bump-allocated *just above it*.
* **`count = *(0x08006fa0)[1] = 4`.** The bulk reader's sub-page count is byte `+1` of a
  **nandboot NAND-geometry descriptor** at `0x08006fa0` (read via the literal at
  `0x080024ec`). The nandboot image ships the **large-page default 4** (MT's 2-KiB page =
  4×512-B sub-pages). So the map-read reads **4×512 = 2 KiB into the 512-B buffer**:
  `buf+2·512 = 0x80fa400` lands on the manager's `+0x2c` pages-per-block divisor
  (`0x80fa44c`), zeroing it → the compiler divide-by-zero guard in `FUN_0800c974` (FatLib
  global-block → (partition, block)) fires the ARM-state semihosting **`svc 0x123456`
  `SYS_EXIT`**.

**Root cause = a skipped-chip-detect seed, exactly the `bss_seed`/`nand_dev_geometry`/
`NAND_ROW_CYCLES` class.** ZC3201's 512-byte-page K9F5608 reads **1** sub-page per page;
the skipped nandboot chip-detect never corrects the image's large-page default (4). The
byte `+0 = 2` in that descriptor is already correct in the image (every earlier read
worked) and is kept.

### Fix (landed, product)

* **`FirmwareProfile.nandboot_geom_seed = (0x08006fa0, bytes([2, 1, 0, 0]))`** — byte `+1`
  = 1 (512-B sub-pages per page). `boot.build_zc3201_machine` writes it to the nandboot
  **alias** (the read literal targets `0x08006fa0`) *and* the load copy (`0x07ffefa0`),
  mirroring MT's `NAND_ROW_CYCLES`. MT has no such seed (its real boot populates it).
* **`machine._on_intr` now honours ARM-state semihosting `svc 0x123456`** (Thumb is
  `0xAB`): `SYS_EXIT` (`r0 = 0x18`) becomes a clean `request_stop("firmware SYS_EXIT …")`
  instead of "ignored" + a garbage fall-through deref. Generation-agnostic (MT never
  issues it — 158 green).

**Verified** (`scripts/zc3201_mtdlog.py`): `FUN_0800c974`'s divisor stays `0x20` (32 clean
divides, 0 zero); the **complete MtdLib mount** (`InitPlane succeed P=0,V=951` /
`P=1,V=1015`) **and the FatLib mount** now finish; the boot proceeds into `app_init`'s
post-mount sequence and opens `A:` (system partition) 3×.

### Leg 15 resume pointer — the nandboot `codepage` file-index lookup

Past the FatLib mount, `app_init` runs a nandboot **boot-file loader** `0x08000868`
(callers `0x08000b3c` / `0x08000df4`) that **strcmp-searches an on-media file-index table**
— 8 records of `0x24` bytes at `0x08007c52`, name at `record+0x14` — for the name **`codepage`**
(`r8`). In the hand-built image **every record's name is empty**, so `codepage` is not found
and the loader hits a **fatal self-spin `b .` at `0x08000944`** (Proven: 20M instructions all
at that PC, `0` IRQs delivered, `state_init_power_on` never reached — the statechart never
starts). The table is **not** the FAT directory (placing a `codepage` file in `a_files`
does *not* populate it — verified): it is an on-media file-index the producer writes. The
`0x080032d8` "log" the panic calls first is a `bx lr` stub, so the spin is a bare halt.

**Next steps, precise:**
1. **Populate the system-partition file-index** the mount reads into `0x08007c52` (the 8
   `0x24`-byte records, incl. `codepage`) so nandboot finds and loads the codepage. Trace
   who fills `0x08007c52` (the caller `0x08000df4` / the mount's directory read) to recover
   the on-media format, and lay it into `build_zc3201_nand_image` — or capture it from a
   producer-formatted system image. (`codepage.bin` is `Firmware.codepage`, 0xD6CCC bytes.)
2. Then `app_init` reaches the **event pump** → statechart **INIT leaf**
   (`state_init_power_on` `0x08038e48`) → standby → book. Re-point `OidSensor`, add a ZC3201
   `firmware.mt`-style debugger (statechart/AO/event-ring, via `FirmwareProfile` +
   `tt-firmware-reveng/correspondences.tsv`), drive the pump → OID tap → GME play, and
   parametrise `tests/test_scripting.py` over both firmwares.

**State now:** the unmodified firmware boots through the **complete MtdLib + FatLib mount**
of both partitions and into `app_init`'s post-mount file loading; it halts at the nandboot
`codepage` file-index lookup (`0x08000868` → spin `0x08000944`) because the hand-built
image's on-media file-index is empty — the first point that needs authentic **system-partition
content**. MT unregressed (158 passed). Product changes this leg:
`FirmwareProfile.nandboot_geom_seed` (+ ZC3201 value), the `boot.build_zc3201_machine`
seed wiring, and the `machine._on_intr` ARM `SYS_EXIT` clean-stop. Tests:
`test_profile_load_layout_distinct` (the seed field) + `test_zc3201_fatlib_mount_completes`
(divisor never zero, boot past the mount into the post-mount lookup, no `SYS_EXIT`). Probe:
`scripts/zc3201_heap_probe.py`.

## Leg 16 — the nandboot system-bin index is cracked; `app_init` reaches the statechart INIT leaf + the event pump

The Leg-15 wall (`codepage` boot-file lookup → fatal spin `0x08000944`) is **passed**.
The unmodified firmware now loads the `codepage` bin through the nandboot boot-file
loader and runs on into the statechart INIT leaf (`state_init_power_on` `0x08038e48`,
reached once) **and the OID/GME event dispatch** (`gme_oid_dispatch` `0x0803629c`,
dispatched repeatedly) — the event pump is live.

### The nandboot boot-file loader, fully RE'd (all Proven, disasm + live-instrumented)

`app_init` loads three named system bins via `FUN_0x08000dcc` → the loader
`FUN_0x08000868`:

* callers (in PROG): `0x0802ced8` **`codepage`** (`max=8` blocks), `0x0802cf78`
  **`font_lib`** (`max=0x14`), `0x0802d030` **`ImageRes`** (`max=0x32`). Only
  `codepage` is requested before the pump; the codepage-load function `0x0802ce94`'s
  caller (`0x0802b870`) **ignores its result**, so a load failure is non-fatal — but a
  **not-found `strcmp` miss is a fatal `b .` spin** at `0x08000944`, so every requested
  name needs an index record.
* **on-media index** (read *raw* via the nandboot bulk reader `func_0x080028b0`, same
  addressing as `_place_page`): **block 0, page 30 = header** (`+0x04` record count,
  `+0x08` bin-region start block, `+0x0c` block span); **page 29 = records**, one
  `0x24`-byte entry per bin: `+0x00` = size in **512-B sectors** (exactly MT's
  `_bin_entries_payload`), `+0x08` = the **abs-page of that bin's block map**, `+0x14` =
  NUL-terminated name. This is the small-page twin of MT's `build_nand_image` bin index
  (rows 253/254 there); it is **not** the FAT directory.
* **block map** (at `record+0x08`): `{u16 origin, u16 backup}` per logical block;
  `FUN_0x08000cd0` reads content page `origin·32 + (pageidx & 31)`.
* **shift globals** `0x080075a2..a4` (nandboot descriptor `0x080070e0 + 0x4c2..4c4`):
  `log2(page)=9`, `log2(pages/block)=5`, plane factor `dev+0xc=2`. The nandboot init
  `FUN_0x08001160` derives these from the device geometry, but that init is **skipped**
  from-entry, so they read **0** and the loader's per-page math collapses — the same
  skipped-seed class as `nandboot_geom_seed`.

### Fix (landed, product)

* **`FirmwareProfile.nandboot_shift_seed = (0x080075a2, bytes([9,5,2]))`** (+ MT `None`);
  `boot.build_zc3201_machine` writes it after loading nandboot (in the `bss_seed`
  window, so after that zero).
* **`build_zc3201_nand_image` lays the system-bin index** (`_lay_system_bin_index`):
  header p30, records p29, one block map per bin in block 0's free pages, and the
  `codepage` content on the **A: reserve even blocks `[2,128)`** (which the MtdLib map
  never assigns, so no FAT collision). The superblock now sits at page **31 only**
  (pages 29/30 are the index; the mount only needs 31 — verified).

**Verified** (`test_zc3201_codepage_index_reaches_statechart`): no not-found spin;
`state_init_power_on` reached; `gme_oid_dispatch` dispatched. MT unregressed.

### OPEN — the size-unit / block-unit contradiction (record `codepage`)

`codepage` is `0xd6ccc` B = **54** 16-KiB blocks, but its load site passes **`max=8`**
blocks (`0x0802ced8`, `mov r2,#8`); the caller's `W1:` guard fails a bin whose
`size >> log2(page·pages_per_block=16384)` exceeds `max`. So the record size **cannot**
be in bytes (`54 > 8` → `W1` → return 0). Using **sectors** (`len//512`, MT's format)
makes `W1` pass (`1719 >> 14 = 0`), which is what lets the boot proceed — **but** with
the authentic shift `[0x4c2]=9` the content walk `FUN_0x08000cd0` then reads only
`1+(1719-1)>>9 = 4` pages, i.e. the codepage load is **partial**. The two consumers want
different units under the same geometry: `W1` wants ~128-KiB blocks (`max=8` ⇒ codepage
≤ 8 blocks), the content walk wants bytes. This points at either (a) the nandboot loader
expecting a **128-KiB block unit** (`dev+0x14`=256) distinct from MtdLib's 16-KiB view —
needs the dev-geometry question resolved — or (b) ZC3201's real nandboot codepage being a
**smaller file** than `Firmware.codepage` (the container's `+0x38/+0x3c` system-bin table
declares only `PROG`+`codepage`, both at the full sizes; `font_lib`/`ImageRes` are **not**
in the container — their content source is unknown, likely the producer format). Reaching
the pump does not need the full codepage; **book-mode rendering / UTF-16 path conversion
likely will** (cf. MT's "garbled codepage" symptom), so this is the first thing to nail in
the next leg if book mode misbehaves.

### Leg 16 resume pointer — drive the pump → standby → book → GME play

The unmodified firmware boots through the complete mount **and** the codepage boot-file
load into `state_init_power_on` `0x08038e48` and the live event pump (`gme_oid_dispatch`
`0x0803629c`). **Standby was not yet observed** at the corresponded addresses
(`state_stdb_standby` ZC `0x0803e454`; the real standby SM is `FUN_08036f7c` per
`correspondences.tsv`) — the pump runs but the statechart did not visibly transition
INIT → standby in a 300 M-insn window; the terminal PC parks in a nandboot leaf
(`0x08000018`), likely a NAND/audio read in the pump.

**Next steps, precise:**
1. **Confirm/instrument standby.** Build a ZC3201 `firmware.mt`-style debugger (AO
   struct + event ring + statechart leaf) via `FirmwareProfile` symbols +
   `correspondences.tsv` (`state_init_power_on` ZC `0x08038e48`, `state_stdb_standby`
   ZC `0x0803e454`/`0x08036f7c`, `gme_oid_dispatch` `0x0803629c`). Find why INIT→standby
   does/does not fire (does the pump need a timer/OID event injected, as MT's driver
   does?). **Lean on MT as the direct template** — the pump/OID/audio are shared.
2. **OID tap → book mode → GME play.** Mirror how tt-emu drives MT from standby: the pump
   loop, the OID-tap event injection (`BootedMachine.tap`/`OidSensor`), book mode, the GME
   interpreter on a tapped OID. Re-point addresses via the profile; the `.gme` is on B:.
3. If book-mode rendering breaks, **resolve the codepage size-unit/geometry contradiction**
   above (and add `font_lib`/`ImageRes` records — a not-found on either spins).
4. **Parametrise `tests/test_scripting.py`** (+ the other gme-based tests) over both
   firmwares; core GME-interpreter/OID-tap/play tests must pass on both.

**State now:** the unmodified firmware boots through the complete MtdLib + FatLib mount of
both partitions, loads the `codepage` system bin via the nandboot boot-file loader, and
reaches the statechart INIT leaf `state_init_power_on` `0x08038e48` and the live event pump
(`gme_oid_dispatch` `0x0803629c`). Product changes this leg:
`FirmwareProfile.nandboot_shift_seed` (+ ZC3201 value), the `boot.build_zc3201_machine`
seed wiring, and `build_zc3201_nand_image`'s `_lay_system_bin_index` (superblock moved to
page 31 only). Tests: `test_profile_load_layout_distinct` (the new seed field) +
`test_zc3201_codepage_index_reaches_statechart` (no spin; INIT leaf + pump reached). The
open blocker is the codepage size-unit/block-unit contradiction (does not block the pump;
may block book-mode rendering).

## Leg 17 — the statechart's periodic driver was dead (timer-status coupling); INIT → standby now fires; new wall = the pen powers off

Leg 16's "the event pump is live (`gme_oid_dispatch` `0x0803629c` dispatched)" was a
**misattribution**: `0x0803629c` is a basic block *inside* the MtdLib map-table paged-lookup
helper `FUN_08036204` (it calls `mtd_helper_d244`), which runs 951× **at mount** — so the
Leg-16 test's "dispatch ≥ 1" was satisfied by the mount, not by any event pump. Instrumenting
the *real* pump (`sm_dispatch_hierarchy` `0x080096a8`) showed it ran only **twice** in 300 M
insns; the statechart never left INIT. All findings below are Proven (disasm + live-run
instrumented; probes `scripts/zc3201_statechart_probe.py` + session scratchpad traces).

### The stall — the HAL software-timer tick never ran

* **The pump is a QF event ring drained by nandboot `FUN 0x08003a84`** (a tight
  `while(true){ sm_dispatch_hierarchy(); drain ring[head..tail] }`): a 32-entry ring of
  12-byte records at the fixed HAL scheduler object `0x080075cc`, `head` u16 `+0x180`,
  `tail` `+0x182`, both masked `& 0x1f` (confirms Leg 2). The **INIT leaf arms a 100 ms HAL
  software timer** (`state_init_power_on` → nandboot `func_0x08006b64(0, 100000, 1, cb=0x080065ec)`);
  the callback `0x080065ec` posts event **`0x30` (sw-timer tick)** via the ring-post
  `0x08003c44`. That periodic tick is what drives INIT → standby.
* **The 100 ms callback never fired** (0 hits) because the **HAL software-timer tick
  `0x08006d38` never ran** (0 hits) — even though the hardware timer IRQ fired 5722×. Root
  cause, register-exact: the ZC3201 timer ISR `0x08003d6c` acks in **two steps** — it first
  clears the **top-level line-10 status** at `0x040000cc` (writing `[0xCC] & ~0x400`, i.e. 0),
  then reads the **second-level timer-fired bit** at `0x0400004c` bit17 (`0x20000`) that gates
  the software-timer tick. But `tt_emu.peripherals.intc.IntcTimer` drove **both** `0xCC` bit10
  and `0x4C` bit17 from the single `_timer_latched`, and its `0xCC`-write-0 path cleared
  `_timer_latched` — so by the time the ISR read `0x4C`, bit17 was already 0 → the tick was
  skipped every IRQ (live: `[0xCC]=0x400`, `[0x4C]=0x12`, bit17 absent). MT is unaffected: its
  ISR acks via the `TIMER1_CTRL` bit28 path and its teardown writes 0 to `0xCC` *expecting* the
  latch to drop.

### Fix (landed, product; MT byte-identical via a flag)

* **`IntcTimer(zc3201_timer_ack=True)`** decouples the two latches: the `0xCC`-write-0 no longer
  clears `_timer_latched`; instead the ISR's ack **write to `0x4C` with bit17 cleared** drops it.
  `build_zc3201_machine` passes the flag; MT constructs `IntcTimer(gpio)` unchanged (flag off ⇒
  the write paths are bit-for-bit as before). This is authentic hardware modelling — the ZC3201
  top-level line 10 is a computed *level* of the second-level timer source, so clearing the
  top-level status must not clear the source latch.

**Verified** (`test_zc3201_statechart_advances_init_to_standby`, `tt_emu/firmware/zc3201.py`
`Zc3201Debugger`): the software timer now ticks (1674× / 40 M), the 100 ms callback fires, the
pump drains the tick events, and the statechart advances **`state_init_power_on` `0x08038e48`
(≈6 M) → the standby state machine `FUN_0803ef7c` `0x0803ef7c` (≈15.6 M)** — the twin of MT's
`standby_handler` (`correspondences.tsv`). MT unregressed.

### New infrastructure

* **`tt_emu/firmware/zc3201.py`** — the ZC3201 twin of `firmware/mt.py`: `recognize`, the event
  ring readers (`ring_state`/`ring_events` at `0x080075cc+0x180`), and `Zc3201Debugger` with
  read-only PC watches on the statechart leaves (`STATE_HANDLERS`: INIT `0x08038e48`, standby
  `0x0803ef7c`, `SetRefresh` `0x0803e454`), `sm_dispatch_hierarchy`, and the software-timer
  tick. Hook-free observation, profile-recognized like the MT debugger.

### Leg 17 resume pointer — the pen enters the power-off path instead of book

Past standby, the standby SM `FUN_0803ef7c` receives event **`0x1015`** and takes its
**app-mode-byte `+0x1b == 8`** branch (set by `FUN_0803e384`, which also OR-sets
`*DAT_0803e448 |= 0x200000`), an **infinite power-off / shutdown loop**: it bit-bangs GPIO
outputs 6 and 1 low, `func_0x08006e28(0x14)` delay, and polls **GPIO input pin 0**
(`func_0x08006978(0)` = `[0x040000bc]` bit0) for release, 50× then `wfi` — i.e. the pen has
**decided to power off**. This is the authentic behaviour of a real pen that powers on with no
resume condition held; MT avoids it because the GPIO model presents the **power button held**
through the app-init sample (`nand-image-layout.md` §7.3.1a, GpioBlock `_boot_power_button` /
GPIO11+GPIO15). `tt_emu.peripherals.gpio.GpioBlock` is **MT-specific** (GPIO11 button / GPIO15
power-hold at MT's offsets); ZC3201's power-hold/OID GPIO wiring differs and is not yet modelled.

**Next steps, precise:**
1. **Find why the pen chooses power-off (mode 8) instead of descending to book.** Event `0x1015`
   is the correct INIT→**standby** transition (`FUN_0803b480` maps `0x1015` → target state 3 =
   standby); the power-off happens *inside* standby because **app-mode byte `+0x1b` was already 8
   on standby entry** (`FUN_0803ef7c` only takes the mode-8 spin when `+0x1b == 8`). Two coupled
   clues to chase: **(a)** the INIT leaf `state_init_power_on` **bailed on its precondition** —
   `FUN_08008a04` (a pointer-range check `addr ∈ [0x08000000,0x08200000)`) returned 0, so INIT
   skipped its real work (no `play_chomp_voice(0x19)`, mode never set to 2) and returned via
   `FUN_080a086c(2000,10)`; the checked pointer is the `FUN_0802b350(4)` allocation stored at
   `*(DAT_08039094+8)` — find why it is out of range (allocator/seed gap). **(b)** `FUN_0803e384`
   sets mode 8 + `*DAT_0803e448 |= 0x200000` (the power-off enter); trace its caller and the
   predicate that routes there vs the book descent — it is the ZC3201 analogue of MT's `+0x24`
   resume latch (recover the exact GPIO pin/register ZC3201 samples for "power button still held"
   and present it — a ZC3201 GpioBlock variant or a profile-driven boot-held pin). The app object
   is `DAT_0803eefc`/`DAT_0802b9b8`.
2. Then re-point the **OID sensor** (the ZC3201 capture-state address + GPIO wiring — MT's
   `bit_count 0x08008c09` equivalent) and the **audio DAC/DMA** so a product/cover-OID tap
   mounts the `.gme` on B: and the shared GME interpreter plays (twins in `symbols` /
   `correspondences.tsv`: `game_play_oid_voice 0x0805c730`, `gme_exec_command 0x0804c6e4`, …).
3. Then parametrise `tests/test_scripting.py` (+ the other gme-based tests) over both firmwares.

**State now:** the unmodified firmware boots through the complete MtdLib + FatLib mount, loads
the `codepage` system bin, reaches the statechart INIT leaf, and — with the timer-status
decouple — the HAL software timer now ticks and drives the statechart **INIT → standby state
machine** (`FUN_0803ef7c`). It then enters the authentic **power-off path** (mode 8) because the
ZC3201 power-hold GPIO condition is not yet presented. MT unregressed. Product changes this leg:
`IntcTimer.zc3201_timer_ack` (+ the two-latch decouple) and the `build_zc3201_machine` wiring;
new `tt_emu/firmware/zc3201.py` (statechart debugger). Tests:
`test_zc3201_statechart_advances_init_to_standby`. Probe: `scripts/zc3201_statechart_probe.py`.

## Leg 18 — the pen does NOT power off (Leg 17 premise corrected); standby descends past a GPIO-pin0 wait; new wall = the audio DAC completion

Leg 17's resume pointer said the pen entered the mode-8 **power-off** path (INIT bailed on
`FUN_08008a04`, `+0x1b` stuck at 8, `FUN_0803e384` fires). **Re-measured at HEAD this is not
what happens** (probe `scripts/zc3201_init_precond.py`, live-instrumented):

* `state_init_power_on` `0x08038e48` runs its **real work** — the movable-block allocator
  `FUN_0802b350(4)` → `FUN_0802b0d0` returns an in-range pointer (`0x80f…`, manager
  `DAT_0802ab5c` header segcount **25**), so the precondition `FUN_08008a04` (ptr ∈
  `[0x08000000,0x08200000]`) **passes**; INIT plays its files and sets the app-mode byte
  `+0x1b = 2` (`ctx 0x0800779c`). `FUN_0803e384` (the mode-8/`|=0x200000` power-off enter)
  is **never called** (0 hits). Standby is entered with **`+0x1b == 2`**, so `FUN_0803ef7c`
  does *not* take its mode-8 shutdown spin. (The Leg-17 note predates the head commit's
  timer-status decouple settling the boot; the power-off premise no longer reproduces.)

### The real wall — standby's book-descent waits on GPIO input pin 0

With mode 2, `FUN_0803ef7c` takes the **book-descent branch** (`bVar11` set by an event whose
payload `*param_2 == 0x104c`, and `+0x1c != 2`): it runs `FUN_080a483c()` then a wait loop at
`0x0803f0fc`:

```
do { func_0x08006954(6,0); iVar8 = func_0x08006978(0); } while (iVar8 != 0);
```

`func_0x08006978(0)` reads **`GPIO_IN0` (`0x040000bc`) bit0** (nandboot leaf, disasm-confirmed:
`ldr r1,[r1,#0xbc]; ands r0,r1,r2 lsl r0`). It waits for pin0 to read **released (0)** before it
sets `+0x44 = 1`, drives `GPIO_out6 = 1`, and `event_post`s (`0x08009544`) the transition that
descends to book. The tt-emu GPIO idle word `0x3201` (MT's) has **bit0 = 1**, so the pen spins
forever. `soc-core-registers.md` documents `GPIO_IN` bits **0/1/11 = battery-OK comparators**;
bit0 idles **released/0** on the 1st-gen board (MT sets it). Presenting the authentic ZC3201 idle
level lets the unmodified standby SM leave the wait and descend — modelling the pin, not hooking.

### Fix (landed, product; MT byte-identical)

* **`FirmwareProfile.gpio_in_idle`** (default MT `0x3201`; **ZC3201 `0x3200`** — bit0 cleared),
  threaded through a new **`GpioBlock(in_idle=…)`** constructor param and
  `build_zc3201_machine`. MT constructs `GpioBlock()` unchanged (default idle), so its whole
  GPIO/boot-button model is bit-for-bit as before.

**Verified** (`test_zc3201_standby_descends_past_gpio_pin0_wait`, `scripts/zc3201_descend_probe.py`):
INIT `0x08038e48` (≈6 M) → standby SM `0x0803ef7c` (≈15.6 M) → **descends past the pin0 wait**,
`play_chomp_voice` fires (≈17.7 M), and the statechart AO current-state handler hands off to the
**voice-player leaf `Fwl_pfVoice_fn` `0x0809eda4`** (≈18.5 M); `sm_dispatch_hierarchy` runs
~630 k times (the event pump is alive). Added to `Zc3201Debugger.STATE_HANDLERS`. MT unregressed.

### Leg 18 resume pointer — the ZC3201 audio DAC completion (blocks the power-on voice → book)

The pen is now parked in the **voice-player active object** (`Fwl_pfVoice_fn` `0x0809eda4`): it
started the power-on voice (`play_chomp_voice` → `voice_play_sample` `0x0809f068`) and **waits
for the DAC to finish** — it polls an audio-state flag `*(DAT_0809ecec + 1) & 4` (getter
`0x0809f57c`) that the audio-done IRQ would advance. That completion never arrives, so book entry
is blocked. Traced the voice-play MMIO (`scripts/zc3201_audio_mmio.py`):

* The DAC DMA **is** on the shared `0x04010000` block (like MT), but ZC3201's **submit encoding
  differs**: it writes the control word at **`+0x00`** directly (e.g. `0x00520424`) with **no
  `+0x0c` wordcount START** (MT's `AudioDma._on_start` gate), and also programs **`0x04080000`**
  (`|= 1`, later `= 0x13`) plus the SysCon audio-clock regs (`0x04000058/5c/64`, `0x04000008`
  divider). Completion is delivered via the second-level/top-level IRQ latches (`0x0400004c` /
  `0x040000cc`, heavily polled) — the ZC3201 audio IRQ line.
* Because the encoding differs, **MT's `AudioDma` model does not fire** (0 DAC submits) and its
  `DMA_CTRL` kick-clear path could *spuriously* clear the audio IRQ line, so it was **not** wired
  into `build_zc3201_machine` (kept `L2NandBuffer`). Retargeting the audio DAC is the next leg:
  RE the ZC3201 DAC submit (the `voice_play_sample` → `FUN_08026fa0`/`FUN_08026fe8` path and the
  `0x04010000`/`0x04080000` control words), model its completion on the correct IRQ line, and
  confirm the voice-player AO finishes and the statechart descends into **book mode**.

**Then** (unchanged): re-point the **OID sensor** (ZC3201 capture-state + GPIO pin — MT
`bit_count 0x08008c09` twin) and drive a product/cover-OID **tap** → book → the shared GME
interpreter plays on the tapped OID (`.gme` on B:), and **parametrise `tests/test_scripting.py`**
(+ the other gme-based tests) over both firmwares.

**State now:** the unmodified firmware boots through the complete mount + codepage, INIT does its
real work (mode 2, no power-off), and — with the ZC3201 GPIO idle (bit0 = 0) — the statechart
**descends past standby's GPIO-pin0 wait** and starts the power-on voice, handing off to the
voice-player AO. It then blocks there on the **unmodelled ZC3201 audio-DAC completion**. MT
unregressed. Product change this leg: `FirmwareProfile.gpio_in_idle` (+ ZC3201 `0x3200`), the
`GpioBlock(in_idle=…)` param, the `build_zc3201_machine` wiring, and the `Fwl_pfVoice_fn` leaf in
`Zc3201Debugger`. Tests: `test_zc3201_standby_descends_past_gpio_pin0_wait` + the `gpio_in_idle`
assertions in `test_profile_load_layout_distinct`. Probes: `scripts/zc3201_init_precond.py`,
`scripts/zc3201_descend_probe.py`, `scripts/zc3201_audio_mmio.py`.

## Leg 19 — the DAC-completion premise is CORRECTED: the pen reaches book-mode ENTRY; the real wall is the un-wired ZC3201 OID sensor

Leg 18's resume pointer said the pen "blocks in the voice-player AO waiting for the ZC3201
audio-DAC completion" and framed the next step as modelling the DAC. **Re-measured at HEAD
(probes `scripts/zc3201_audio_block.py`, `scripts/zc3201_chomp_open.py` + session traces,
all live-instrumented), this premise is wrong.** The DAC completion is **not** on the
critical path to book/GME play. Load-bearing findings (all Proven — disasm + live run):

* **`play_chomp_voice(0x13)` is a fire-and-forget power-on chime inside the book/game-mode
  ENTRY.** Its caller (LR `0x0804c3c0`) is **`FUN_0804c164`** — the book/game entry action
  (huge state-reset of the app context `iVar11+0x40`, then `play_chomp_voice(0x13)`). Its
  **tail does NOT wait for the voice**: it sets the app-mode byte `+0x1b = 2`, a ready flag
  `+0x18 = 1`, arms the **100 ms HAL software timer** `func_0x08006b64(0, 100000, 1,
  cb=0x080065ec)` (the same periodic driver Leg 17 identified — `cb` posts event `0x30`) and
  **returns**. So book entry **completes**; the pen runs the event-pump AO-poll idle (parks at
  `sm_dispatch` `0x080096a8`, returns to the pump `0x0800368c`), with the periodic tick armed.

* **The power-on chime never actually plays — and that is incidental.** `play_chomp_voice`
  opens `A:/VOIMG/Chomp_Voice.bin` via a **UTF-16 path** built by `FUN_080a4c48` →
  `FUN_0802675c` → `FUN_080265ec`. That converter takes its **table-conversion branch**
  because the **codepage-type flag `*0x08007c4c == 1`** (`FUN_08025df4` returns it; the flag is
  set by the **partial codepage load**, Leg 16's open contradiction), and with the partial
  codepage table it produces an **empty path** → `fs_open` returns `-1` → **`voice_play_sample`
  is never entered** (Proven: `voice_play_sample` `0x0809f068` 0 hits; the chomp `fs_open`
  returns `0xffffffff`). Forcing the simple-widening branch (`*0x08007c4c = 0`) **and** planting
  a synthetic `A:/VOIMG/Chomp_Voice.bin` makes `voice_play_sample` run to its tail and set the
  "done" bit — **but book does not "advance", because the pen is already in book entry**; there
  is nothing to advance *to*. ZC3201's **real** `Chomp_Voice.bin` is **unavailable** (the lab
  `firmware-re/tools/ttemu/content/A/VOIMG/` is empty; only the *MT* voice image exists), so the
  authentic chime cannot be reproduced — and since it is fire-and-forget, it **does not need to
  be** for the GME/OID-tap tests.

* **The "done-flag" `*(DAT_0809ecec+1) & 4` is the AMP-state bit, not a DAC-completion
  signal.** It is set **synchronously** by `FUN_0809efa4` at `voice_play_sample`'s tail (gated
  on the amp-on bit0 that `FUN_0809ed54` sets two calls earlier) — it drives GPIO9 (amp) /
  GPIO8 (mute), not playback progress. The voice-player AO (`Fwl_pfVoice_fn` `0x0809eda4`; its
  poll method `0x0809eb7c` returns `*(DAT_0809ecec+8)` = the medialib player) only *releases* on
  the medialib decoder **EOF** — `FUN_080ae5fc` returning the decoder state byte `*(dec+0x30)`
  nonzero. With no voice playing the player is `0`, the poll returns `0`, and the AO is **idle,
  not blocking**. So neither the amp bit nor the decoder EOF gates book entry.

* **The DAC MMIO is real but downstream.** After book entry the firmware does program the DAC
  (`0x04010000` control `0x00520424` ×96, `0x04080000` `|=1`→`=0x13` ×60, status `0x04010010`
  ×48 reads; completion via the `0x4c`/`0xcc` IRQ latches), currently routed to the
  `L2NandBuffer` with no audio completion. This only matters **once a voice actually plays**
  (which needs real ZC3201 voice data we do not have), and is **not** on the path to the
  OID-tap/GME tests. (So the Leg-18 "model the DAC completion" step is deferred/optional, not
  the wall.)

### The real wall — the ZC3201 OID sensor is not wired

`build_zc3201_machine` adds **no `OidSensor`** (unlike MT's `build_machine`). The pen sits in
book-mode idle **polling for input** (`GPIO_IN 0x040000bc` read ~1250×/run in the idle tail).
The tap event **`0x1060`** — consumed by the standby/book handlers `FUN_0803deac` /
`FUN_0803b710` and, in the reading state, by **`game_reading_sm_dispatch` `0x0805cbd4`** →
**`game_play_oid_voice` `0x0805c730`** (correspondences.tsv twin of MT `0x0806a0dc`) →
`gme_oid_to_playscript` `0x0804d358` — is never posted because nothing drives the sensor. MT's
`OidSensor` (clock GPIO2 / **data GPIO9**, `bit_count 0x08008c09`) **cannot be reused verbatim**:
on ZC3201 **GPIO9 is the audio amp** (`FUN_0809efa4`/`FUN_0809f538` bit-bang pin 9), so the OID
data/attention pin differs. This is a genuine ZC3201 hardware delta needing RE.

### Leg 19 resume pointer — wire the ZC3201 OID sensor → tap → GME play

1. **RE the ZC3201 OID sensor read.** Find the sensor-capture function that reads the physical
   sensor and posts event `0x1060` (the producer; the consumers `0x0803deac`/`0x0805cbd4` are
   known). It is GPIO-based (idle polls `GPIO_IN 0x040000bc`); recover its clock/data GPIO pins
   (≠ MT's GPIO2/GPIO9 — GPIO9 is the amp here), the ZC3201 `bit_count`/capture-state address
   (MT `0x08008c09` twin), and the frame format. `akoid_*` has **no** ZC3201 twin in
   correspondences.tsv, so the 1st-gen sensor path is distinct — trace it fresh (start from
   `game_reading_sm_dispatch 0x0805cbd4` backward, and from the book-idle GPIO_IN poll).
2. **Model + wire a ZC3201 `OidSensor` variant** (profile-driven pins/`bit_count`) into
   `build_zc3201_machine`, add `BootedMachine.tap`-style injection.
3. **Drive:** boot → book idle → `tap(product/cover OID)` → confirm `0x1060` posts →
   `game_reading_sm_dispatch` → `game_play_oid_voice` / `gme_oid_to_playscript` fire → GME
   interpreter plays the tapped OID's audio. Capture the play at `voice_play_sample`
   `0x0809f068` entry (the same observable the lab `zc3201_emu.py` uses — `{handle, off, size}`),
   which does **not** require the DAC/codec to run. The `.gme` is on `B:/`
   (`firmware-re/tools/ttemu/content/B/example.gme`).
4. **Parametrise `tests/test_scripting.py`** (+ other gme-based tests) over both firmwares.
   (Optional, only if a system voice must actually be heard: solve Leg 16's codepage size-unit
   contradiction for the full codepage load — which also fixes the chime path — and, if a real
   ZC3201 voice image is obtained, model the DAC completion per the Leg-18 MMIO map.)

**State now:** the unmodified firmware boots through the complete mount + codepage, descends
past standby, and **reaches book/game-mode ENTRY (`FUN_0804c164`), which completes and arms the
100 ms tick** — the pen is in book-mode idle, not blocked on any audio-DAC completion (Leg 18's
premise is corrected). The un-wired ZC3201 OID sensor is the sole remaining wall to a GME OID-tap
play. **No product code changed this leg** (analysis + probes only): `scripts/zc3201_audio_block.py`,
`scripts/zc3201_chomp_open.py`. MT unregressed.

### Leg 19 addendum — concrete MT OID-HAL anchors for finding the ZC3201 twins

The MT OID capture (from `2N-update3202MT/docs/oid-sensor-read-protocol.md`) is a **nandboot-HAL**
bit-bang, timer-driven — the architecture the ZC3201 nandboot HAL mirrors (same Anyka family).
The MT reference points, to find the ZC3201 twins by structure (the ZC3201 nandboot blob is at
`0x07ff8000`, aliased `0x08000000`; disasm it — these are HAL `0x07ffxxxx`/`0x0800xxxx` addresses,
**not** in the PROG decomp):

* **`hal_oid_bus_idle` `0x08005560`** — rest state: GPIO9→input (pull high), GPIO2→output, clock low;
* **`hal_oid_shift_in` `0x080055a4`** — the frame shift-in (IRQs off): abort unless GPIO9==0
  (attention), ACK, then clock `bit_count` bits sampling GPIO9 in the clock-LOW phase, MSB first;
* **`OidCaptureState`** struct at **`0x08008c08`**: `+0` frame_ready, **`+1` bit_count** (23 decode /
  0x20 poll) — tt-emu's `OidSensor.BIT_COUNT_ADDR 0x08008c09`;
* pins **GPIO2 = clock (host out), GPIO9 = data/attention (bidir)** — **but GPIO9 is the audio amp on
  ZC3201** (`FUN_0809efa4`/`FUN_0809f538` drive pin 9), so the ZC3201 data/attention pin **must
  differ**: recover it from the ZC3201 `hal_oid_shift_in` twin's `hal_gpio_*` pin args;
* **poll timer**: MT arms it at splash (`hal_oid_timer_start = func_0x080058b0`), callback
  `hal_oid_timer_cb 0x07ffd7cc` gates on attention and runs `hal_oid_capture_decode23`, posting
  `0x1060`. **First check whether the ZC3201 book entry starts its OID poll timer at all** — if not,
  that is an even earlier blocker than the pin wiring.

So the concrete next-leg tasks: (a) disasm the ZC3201 nandboot to find the `hal_oid_shift_in` /
`hal_oid_bus_idle` / `hal_oid_timer_cb` twins and their GPIO pins + the ZC3201 `bit_count` address;
(b) confirm the OID poll timer runs in book mode; (c) model a profile-driven ZC3201 `OidSensor`
variant (pins + `bit_count` addr) and wire it into `build_zc3201_machine` + a `tap()` API; (d) tap →
`0x1060` → `game_reading_sm_dispatch 0x0805cbd4` → `game_play_oid_voice 0x0805c730` → capture at
`voice_play_sample 0x0809f068` entry; (e) parametrise `tests/test_scripting.py` over both firmwares.

## Leg 20 — the ZC3201 OID sensor is RE'd, modelled and WIRED; a tap is captured + dispatched. New wall = game discovery / launch (not the sensor)

The Leg-19 wall — "`build_zc3201_machine` wires no `OidSensor`" — is **solved**. The entire
1st-gen OID capture HAL was reverse-engineered from the nandboot blob (disasm-cited below),
a profile-driven `OidSensor` variant was wired in, and the unmodified firmware now captures a
tapped OID and dispatches its event into the statechart. All findings Proven (disasm +
live-instrumented run; `scripts/zc3201_oid_probe.py`, `scripts/zc3201_oid_trace.py`).

### The ZC3201 OID HAL (nandboot, all in the `0x08000000` alias view)

The 1st-gen sensor is the **same two-wire bit-bang protocol** as MT (same Anyka family), on
**different pins / RAM** — confirming Leg 19's prediction that MT's GPIO2/GPIO9 wiring can't be
reused verbatim. The GPIO helpers decode to `GPIO_IN 0x040000bc`, `GPIO_OUT 0x04000080`,
`GPIO_DIR 0x0400007c` (DIR bit **1 = input**, so `dir(pin,0)`→input / `dir(pin,1)`→output):

* **`hal_oid_bus_idle` `0x08005cb0`** — rest: **GPIO16** → input (pull high), **GPIO7** → output
  low (clock). MT's data GPIO9 is the **audio amp** on ZC3201 (`FUN_0809efa4`/`FUN_0809f538`
  bit-bang pin 9), so the OID data line moved to **GPIO16**.
* **`hal_oid_shift_in` `0x08005d80`** — abort unless **GPIO16 == 0** (attention); raise clock;
  drive GPIO16 out high then low (host ACK); pulse clock; release GPIO16 to input; then clock
  `bit_count` bits, **sampling GPIO16 in the clock-LOW phase, MSB first** (accumulator `<<1` then
  `+bit`). Identical handshake structure to MT — the shared `OidSensor` state machine drives it.
* **capture-state struct `0x08007bf8`**: `+0` frame_ready, **`+1` bit_count** (`0x17` = 23 on the
  gameplay decode) — the twin of MT's `0x08008c09`.
* **`hal_oid_timer_start` `0x08005cf0`** — arms a **40 ms** (`0x9c40` µs) repeating HAL software
  timer, callback **`0x08005f48`**. Armed by **`state_init_power_on` (`0x08038eac`)** and the
  **book-descent SM (`0x0803f13c`)** → so the poll runs on the path we already reach.
* **poll callback `0x08005f48`** → **decode `0x08005eec`**: on attention, sets bit_count = 23,
  shifts the frame in, stores **`0x400000 | oid`** to `[gb_app_context 0x0800779c + 0x40] + 8`,
  then (`0x08005d34`, type check `& 0x600000 == 0x400000`) posts event **`0x1063`** via the
  ring-post `0x08003c44`. `frame32(oid)` (`(0x400000|oid18)<<9 | …`) is read as its top 23 bits =
  `0x400000|oid18`, which passes the type check — so the **MT `frame32` encoding is reused as-is**.

### What landed (product; MT byte-identical via defaults — full suite 162 green)

* **`OidSensor(pin_clock, pin_data, bit_count_addr)`** — the shared model (`peripherals/oid.py`)
  is now wiring-parameterised; MT's constants are the defaults, so MT is bit-for-bit unchanged.
* **`GpioBlock(amp_pin=…)`** — the amp-enable→`GPIO_IN` mirror pin is per-generation (MT GPIO16;
  ZC3201 GPIO9), so it no longer clobbers ZC3201's OID data bit (GPIO16).
* **`FirmwareProfile.oid_pin_clock/oid_pin_data/oid_bit_count_addr/gpio_amp_pin`** — ZC3201 = 7 /
  16 / `0x08007bf9` / 9 (MT = 2 / 9 / `0x08008c09` / 16).
* **`build_zc3201_machine`** now constructs and wires the `OidSensor` at the profile pins and
  exposes it as `machine.oid` (`.tap`/`.hold`/`.lift` — the same API MT's `BootedMachine.tap`
  drives). Nothing hooked: the firmware runs its own capture against the modelled GPIO.
* **`tt_emu/firmware/zc3201.py`** — corrected the Leg-17 dispatch misattribution: `FUN_080096a8`
  (`PC_VOICE_POLL`) is the **voice/media poll** dispatcher (object `0x08009708`, `+8` poll, `+0xc`
  `Fwl_pfVoice_fn`), **not** the statechart event dispatch. The real event dispatch is
  **`PC_EVENT_DISPATCH 0x080037d8`** (the pump `0x08003a84` drains ring `0x080075cc`
  head `0x0800774c` and forwards to the QHsm handler of the current state — scheduler `0x080075a8`).
  Added the OID HAL landmarks + `EVENT_OID_TAP 0x1063`.
* **Tests** — `test_zc3201_oid_profile_wiring` (profile data) and
  `test_zc3201_oid_tap_captured_and_dispatched` (end-to-end: the 40 ms poll runs in book mode, a
  held tap is latched, `[gb_app_context+0x40]+8 == 0x400000|oid`, event `0x1063` dispatched).

### Verified end to end (`scripts/zc3201_oid_probe.py` / `_trace.py`)

Boot → book-mode idle: the 40 ms poll callback `0x08005f48` fires **~4700×**. `machine.oid.hold(oid)`
→ `hal_oid_shift_in` runs once, the frame is latched (`gameplay_frames_served++`),
`[gb_app_context+0x40]+8 == 0x400000|oid` (e.g. `0x401f81` for oid 8065), and event **`0x1063` is
dispatched** to the app's current book-idle state handler (`0x080037d8`, event confirmed).

### Leg 20 resume pointer — the tap works; the wall is game discovery / launch (+ codepage paths)

The OID sensor is no longer the blocker — the firmware **receives** the tap. But the product tap
does **not launch example.gme**; instead the book state plays a **tap-acknowledge chomp chime**
(`play_chomp_voice` → `fs_open("A:/VOIMG/Chomp_Voice.bin")`, which fails — `VOIMG` is empty). Two
coupled downstream walls remain, both **distinct from the OID sensor** and rooted in the
FS/codepage layers:

1. **Game discovery never enumerates the `.gme`.** Instrumenting `fs_open 0x0800c0ec` across boot
   shows the firmware opening fixed system files — `A:/SYSTEM/profile.dat`, `B:/FLAG.bin`,
   `B:/.tiptoi.log`, `B:/Questionstatus.txt`, `A:/African.fen` — but **never any `*.gme`**. So no
   booklist entry for product 42 exists, and the product-OID tap finds nothing to mount (it falls
   through to the generic chime). Trace ZC3201's book-mode game-discovery / directory-enumeration
   (the twin of MT's B:/ `.gme` scan → booklist) and find why `build_zc3201_nand_image`'s FAT16 B:
   (which *does* place `example.gme`) is not enumerated — likely a FAT directory-listing path the
   small-page image or the mount does not satisfy. This is the primary wall to a GME play.
2. **The codepage/UTF-16 path conversion (Legs 16/19) still bites some paths.** With the
   codepage-type flag `*0x08007c4c == 1` the chomp/tap path builds **empty** (forcing simple
   widening `*0x08007c4c = 0` yields the correct `A:/VOIMG/Chomp_Voice.bin`); yet *other* boot
   paths (`B:/FLAG.bin`, …) convert **correctly** with the flag at 1 — so the partial codepage
   corrupts only some conversions. Resolve Leg 16's codepage size-unit contradiction for the full
   codepage load (authentic fix), which should make every path convert.

Once a product tap mounts `example.gme` and a content tap (OID 8065) reaches the shared GME
interpreter, capture the play at **`voice_load_and_play 0x0809f16c`** (r1 = media file offset — the
observable the lab `gme_engine_zc3201.py` uses) or **`gme_exec_command 0x0804c6e4`**, then
parametrise the gme-based tests over both firmwares. The tap-injection half is done and tested;
`machine.oid.tap(oid)` is the ZC3201 equivalent of MT's `BootedMachine.tap`.

**State now:** the unmodified firmware boots to book-mode idle, the modelled OID sensor's 40 ms
poll runs, and a `machine.oid` tap is captured and dispatched into the statechart (event `0x1063`)
— the OID sensor is fully wired and hook-free. It does **not** yet play a GME because the book
state does not discover/launch `example.gme` (no `*.gme` enumeration) and plays a chime instead.
The success criterion (a GME plays on a tapped ZC3201 OID; gme tests on both firmwares) is
**not yet met**. MT unregressed (162 passed). Product changes this leg: the `OidSensor` /
`GpioBlock` wiring params, the four `FirmwareProfile.oid_*/gpio_amp_pin` fields (+ ZC3201 values),
the `build_zc3201_machine` OID wiring + `machine.oid`, and the `firmware/zc3201.py` dispatch
correction + OID landmarks. Probes: `scripts/zc3201_oid_probe.py`, `scripts/zc3201_oid_trace.py`,
`scripts/zc_nb.py` (nandboot disassembler).

## Leg 21 — the codepage IS resolved (simple-widening); game discovery RE'd end-to-end; two precise FS-layer blockers pinned

Leg 20 left two "FS-layer" walls: (1) the recurring codepage full-load, and (2) game
discovery never enumerating `B:/*.gme`. This leg **resolves the codepage** (authentically),
**adds the missing A: factory content**, and **reverse-engineers the entire ZC3201 discovery /
mount / play chain** — pinning the two remaining blockers to a precision the next leg can
close. All findings Proven (disasm + live-instrumented; probes
`scripts/zc3201_fsopen_probe.py`, `scripts/zc3201_cprecover_probe.py`,
`scripts/zc3201_discovery_probe.py`).

### Wall 1 SOLVED — the codepage size-unit contradiction is resolved: the pen falls back to simple widening

The Leg-16 "size-unit / block-unit contradiction" is fully cracked, and the resolution is
**authentic, not a hack**. The nandboot boot-file loader (`func_0x08000dcc` →
`FUN_0x08000868` index search → `FUN_0x08000cd0` content walk, disasm-cited) and the codepage
**demand-pager** (`codepage_recover` `0x08025b7c`) tie the record `size` field to three
consumers, all keyed to the **16-KiB block / 512-B page** device geometry:

* **W1 guard** (`func_0x08000dcc` `0x08000e2c`): rejects the bin unless `size >> 14 <= max`
  (codepage `max = 8`, i.e. **128 KiB**).
* **block-map copy** count `r7 = 1 + (size-1)>>14` — the loader copies exactly `r7`
  `{origin,backup}` entries into the codepage descriptor's **inline** block-map (`cp+6`), which
  is **only 9 entries** wide (`cp+6 .. cp+0x18`, where the 0x20-byte header begins).
* the demand-pager `codepage_recover(off)`: indexes `cp+6[off>>14]` for the origin block, reads
  `32·origin + (off>>9 & 31)`, returns `page[off & 0x1ff]`.

The codepage is **0xD6CCC = 879 820 B = 54 blocks** — far over the 8-block (128-KiB) cap. There
is **no** size value where W1 passes *and* the full table loads. The self-heal fallback
(`codepage_recover` → `FUN_080088c4` re-reads the on-NAND block-map for the faulting block)
**cannot rescue a partial pre-load on this hardware**: a real K9F5608 has a 16-bit row address,
so an out-of-range physical read (from a stale/garbage high `cp+6[]` entry) **wraps to a valid
page and succeeds with wrong data** — `FUN_08025b04` returns success, so the fallback never
fires. Therefore the **only** self-consistent behaviour is that the 879-KiB codepage **fails
W1, `codepage_load` returns 0, the codepage-type flag `*0x08007c4c` stays 0, and the path
converter (`FUN_080265ec`) takes its `flag==0` branch = plain byte-widening** (ASCII → UTF-16).
Every tiptoi filename is ASCII, so simple widening is exactly right — this is what the real pen
does, and it matches Leg 19's observation that forcing `*0x08007c4c = 0` yields the correct
paths.

**Fix (landed, product):** `nand_image_zc3201._lay_system_bin_index` now stores the system-bin
record `size` in **bytes** (was 512-B sectors), so codepage's true `0xD6CCC` trips W1 → flag 0 →
simple widening. **Verified** (`scripts/zc3201_fsopen_probe.py`): flag `*0x08007c4c == 0`, and
every `fs_open` path across boot converts **non-empty** — `A:/SYSTEM/profile.dat`, `B:/FLAG.bin`,
`A:/VOIMG/Chomp_Voice.bin` (the chomp path Leg 19/20 saw empty), `A:/studylist.lst` (the
discovery list — previously empty, which was the hidden discovery blocker), `A:/African.fen`, …
**Zero empty paths** (was 4). The MT nand tests are unaffected (large-page builder untouched);
`tests/test_firmware_profile.py` + `tests/test_nand.py` green.

### A: factory content — merge the container's `to_udisk` payload (DIAGNOSED, HELD)

The authentic A: factory content is the ZC3201 `.upd`'s own `to_udisk` payload
(`firmware.udisk_files` = **`VOIMG/Chomp_Voice.bin`, 4 320 228 B**), which MT's
`build_nand_image` merges into A:. Merging it into `build_zc3201_nand_image` makes
`A:/VOIMG/Chomp_Voice.bin` **exist**, so with the simple-widening codepage the power-on chime's
`fs_open` succeeds and `play_chomp_voice` **actually plays the chime** (`voice_play_sample` runs
once — real progress, the chime never played before). **But this is HELD** (the merge is
commented in `build_zc3201_nand_image`): the ZC3201 audio **DAC DMA shares the `0x04010000`
block with the L2 NAND staging buffer and its completion is unmodelled** (Leg 18). Verified
(`scripts/…` diag): once the chime plays, the boot still reaches book-mode idle and the OID tap
is **captured** (`ctx+8 == 0x400000|oid`), but the DAC/L2 interaction **parks the event pump** so
the tap event `0x1063` is no longer **dispatched** into the statechart (`0x080037d8`). So a real
`.gme` play needs the ZC3201 DAC modelled / separated from the L2 buffer **first**; until then
the merge is left off to keep the OID/standby tests green. (The test `.gme` stays B: content.)

### Wall 2 — game discovery is fully RE'd; it is the MT flow, renamed `studylist.lst`

ZC3201's discovery is the structural twin of MT's `oidfilelist.lst` flow
(`2N-update3202MT/docs/book-discovery-and-load.md`), with the list file renamed
**`A:/studylist.lst`** (list-id 1), scan root **`B:`**, filter **`*.gme`**, identical
**0x424-header + 0x214-record** format. Pinned addresses (runtime base `0x08008000`):

* discovery setup `FUN_080a178c` → open/create list `FUN_080a1f9c` (`lst_file_open_or_create`
  twin; `fs_open(path,2,2)` r/w else `fs_open(path,1,1)` create; handle → `ctx+0x62c`) →
  gate `FUN_080a2224` (skip if the list already has a valid 0x424 header) → finalize
  `FUN_080a2070` → recursive scanner **`FUN_080a1d40`** (`opendir` `FUN_080ab860`, per non-dir
  dirent writes a 0x214 record via `FUN_080a089c`, count → `ctx+4`).
* mount = **`gme_mount_check_product` `FUN_080297dc`**: loop bound `*(u16)*DAT_08029b68` (the
  fresh scan count); per record `fs_open(path,0,0)`, magic `hdr@8 == 0x238B`, and (mode 1)
  `hdr@0x14 == game_subctx+4` (tapped OID); match ⇒ handle → `p_filehandle_current_gme`
  `0x080d20a0`. (No language check, unlike MT.)
* tap dispatch = **`study_main` `0x0804dc38`** (event `0x1063`): product band → `FUN_080297dc`;
  content band → `gme_oid_to_playscript 0x0804d358` → `gme_exec_command 0x0804c6e4` /
  `voice_load_and_play 0x0809f16c`. Initial book-open mount is `akoid_open_check 0x080299a0`
  (mode 0). example.gme = product **42**, magic 0x238B, content OIDs 8065-8067.

**Requirement (confirmed):** the `.gme` lives on **B:**, and the firmware **creates + writes
`A:/studylist.lst`** at scan time, so the emulator's A: FAT must be **writable at runtime**
(MT's tt-emu already relies on this — it never pre-seeds `oidfilelist.lst`). Our `NfcController`
small-page write path round-trips (Leg 13), so this should hold once the two blockers below are
cleared.

### Blocker #1 (Proven) — the scan root path is built on a stack the ZC3201 pointer-guard rejects

The scan root `"B:"` is converted to a **stack-local** wide string and copied into the scan
context (`ctx+8`) by `FUN_08024d84` — but that copy is **guarded**: `FUN_08024d84` copies only if
`FUN_08008a04(src)` passes, and `FUN_08008a04(p) = (p ∈ [0x08000000, 0x08200000])` (disasm:
`p + 0xf8000000 < 0x200001`). ZC3201's SVC stack seed is the MT `svc_stack_top = 0x08400000`, so
at discovery **SP ≈ 0x083f_fce0** and the stack-resident `"B:"` (0x083f_fce8) **fails the range
check** → the copy is skipped → `ctx+8` keeps stale heap garbage (observed: `"…v0136…GERMAN…"`
firmware-version/language bytes) → `opendir` gets a garbage root → **0 records** → nothing
discovered. (This is a genuine ZC3201 delta: its valid-pointer window is only the lower **2 MiB**,
vs MT's 4 MiB.)

Lowering `svc_stack_top` **fixes the root** (`opendir` path becomes `"B:/"`), and OID *capture*
still works (`ctx+8 == 0x400000|oid`), **but** every value tried below the guard limit
(`0x08200000`, `0x081d0000`) **perturbs the fixed high-heap objects** (iterator `0x081d8058`,
services vtable `0x081d9ad8`, high-heap `0x081dd000`) enough that the OID tap event `0x1063` is
**no longer dispatched at `PC_EVENT_DISPATCH 0x080037d8`** (the boot's deep stack overruns them).
So the change was **reverted** (kept at the MT default) to keep the boot/OID tests green; the
`FirmwareProfile.svc_stack_top` field + a full note are in place. **Next:** recover the *real*
ZC3201 SVC stack base (the skipped boot ROM sets it; it must be < 0x08200000 yet clear of the
`0x081dxxxx` high objects — candidate: just under `0x081d8000`, with the high-heap objects
relocated or the stack given a dedicated lower window), or move the high-heap allocations.

### Blocker #2 (Proven) — `opendir("B:/")` resolves to a NON-directory → enumeration finds nothing

Independently of #1 (i.e. even with the correct `"B:/"` root), the recursive scanner's
`opendir` (`FUN_080ab860`) calls `File_Open("B:/")` (`0x080a8e04`) and then
`FUN_080a8a8c(handle)`, which returns 1 **only if the handle is a directory** (`*(fh+0x1c)` ==
valid-magic `0x123455aa` **and** `dirent+0x3c & 0x10`). For `"B:/"` the resolved handle has
`dirent+0x3b = 1` but **`dirent+0x3c (attr) = 0`** (not a directory) → `FUN_080a8a8c` returns 0 →
`opendir` returns NULL → the scan loop never runs → **0 records**. So `File_Open("B:/")` is not
resolving the B: **root directory** as a directory (attr 0x10). The B: FAT16 itself is correct
(built with `EXAMPLE.GME` attr 0x20 in the root dir), so the fault is in how FatLib resolves the
drive-root path `"B:/"` (trailing-slash handling, or the B: partition-1 FAT read through the
MtdLib map). **Next:** trace `File_Open`'s drive-root path parse (compare against MT, whose
`opendir("B:/")` succeeds), and/or verify the B: FAT16 boot sector + root-dir clusters read back
correctly through the partition-1 map (Leg 13 verified only A:).

### Leg 21 resume pointer

The codepage wall is closed (Wall 1 done); discovery is fully mapped; the two remaining blockers
are FS-layer and precisely pinned. **To finish:** (a) place the ZC3201 SVC stack in a window that
is `< 0x08200000` *and* clear of the `0x081dxxxx` high-heap objects (recover the real base or
relocate the objects) so the scan root copies and the event pump stays intact; (b) fix
`File_Open("B:/")` to resolve the B: root as a directory (attr 0x10) so `opendir` enumerates
`EXAMPLE.GME`; then `studylist.lst` gains a record, a **product tap (OID 42)** mounts example.gme
via `FUN_080297dc`, a **content tap (8065)** reaches `gme_oid_to_playscript` →
`voice_load_and_play 0x0809f16c` (capture there — the lab observable), and
`tests/test_scripting.py` parametrises over both firmwares. **State:** MT green; ZC3201 boots →
book-mode idle → OID poll runs → tap captured (`ctx+8 == 0x400000|oid`) → discovery scan runs with
the correct list path; blocked at the two FS-layer walls above. Product change this leg (kept, green):
`nand_image_zc3201._lay_system_bin_index` (system-bin record size in **bytes** → the 879-KiB
codepage trips W1 → flag 0 → simple-widening path conversion). Diagnosed but **held** (not landed):
the `firmware.udisk_files` → A: merge (chime plays but the unmodelled DAC/L2 interaction drops the
OID tap dispatch — needs the DAC modelled first) and the `FirmwareProfile.svc_stack_top` field
(added + documented; ZC3201 kept at the MT default because every in-range value collides with the
fixed high-heap objects and perturbs the event pump). Probes:
`scripts/zc3201_fsopen_probe.py`, `scripts/zc3201_cprecover_probe.py`,
`scripts/zc3201_discovery_probe.py`.

## Leg 22 — ALL THREE walls solved: the .gme is discovered, mounted, and PLAYS on a tapped OID

Leg 21's three pinned walls are each closed, with the fix Proven against the firmware's own
behaviour (live-instrumented probes). The unmodified 1st-gen firmware now boots → discovers
`B:/EXAMPLE.GME` into `A:/studylist.lst` → mounts it on a product-OID tap → and **plays a
content-OID's media through the shared GME interpreter** (`gme_oid_to_playscript` →
`voice_load_and_play` → `voice_play_sample`), with the played `(offset,size)` matching the game's
own media table. A dedicated gme-based test asserts the whole chain
(`tests/test_zc3201_scripting.py`); the full suite is **165 passed** (MT unregressed at 164 + the
new ZC3201 test).

### Wall 1 SOLVED — the authentic SVC stack top is `0x08200000` (not the MT `0x08400000`)

The real value came straight from the firmware's **own reset handler** (nandboot `0x07ff8094`,
disasm): after zeroing `.bss` `[0x08006fe4,0x08008000)` and setting the IRQ/ABT mode stacks, it
does — in SVC mode, immediately before `ldr pc, =boot_task_main` —

```
07ff8108  mov r1, #0x8200000
07ff810c  mov sp, r1          ; the SVC stack top
07ff8110  ldr pc, [pc,#..]    ; = boot_task_main 0x0802b8bc
```

So ZC3201's SVC stack top is **`0x08200000`** = exactly the top of its `Utl_UStr*` pointer-guard
window (`FUN_08008a04` admits `[0x08000000, 0x08200000]`). At the MT `0x08400000` the discovery
scan's stack-resident `"B:"` root fails the guard, the copy is skipped, and nothing enumerates; at
the authentic `0x08200000` the root passes, discovery enumerates `EXAMPLE.GME`, and the OID tap
dispatches. The high-heap objects (`0x081d8058`, `0x081d9ad8`, `0x081dd000`) live below
`0x08200000` and are **not** perturbed — Leg 21 saw perturbation only because B: was unmountable
then (Wall 2), so the boot never got far enough to matter. Fix: `FirmwareProfile.svc_stack_top =
0x08200000` (ZC3201; MT stays `0x08400000`).

### Wall 2 SOLVED — A: and B: share ONE even-block whole-disk map (B: was on the wrong plane)

`opendir("B:/")` returned NULL because **B: was never registered as a FAT drive** (drive count 1):
`FUN_0802c09c`'s read of B:'s logical sector 0 returned `0xFF`. Tracing the NAND rows the FatLib
mount reads (`_data_read_smallpage`) showed B:'s partition base (`FUN_0802ca7c`, = A:'s block count
= 512) resolving logical sector 0 to **physical block 1152 = `128 + 2·512`** — i.e. the FatLib
layer mounts *both* A: and B: through the **single whole-disk map object**
(`FUN_0802f0c0`/`FUN_0802cbd8`), a **contiguous even-block logical space** (A: = whole-disk logical
`[0,a_blocks)`, B: = `[a_blocks, a_blocks+b_blocks)`, `phys = 128 + 2·logical`) — *not* two hardware
planes. The Leg-13 image placed B:'s FAT data on the odd (plane-1) blocks at an independent logical
0, which the whole-disk map never routes to. Fix (`nand_image_zc3201.py`): place B:'s FAT volume on
the **even-block map at logical offset `a_blocks`** (`_place_volume(logical_offset=…)`), clamp
`a_blocks+b_blocks ≤ lo (951)`. Proven: B: then reads its FAT16 boot sector at logical 0
(`0x55AA`, `"FAT16   "`), registers as the 2nd drive, `opendir("B:/")` resolves the root as a
directory (attr `0x10`), and the scanner enumerates `B:/EXAMPLE.GME`. (The plane-1 odd-block map
*tags* are still laid — the lower MtdLib InitPlane scan wants a second plane — but both FAT volumes'
*data* lives on the even-block whole-disk map.)

### Wall 3 SOLVED (for dispatch + capture) — the audio-codec command-complete handshake

After the first voice submits, the event pump parked in a 6.5 M-iteration spin at nandboot alias
`0x08005c08`: the codec-command HAL writes `0x04036004` (`val & 0x3fe00000 | 0x10000 | 0x10`) and
**polls bit 27 (command-complete)** — and `0x04036000` was unmapped in `build_zc3201_machine` (only
MT mapped it, and only for the bit-19 clock-enable gate). With bit 27 never set the codec handshake
never returns, so a played voice hangs the pump and the OID content tap is never dispatched. Fix: a
ZC3201 audio-codec model (`stubs.make_zc3201_audio_codec_stub` / `OrBitsRegisterStub`) that reads
`0x04036004` back with bits 19 **and** 27 sticky-high (the "clock ready" + "command complete" status
the real codec latches), plus the DAC/dormant scratch blocks MT also maps. This is authentic
hardware modelling, not a firmware hook. With it the handshake completes, the pump stays alive, and
**every content-OID tap reaches `voice_play_sample`** (capture point). The full PCM decode path
(medialib → DAC → S16LE) is **not** modelled — the test captures at `voice_play_sample`
`(r1=offset, r2=size)`, the same observable the `firmware-re` lab `zc3201_emu.py` validates.

### A: factory content re-enabled — the power-on chime plays

With Wall 3's codec model the DAC no longer parks the pump, so the Leg-21-held
`firmware.udisk_files` → A: merge is **re-enabled** (`build_zc3201_nand_image`, mirroring
`nand_image.py:384`): `A:/VOIMG/Chomp_Voice.bin` (4.3 MiB) exists, the power-on chime's `fs_open`
succeeds, and `play_chomp_voice` plays it (`voice_play_sample` offset `0x55116`) — while the
subsequent product/content OID taps still dispatch and play (verified in one run: chime + product
mount + content play).

### Verified chain (`tests/test_zc3201_scripting.py`, probes `scripts/zc3201_opendir_probe.py`)

boot → book idle → power-on chime plays → **product tap (OID 42)** → `gme_mount_check_product`
mounts `EXAMPLE.GME` → its welcome media plays (media 1, offset `0x2d4a`) → **content tap (OID
8065)** → `gme_oid_to_playscript` → `voice_load_and_play` → `voice_play_sample` plays media 2
(offset `0x6332`, matching the game's media table). MT byte-for-byte unregressed (all ZC3201 changes
are ZC3201-profile / `build_zc3201_machine`-only).

### Leg 22 resume pointer — the remaining piece is Emulator-API/PCM parametrization

The **hardware bring-up is complete**: the core "GME interpreter / OID-tap / play" criterion passes
on ZC3201 at the machine level (`tests/test_zc3201_scripting.py`). What is **not** yet done is
parametrising the *high-level* `tests/test_scripting.py` over ZC3201, because its assertions ride the
MT-specific `Emulator` scripting API (`pen.state`/`registers`/`now_playing`/`tap`/`wait_for_audio`
/`expect_play`, `MtDebugger`, the MMU `read_va`, and **real S16LE PCM** capture). To finish:

1. **A ZC3201 `Emulator` build path** — select `build_zc3201_machine` + the existing
   `Zc3201Debugger` when `firmware.recognize` is ZC3201; map its book-mode state, mount/product
   detection (`gme_mount_check_product`), OID `tap` (already `machine.oid.hold/lift`), and a play
   observable at `voice_play_sample` (offset/size → media index via `GmeScripts.media_table`).
2. **PCM capture (only if a byte-level clip assertion is wanted on ZC3201)** — model the ZC3201 DAC
   DMA PCM output (medialib decoder → `0x04010000`/`0x04080000` → S16LE), the one genuinely large
   remaining model. Until then the ZC3201 play test asserts *media identity* (offset/size), not PCM.
3. **Parametrise** `test_scripting.py`'s mount+tap+play core over both firmwares; keep the
   PCM/register/YAML/multi-part-readout assertions MT-only (marked with a reason) unless (2) is done.

Product changes this leg: `FirmwareProfile.svc_stack_top` (ZC3201 `0x08200000`),
`nand_image_zc3201._place_volume(logical_offset=…)` + the even-block A:/B: layout + the re-enabled
udisk merge, `stubs.OrBitsRegisterStub`/`make_zc3201_audio_codec_stub`, the codec/DAC/dormant wiring
in `build_zc3201_machine`. New: `tests/test_zc3201_scripting.py`, `tests/_data.gme_zc3201`,
`scripts/zc3201_opendir_probe.py`. Full suite **165 passed**.
