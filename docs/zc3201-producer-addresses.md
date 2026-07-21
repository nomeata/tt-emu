# ZC3201 (1st-gen) `producer.bin` addresses — for the run-producer harness

Binary: `tt-firmware-reveng/ZC3201/data/producer.bin`, 87,816 B (`0x15708`),
ARM **ARMv5TE little-endian, non-thumb** at entry. **Load base / entry =
`0x08000000`.** BURN version field @`0x08000020` = `"BURN"`, `{2,3,0x33}` →
**2.3.51**.

Method (same as `mt-producer-addresses.md`): literal-pool pointer/string/magic
anchoring + `assert(__FILE__)` band anchoring + opcode-prologue matching + jump
-table / `bl` call-site confirmation. Reference twin decomp:
`fw/2N-Update3202/out/ghidra_artifacts/producer_decomp/` (OLD). Claims are
**Proven** (bytes/disasm cited) unless marked **Tentative**.

---

## 0. HEADLINE: this is NOT the MT/OLD structural twin — read first

The ZC3201 producer shares only the **FS / MtdLib / FatLib / asa library
lineage** with the 2N-Update3202 producers. The **producer application layer is a
different, older build** and several MT concepts **do not exist here**. Do not
transfer MT addresses or offsets.

| MT/OLD concept | ZC3201 reality |
|---|---|
| Two dispatchers `pr_cmd_burn` + `pr_cmd_media` | **One** dispatcher `0x08003664`, a 29-entry jump table |
| command-context global, cmd num at `+4` | **no such global** — command = **`packet[0..3]`** (a 12-byte packet) |
| commands 5/6/7/9/10/0x10/0x11/**0x22** | commands **1..0x1d** only; **there is no cmd 0x22** |
| flat OS/HAL vtable, NAND ops at `+0x20..+0x38` | **no flat NAND vtable**; OS prims are thin wrappers, NAND access is indirect through MtdLib/nandflash driver objects |
| 6 spare-tag magics | **only 2 exist**: `0x11235813`, `0x12345678`. `0x12121212/0x34343434/0x56565656/0x5a5a5a5a` are **ABSENT** |
| String-rich (dispatch/format strings) | **33 strings total** — heavily stripped; anchoring leans on magics + `assert(__FILE__)` + opcodes |

Consequence for the harness: you cannot reuse the MT vtable-hook seam. The
ZC3201 producer builds flash structures via the MtdLib/FatLib libraries (with a
`gb_RAMBuffer` working buffer, §1) and drives real NAND through an
indirectly-dispatched driver (§3). Recommended seams are given in §3/§6.

---

## 1. Entry / init sequence (Q1) — Proven

| what | ZC3201 addr | evidence |
|---|---|---|
| entry (word0 `ea00000b`) | **`0x08000000`** | `b 0x08000034` |
| reset init | **`0x08000034`** | clears WDT reg `0x04000034`; sets **IRQ(0x12) sp=`0x0802d000`**, **SVC(0x13) sp=`0x0802b000`**; `ldr pc,[0x080000c4]` → main. (No bss zero-fill in the vector; done later.) |
| IRQ handler / IRQ return | `0x08000068` / `0x080000b4` | `push{r0-r12,lr}` … `subs pc,lr,#4` |
| **C main** | **`0x08003430`** | `mov ip,sp;push{r4,fp,ip,lr,pc}`; runs early inits then the USB loop |
| **printf** (log oracle) | **`0x080003dc`** | `push{r0-r3};push{r4,r5,fp,ip,lr,pc}`; `%`(0x25) format parser — identical shape to MT printf |
| **malloc** wrapper | **`0x080004d4`** | `str lr…;pop{lr};b 0x08012c48` (real allocator `0x08012c48`, rounds size to `0x20`) |
| **free** wrapper | **`0x080004e0`** | `…b 0x08012dfc` |
| `gb_RAMBuffer` setup | `0x0800049c` | prints `"gb_RAMBuffer=0x%x, size=%x"` (@`0x08013554`); **buffer=`0x08015710`, size=`0xa000`**; inits via `0x08012bdc` |

bss / RAM: image occupies `0x08000000..0x08015708`; it runs **flat in RAM** so its
`.data` tail is writable. Initialized globals sit in the image tail (e.g. the
command ring indices `0x08015610/0x08015614`, §2); heap/bss/ring extend past the
image up to the stacks (**SVC `0x0802b000`, IRQ `0x0802d000`**). `gb_RAMBuffer` =
`0x08015710` (+`0xa000`).

### USB command loop (harness stop point) — Proven
`main` sets up a 12-byte packet buffer at `fp-0x1c`, then:

```
loop @0x080034a4:  r0=&pkt; bl 0x080035f8 (receive) ; tst r0,#0xff ; beq loop   ; spin while no cmd
                   r0=fmt; r1=pkt[0]; bl 0x080003dc (printf)
                   r0=&pkt; bl 0x08003664 (DISPATCH) ; b loop
```

- **USB loop spin address (stop the harness here): `0x080034a4`**
- **receive fn: `0x080035f8`** — a ring dequeue: compares write/read indices, if
  equal returns 0; else `memcpy` 12 bytes from `ring[read]`, `read++`.
  - **write index (host/producer): `0x08015610`**
  - **read index (consumer): `0x08015614`**
  - **ring base: `0x08024fa0`**, entry size **12 bytes**
- Harness handoff options: (a) place a 12-byte packet at `ring[write]`, bump
  `0x08015610`, let the loop consume; or (b) simplest — call **`0x08003664`**
  directly with `r0` = pointer to a 12-byte packet.

---

## 2. Dispatcher (Q2) — Proven, directly verified

**Single dispatcher `0x08003664`** (no burn/media split; **no cmd-ctx global**).

```
0x08003664  r4=pkt
            r3 = pkt[0..3] (LE word)          ; the COMMAND
            ip = pkt[4..7] (LE word)          ; arg word0 (passed as r0 to handler)
            r3 = r3 - 1 ; cmp r3,#0x1c
            ldrls pc,[pc,r3,lsl#2]            ; jump table @0x080036c4, 29 entries
            (default: return)
            handler: r1 = pkt[8]|pkt[9]<<8    ; arg word1 (r1)
                     r0 = ip ; bl <worker>
```

- **Command number = `packet[0..3]`, valid range `1..0x1d` (1..29).**
- **Jump table: `0x080036c4`** (29 words).
- Each handler forwards `pkt[4..7]`→`r0`, `pkt[8..9]`→`r1` to its worker.

Jump-table targets (handlers), cmd = index+1:

| cmd | handler | cmd | handler | cmd | handler |
|--|--|--|--|--|--|
| 1 | 0x08003738 | 11 | 0x08003810 | 21 | 0x080038d8 |
| 2 | 0x08003750 | 12 | 0x08003828 | 22 | 0x08003900 |
| 3 | 0x08003768 | 13 | 0x08003840 | 23 | (default) |
| 4 | 0x08003780 | 14 | 0x08003858 | 24 | 0x08003908 |
| 5 | 0x08003798 | 15 | (default) | 25 | 0x08003910 |
| 6 | 0x080037b0 | 16 | 0x08003870 | 26 | 0x08003918 |
| 7 | 0x080037c8 | 17 | (default) | 27 | (default) |
| 8 | (default) | 18 | (default) | 28 | 0x08003920 |
| 9 | 0x080037e0 | 19 | 0x08003888 | 29 | 0x08003928 |
| 10 | 0x080037f8 | 20 | 0x080038b0 | | |

Each handler is a 3-instr stub that tail-calls a worker `bl 0x8001xxx` (§4).

---

## 3. OS primitives + NAND seam (Q3) — divergent; partially Proven

**There is no MT-style flat OS/HAL NAND vtable in ZC3201.** The OS primitives are
individual thin wrappers, not a copied fn-ptr struct:

| primitive | addr | evidence |
|---|---|---|
| **printf** | **`0x080003dc`** | Proven (§1) |
| **malloc** | **`0x080004d4`** → `0x08012c48` | Proven (§1) |
| **free** | **`0x080004e0`** → `0x08012dfc` | Proven (§1) |
| RAM-buffer init | `0x08012bdc` | inits `gb_RAMBuffer` |

**NAND access seam (Tentative on exact leaves).** NAND read/write/erase are
reached **indirectly** through the MtdLib/FatLib "medium" object method pointers
(e.g. `ldr pc,[ip,#0x48]`, `mov pc,r3` seen at `0x080125cc/0x080125f0`), so no
flat `+0x20..+0x38` table exists and static `bl`-tracing does not reach the raw
NFC ops. The relevant driver bands (Proven by `assert(__FILE__)`):

| layer | src string | band (fn starts) |
|---|---|---|
| FatLib `V1.0.6` | `@0x08013c68` (load @`0x080025c0`) | init fn ≈ `0x0800254c` |
| asa (`ANYKA325` `@0x080156d5`) | loads @`0x0800a674`,`0x0800abe4` | `0x0800a54c`, `0x0800ab84` |
| FS driver (mkfs/format) `fs/driver.c` `@0x08014a8c` | loads @`0x0800bb38`… | `0x0800bacc`, `0x0800bd98`, `0x0800bfb8` |
| medium / partition `medium.c` `@0x08014dd0` | loads @`0x0800dd38`… | `0x0800dc9c`, `0x0800de30`, `0x0800df68` |
| **MtdLib / NandMtd** `nandmtd.c` `@0x08014e34` | loads @`0x0800e378`… | `0x0800e078` … `0x08010848` (block math, format) |
| **low-level nandflash** `nandflash.c` `@0x080154f0` | load @`0x0801270c` | `0x08012670`; driver math `0x08011a68`… |
| MtdLib banner `MtdLib_Base_1.0.8` `@0x080155c0` | load @`0x08012b10` | — |

Raw NFC hardware-register I/O was **not statically pinned** (built via
`mov`+`add` immediates, dispatched indirectly). MMIO seen in init: WDT/clock at
`0x04000034` / `0x040000cc`; a DMA/controller config store to `0x04010000` with a
RAM source (`0x02d30024`) at `0x080004ec`. **Recommendation for the harness:**
hook `malloc/free/printf` above; for NAND, hook at the **nandmtd band
(`0x0800e078..0x08010848`)** or the **nandflash band (`0x08012670`+)** and/or read
the produced structures out of `gb_RAMBuffer`/the working buffers — or single-step
once to capture the medium-object method table at runtime. Do **not** assume MT
offsets.

---

## 4. Command → worker map (Q4) — Proven edges; semantic labels Tentative

Each edge below is Proven (dispatcher jump table + handler `bl`). The MT command
numbers do **not** apply; these are the ZC3201 numbers.

| cmd | handler | worker `bl` | reaches | Tentative role |
|---|---|---|---|---|
| **5** | 0x08001660 | **0x08002da0** (+ **0x08002eb4**) | fs/driver + nandflash | init / chip setup |
| **6** | 0x08001a38 | **0x08008460** (via gate 0x080009b8) | state reset | detect-nand-param reset |
| **7** | 0x08001aa8 | **0x0800849c** (pre 0x08013378) | full-chip geometry op | **format / erase-all** |
| **9** | 0x08001ca4 | **0x0800a1bc** | asa param globals | asa/partition config set |
| **10** | 0x08001c30 | **0x0800849c** (same as cmd 7) | full-chip geometry op | mkfs / format |
| **11** | 0x08001b5c | **0x0800cf14**, **0x08009cb4** | block/boot writers | write data / boot block |

`0x0800849c` (cmd 7 **and** cmd 10) is the central format worker — Proven: it
resets status via `0x08008460`, then computes `blk_cnt*pages_per_blk*
pages_per_something` from the NAND-geometry global (`ptr @0x0801569c`,
fields `[+0x14]/[+0x18]/[+0x1c]`), reads the request packet, and iterates the
whole chip. It is the ZC3201 analogue of the MT format/mkfs worker.

`0x08002da0`/`0x08002eb4` (cmd 5) descend into the FS-driver and nandflash bands
respectively — Proven reachability. `0x0800a1bc` (cmd 9) is a small setter into 3
asa globals gated by one flag (Proven disasm) — a config-set, not a heavy worker.

---

## 5. Metadata / tag magics (Q5) — Proven

Only **two** of the MT six magics exist in the image (byte-scan, aligned):

| magic | present? | count | site | writer fn |
|---|---|---|---|---|
| `0x11235813` (blank page) | **yes** | 1 (`@0x08009ba8`) | loaded @`0x08009af4` (r3) | **`0x0800980c`** |
| `0x12345678` (boot data) | **yes** | many | loaded @`0x08009274`,`0x08009778`,`0x800c690`,`0x800e300`,… | fns `0x08009130`, `0x08009718`, `0x0800c1ac`, `0x0800e078`, … |
| `0x12121212` (map) | **no** | 0 | — | — |
| `0x34343434` (bin-info) | **no** | 0 | — | — |
| `0x56565656` (bin-info hdr) | **no** | 0 | — | — |
| `0x5a5a5a5a` (zone table) | **no** | 0 | — | — |

So the ZC3201 on-flash metadata scheme is **not** the MT map/bin-info/zone-table
tagging. The blank-page (`0x11235813`) and boot-data (`0x12345678`) writers exist
and are anchored above; the map/bin-info/zone writers **have no counterpart**.

---

## 6. Chip detect (Q6) — Partial

- cmd **5** (`0x08002eb4` branch) and cmd **6** (`0x08008460`) manage chip
  detect/param state (a small status struct is zeroed at `0x08008460`; `strb`
  fields `[+0..+7]`). Proven that these are the detect/reset path.
- The NAND geometry is held in a global struct (`ptr @0x0801569c`) with
  block/page/plane fields at `[+0x14]/[+0x18]/[+0x1c]`, consumed by the format
  worker `0x0800849c` (Proven).
- **The raw NFC read-ID register was not statically pinned** (indirect driver
  dispatch, immediate-built MMIO). The chip-ID/read-ID path lives in the
  nandflash band (`0x08012670`+) / nandmtd band. To feed the flash-IC row and
  return the chip-ID, the harness should trace this dynamically or intercept the
  detect worker — MT's flat read-ID-register approach does not transfer.

---

## 7. Quick list for the harness

```
load/entry     0x08000000  (b 0x08000034 reset -> main 0x08003430)
SVC sp 0x0802b000   IRQ sp 0x0802d000   image 0x08000000..0x08015708 (flat, writable)
gb_RAMBuffer   0x08015710 (+0xa000)     init 0x08012bdc
printf         0x080003dc
malloc/free    0x080004d4 / 0x080004e0  (real: 0x08012c48 / 0x08012dfc)
USB loop stop  0x080034a4   receive 0x080035f8
  ring: write-idx 0x08015610  read-idx 0x08015614  base 0x08024fa0  entry=12B
DISPATCHER     0x08003664   (single; cmd = packet[0..3], 1..0x1d; jumptab 0x080036c4)
               NO burn/media split, NO cmd-ctx global, NO cmd 0x22
workers   cmd5 0x08002da0/0x08002eb4   cmd6 0x08008460   cmd7 0x0800849c
          cmd9 0x0800a1bc   cmd10 0x0800849c   cmd11 0x0800cf14/0x08009cb4
format worker (cmd7==cmd10)  0x0800849c   (iterates full chip geometry)
magics   blank 0x11235813 -> writer 0x0800980c ;  boot 0x12345678 -> 0x08009130 et al
         map/bin-info/bin-hdr/zone-table magics ABSENT
NAND seam: NO flat vtable. Hook nandmtd band 0x0800e078..0x08010848 or
           nandflash band 0x08012670+, or read gb_RAMBuffer output.
```

All addresses **Proven** against `ZC3201/data/producer.bin` bytes/disasm on
2026-07-21 unless marked Tentative (semantic role labels in §4/§6, and the exact
NAND-leaf identities in §3/§6, which require dynamic tracing of the indirect
driver dispatch).

---

## 8. Dynamic-trace findings (`scripts/zc3201_producer_probe.py`) — Proven

The static RE above was confirmed by *running* the producer under Unicorn (the
harness the §7 quick-list feeds). Results:

* **The producer boots under the harness.** Startup `0x08000000 → 0x08003430`
  reaches the USB loop `0x080034a4` cleanly, printing `asic freq: 60000000`,
  `malloc init`, `gb_RAMBuffer=0x08015710, size=0xa000`, `Open USB interrupt`,
  `Enter event loop ......`. The `printf 0x080003dc` / `malloc 0x080004d4` /
  `free 0x080004e0` hooks and the single dispatcher `0x08003664` (called directly
  with a 12-byte packet, `packet[0..3]=cmd`) all work.
* **The NFC register band is `0x04070000`** — *not* MT's `0x0404a000`. During cmd 5
  the producer busy-polls **`0x04070200`** (a ready bit) at HAL PC `~0x08006744`
  and reads **`0x0407033c`** at PC `~0x08006784`. Returning "ready" (`0xFFFFFFFF`)
  for `0x04070000..0x04072000` removes a ~20M-iteration spin. (The HAL NAND band
  is around `0x08006700..0x08006810`.)
* **BLOCKER — `gNand` init fails chip-detect.** cmd 5 prints the FatLib
  (`FatLib_V1.0.6`) and MtdLib (`MtdLib_Base_1.0.8`) banners, then **`init error
  gNand`** (string `@0x08013ec0`, printed at `0x08003030`; the failing init fn is
  `0x08003084`) — *before* any read-ID, so cmd 5 returns 0 and the format worker
  `0x0800849c` (cmd 7) then sees `dataLen 0` and does nothing. The geometry global
  `0x0801569c` is still null at this point. **Next step:** model the `0x04070000`
  NAND controller (and/or seed the gNand geometry global) enough to pass
  `0x08003084`'s detect, then cmd 7's format worker iterates the chip and its NAND
  writes can be captured to a `WritableNand` (the ZC3201 variant of
  `tt_emu.nand_provision`, hooking the nandmtd/nandflash bands or the `0x04070000`
  register I/O).

---

## 9. The real format protocol — the chip-detect blocker is SOLVED (Proven)

§8's "gNand init fails chip-detect *before* any read-ID" was a symptom of driving
cmd 5 in isolation. The producer's format is **not** standalone: it needs the
device geometry loaded first by the host's protocol sequence. All Proven by
running the updated `scripts/zc3201_producer_probe.py`:

* **Command semantics (from the workers' debug strings).** cmd 1 `transc_test`;
  **cmd 2 `transc_get_chip_id`** (worker `0x08001260`); **cmd 3
  `transc_set_chip_param`** (worker `0x08001430`); cmd 4 `transc_erase`
  (`0x080015f8`); **cmd 5 `transc_format`** (worker `0x08001660`, which calls the
  gNand init `0x08002eb4`). (The §4 semantic labels were mis-assigned; these are
  Proven from the strings.)

* **The ring packet's `arg0` (`pkt[4..7]`) is a POINTER**, not inline data. The
  dispatcher forwards `r0 = pkt[4..7]` to the worker; for cmd 3 that is a pointer
  to a **287-byte (`0x11f`) chip-param blob** which the worker memcpy's into the
  static struct `0x0801f751`, then re-reads the physical chip-ID over the NFC and
  matches it against the blob's `[0:3]` expected-ID.

* **The chip-ID is read on NFC `0x0404a150`** (the MT producer's `nfc_readid_reg`)
  — the ZC3201 NAND HAL uses BOTH bands: `0x0404a000` (status/ready `+0x158`
  bit31, data `+0x150`) AND `0x04070000`. Returning the pen chip-ID there makes
  cmd 2 report `Get chip id: 0x…, count: 1` and cmd 3 print **`find chip=1`**.

* **cmd 3 builds the device descriptor `*0x08024b50`** (0x54 B) and sets the
  geometry global `0x0801569c`. The **NAND method vtable is filled with STATIC
  leaves** (docs/§8 expected these to need dynamic tracing — they are pinned):
  `+0x28=0x08005c1c +0x2c=0x08005b14 +0x34=0x08005be0 +0x38=0x08005d44
  +0x3c=0x08005db0 +0x40=0x08005a04 +0x44=0x08005af8 +0x30=0x08005cd8`. These are
  the read/write/erase/readspare leaves a capture harness hooks to a `WritableNand`.

* **Descriptor geometry decode** (builder `0x080056d4`, `r4 = 0x0801f751 = blob`):
  `desc[+4]=LE32(blob[0x14:0x18])` (block count), `desc[+0x14]=LE16(blob[6:8])`
  (page size), `desc[+0x10]=LE16(blob[0xc:0xe])`, `desc[+0xc]=LE16(blob[8:0xa]) /
  desc[+0x10]`, `desc[+0x1c]=LE16(blob[4:6])`, `desc[+0x18]=LE16(blob[4:6]) /
  desc[+0x1c]` (=1), `desc[+0]=blob[0x13]`, `desc[+8]=n_chips·desc[+0xc]`;
  `blob[0xf]==1` selects a different path (`0x08001590`). (`0x080133f8` is an
  unsigned divide.)

**Now-precise remaining wall.** With `cmd 2 → cmd 3 (matching chip-ID + blob) →
cmd 5`, the gNand init `0x08002eb4` **no longer prints "init error gNand"** — the
alloc succeeds against the real geometry. It then builds the FatLib/MtdLib
**medium** (`0x0800cd78` + `0x08012a88`) and calls a still-**null** method pointer
→ fetch fault at PC `0x10000`, **caller LR `0x0801271c`** (the nandflash band). The
faulting call is `ldr pc,[sb]` at `0x08012718` where `sb = *0x08027090` — the
**MtdLib allocator-pool object**, still null because the MtdLib init aborts on the
scrambled placeholder geometry (so both next-steps below reduce to "supply the real
geometry blob"). Two
coupled next steps: (1) the EXACT geometry sub-field encoding needs the real host
`flash_ic.ini` values (the pen's true NAND chip-ID + page/block byte sizes) so the
medium's method table initialises fully — the placeholder blob here yields a
partially-scrambled geometry (`page size=1`, `planes=64`, `plane size=512`); (2)
once the medium builds, cmd 5's format worker `0x0800849c` iterates the chip and
its writes (through the `0x08005b14/...` leaves) are captured to a `WritableNand`.

---

## 10. The MtdLib-init wall is SOLVED — `transc_format` completes (Proven)

§9's "still-null method pointer → PC `0x10000` fault" is **fully cracked**. It was
*not* the geometry alone; it was a **SoC-signature check**. Three ingredients,
all Proven by running `scripts/zc3201_producer_probe.py`:

1. **The cmd-3 chip-param blob IS the `.upd`'s own `flash_ic` descriptor**, not a
   hand-built struct. It sits at **`update.upd[0x200:0x240]`** (the first 64-byte
   `flash_ic` row): the Samsung **K9F5608** entry —
   `ec75a5bd 0002 2000 0008 0008 0004 10 01 0f 02 0b 01 00000010 … "Samsung K9F5608"`.
   Fields (`chipid u32, pagesize u16, pagesperblock u16, totalblocknum u16,
   groupblocknum u16, planeblocknum u16, sparesize u8, columnaddrcycle u8,
   lastcolumnaddrcyclemask u8, rowaddrcycle u8, lastrowaddrcyclemask u8,
   customnandflash u8, flag u32, cmdlen u32, datalen u32, descriptor[32]`) decode
   to **page 512, spare 16, 32 pages/block, 2048 blocks, planeblocks 1024,
   col-cycles 1, row-cycles 2, custom 1**. The builder `0x080056d4` decode
   (r4=blob, r5=desc), corrected from §9:
     - `desc[+0]  = blob[0x13]` (custom = 1)
     - `desc[+4]  = LE32(blob[0x14:0x18])` (**flag** `0x10000000`, *not* block count)
     - `desc[+0x1c]= LE16(blob[4:6])` (**page size** 512)
     - `desc[+0x10]= LE16(blob[0xc:0xe])` (**planeblocks** 1024)
     - `desc[+0xc] = LE16(blob[8:0xa]) / desc[+0x10]` (**planes** = 2048/1024 = 2)
     - `desc[+0x14]= LE16(blob[6:8])` (**pages/block** 32)
     - `desc[+0x18]= LE16(blob[4:6]) / desc[+0x1c]` (= 1)
     - `desc[+8]  = n_chips · desc[+0xc]` (= 2)
   The load-bearing branch is **`blob[0xf]==1`** (`columnaddrcycle==1` → *small-page*
   NAND). The Leg-6 placeholder blob had `blob[0xf]=0` → the large-page path →
   the scrambled `page size=1, planes=64`. Feeding the real descriptor prints
   `small page nand`, `NandPageCycle is 1`, `find chip=1 page size=512`,
   `planes=2 plane size=1024`.

2. **The physical chip-ID `0xBDA575EC`** on NFC `0x0404a150` — the dword the builder
   assembles from bytes `EC 75 A5 BD` (`blob[0]|blob[1]<<8|blob[2]<<16|blob[3]<<24`);
   it must equal `blob[0:4]` for `find chip=1`.

3. **The SoC chip-ID `0x33323931` ("1923") at `0x04000000`** (`SysCon REG_CHIP_ID`).
   *This is the real wall.* `mtd_set_pool` **`0x08012a88`** copies the pool method
   table (6 words) to the global **`0x08027090`**, prints the MtdLib banner, then
   calls the SoC-signature check **`0x08012a0c`** — a jump table
   (`cmp r0,#4; ldr pc,[pc,r0,lsl#2]`) of variants that each `ldr *0x04000000; cmp
   <sig>` (`0x33323931` "1923", `0x33323236` "6223", `0x414b3620`, `0x414b3224`).
   On **success** (`ldmdbne`) it returns with the pool intact; on **failure** it
   runs `pool[0xc](pool, 0, 0x18)` — an error-cleanup **memset that zeroes the pool
   method table**. The next `mov lr,pc; ldr pc,[sb]` at **`0x08012718`** (sb =
   `0x08027090`) then loads a zeroed slot → the PC-fault §9 saw. A harness returning
   `0` at `0x04000000` fails the check every time; returning the ZC3201 SoC chip-ID
   passes it, the pool survives, and cmd 5 finishes.

With all three, **cmd 2 → cmd 3 → cmd 5 completes**: `transc_format` returns
`RET r0=1` and MtdLib initialises the partition —
`MtdLib - NandPart:Bits=0x10000000,FstPl=0,StartB=0,BCnt=2048,PCnt=2,LPCnt=2,
BPerP=1024,PgPerB=32,BytPSec=512`.

**Remaining wall (next leg).** `transc_format` (cmd 5, worker `0x08001660`) only
**initialises** the MtdLib partition — it calls **none** of the 8 static NAND leaves
(verified: 0 hits during cmd 5). The actual chip erase + FS-metadata + FAT write is
driven by **other** commands: `transc_erase` cmd 4 (`0x080015f8`), the full-chip
iterate worker `0x0800849c` (cmd 7/10), and the block/boot writers
`0x0800cf14`/`0x08009cb4` (cmd 11). These need the **host protocol's real packet
argument layout** — cmd 4's "erase start/end" came through as `0/0` with the naive
`pkt[4..7]`/`pkt[8..9]` ring-word mapping, so the args are a small struct pointed to
by `arg0`, not the two ring words. Reconstruct that packet sequence (the Windows
host tool's driver), then hook the static leaves to a `WritableNand` and capture.

---

## 11. The write protocol — cmd semantics corrected; erase captured (Proven, Leg 8)

§10's Leg-7 resume pointer assumed cmd 4/7/10/11 were "erase / iterate-format /
writers" all funnelling through the 8 static gNand leaves. Driving them under
`scripts/zc3201_producer_capture.py` (which reuses the §10 cmd 2→3→5 recipe)
**corrects the command semantics** and pins the real write seam. All Proven from the
workers' own debug strings + disasm + a live run.

### 11.1 Dispatcher arg forwarding (re-confirmed)
`0x08003664`: `cmd = pkt[0..3]`; **`r0 = pkt[4..7]` (arg0)**, **`r1 = pkt[8..9]`
(arg1)**. Each worker receives arg0 in r0 (a POINTER) and arg1 in r1.

### 11.2 Real command semantics (from the `+++<name>+++` debug prints)
| cmd | worker | name | arg0 (pointer) target | fires leaves? |
|---|---|---|---|---|
| **4** | `0x080015f8` | **`transc_erase`** | **8-byte** `{u32 start, u32 end}` (memcpy 8 via `0x8013204`) → erase impl `0x08008320(start,end)` | **YES** — erase leaf `desc+0x38` |
| **7** | `0x08001aa8` → `0x0800849c` | **`transc_data`** | **0x18-byte** `{u32 dataLen; u32 ?; char name[16]}` (memcpy 0x18 into `0x08020a9c`) | via medium (below) |
| **10** | — | **`transc_update_self`** | — | no |

The Leg-7 labels ("iterate/format worker 0x0800849c is cmd 7/10", "cmd 11 writers")
were **wrong**: `0x0800849c` is `transc_data`'s **file-write** worker, not a format
iterator, and cmd 10 is `transc_update_self`.

### 11.3 `transc_erase` — PROVEN captured
`0x08008320(start,end)`: reads the gNand device `*0x08024b50`, clamps `end` to
`blocks·planes`, and for each block calls the **erase leaf `desc+0x38` = `0x08005d44`**
as **`leaf(dev, r1=0, r2=block)`** (block steps by the plane count = 2). Inside the
leaf, `abs_page = block · [dev+0x14](pages/block)`. Driving cmd 4 with
`arg0 → {0, 2048}` prints `erase start:0, end:2048` and fires the leaf **2043×**
(1019 distinct blocks) — the **full-chip erase is captured** into a `WritableNand`.

### 11.4 `transc_data` — the write seam + its blocker
`0x08001aa8` memcpy's the 0x18-byte arg0 into descriptor `0x08020a9c`, prints
`file:%s` (`desc+8` = the name) and `dataLen:%d` (`desc+0`), then calls the
write worker `0x0800849c(desc)`. That worker:

* returns early if `desc[8..0xb] == 0` (empty name);
* **looks up the file** by name in the producer's **file-records table** — global
  **`0x08020888`** (record **count at `0x0802088c`**; 0x24-byte records at
  **`0x08020898`**). The search is `strcmp 0x08007b40` driven by the record iterator
  **`0x08007bc8`**;
* streams the matching file's bytes to NAND through the **medium object
  `0x08027060`** (a method table built by cmd 5; write via `[0x08027060+8]`,
  finalize via `[+0x14]`). The medium's write reaches the gNand **write leaf
  `desc+0x2c` = `0x08005b14`** (`r0=dev, r2=block, data+tag via r1/r3/stack`).

**Blocker (the next leg's wall):** with an empty file-records table, cmd 7 finds
nothing and writes nothing (0 leaves, verified: `count@0x0802088c == 0`). The
file records **and their data** must be UPLOADED by *another* host command before
`transc_data` can commit them. Reversing that upload command — which fills
`0x08020888`/`0x08020898` and the data source the iterator `0x08007bc8` drains — is
the precise next step, after which cmd 7 fires the write leaf and the writes
(MtdLib metadata + FAT + file content) capture into the `WritableNand`. The
BurnTool plan (`BurnTool*/config_researcher.txt`) names the files: `PROG.bin`→NAND
`0x0`, voice→`A:VOIMG` udisk, one FAT partition (type 2), `fs start 0xcc0000`, 64
reserve blocks — a 32 MiB K9F5608 (2048×32×512).

### 11.5 Harness state
`scripts/zc3201_producer_capture.py` drives cmd 2→3→5→4→7, hooks the 8 static leaves
to a `WritableNand`, captures the full-chip erase, logs the leaf calling convention,
and reports the (empty) file-records table. It is the ZC3201 ring-dispatch analogue
of `firmware-re/tools/ttrun_producer.py`; once the file-upload command is reversed it
becomes the `run_producer_zc3201` seam for `tt_emu.nand_provision`.
