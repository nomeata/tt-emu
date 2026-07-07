# NAND flash chip + NAND flash controller (NFC)

Implementation reference for the **NAND flash subsystem** of the tiptoi 2N ("MT") pen
(Chomptech ZC3202N = Anyka AK1050 class, ARM926EJ-S): the flash chip's geometry and
addressing model, the NFC register block at `0x0404A000`, the ECC engine at
`0x0405B000`, the shared L2 buffer controller at `0x04010000` with its SRAM data
window at `0x08006800`, and the primitive operations (read / program / erase /
copy-back / read-ID / status) the firmware performs through them.

This file is about **how a physical location on the flash is addressed and moved** —
enough to implement a NAND-controller model in an emulator. What the bytes *mean*
(boot area, partitions, NFTL translation layer, FAT filesystem, spare-tag format) is
documented separately in `nand-image-layout.md`. The SRAM/boot-time memory context is
in `memory-map-and-boot.md`; the clock gate and pin-share registers the driver touches
are in `system-control-and-clock.md`.

Facts are marked **Observed** (read from firmware disassembly, a live-pen memory dump,
or demonstrated in emulation) or **Inferred**. Unmarked statements are Observed.
Registers are 32-bit little-endian.

---

## 1. Architecture: three MMIO blocks + one SRAM window, no DMA engine

```
CPU  ──memcpy──►  L2 SRAM buffer 4 @ 0x08006800 (512-B circular window)
                        ▲ │
                        │ ▼   (streamed by the controller itself)
   L2 buffer ctrl @0x04010000  ◄──►  ECC engine @0x0405B000  ◄──►  NFC @0x0404A000  ◄──► NAND bus
                                     (BCH encode/decode,           (command-list
                                      correction FIFO)              sequencer, R/B,
                                                                    CE, timing)
```

- **Observed: there is no DMA unit in the NAND page path.** The CPU `memcpy`s between
  the caller's buffer and the 512-byte L2 SRAM window; the controller moves bytes
  NAND ↔ ECC ↔ L2 by itself ("internal streaming"). The L2 buffer controller block at
  `0x04010000` is shared with other peripherals (audio uses it as a DMA engine — see
  `audio-dac-dma.md`); NAND uses **buffer 4** only.
- The NFC executes a **command list** of 32-bit micro-ops staged at `0x0404A100..`,
  started by a GO write to `0x0404A158`. Data-phase micro-ops route bytes through the
  ECC engine into/out of L2 buffer 4; **ECC parity is generated and consumed inside
  the engine and never appears in the SRAM window.**
- All NAND driver I/O is **polled** — ready bits and fill levels, no interrupt.
  Every poll happens strictly *after* the GO/strobe write that triggers the work, so
  a purely reactive emulator model (no timers) is sufficient (Observed in emulation).
- Ops are bracketed by an acquire/release pair: module **clock gate** on/off and
  **pin-share** save/set/restore (`[0x04000034] = 0` selects the NAND pins; see
  `system-control-and-clock.md`). One-time init: L2 ctrl `[0x04010000] = 0x620024`,
  clock-gate enable, NAND clock divider `[0x04036000] = (div-1) | 0x30000000 | 0x600000`.
  All of these can be modeled as benign read-back-what-was-written stubs.

---

## 2. NAND chip geometry (logical)

The **logical** geometry — the contract every layer of the firmware assumes — is:

| quantity | value | |
|---|---|---|
| data page | **2048 bytes** | Observed (device struct, flash image format) |
| spare / OOB per page | **64 bytes** | Observed (flash image format; firmware uses only an 8-byte tag of it) |
| pages per block | **64** → block = **128 KiB** data + 4 KiB spare | Observed |
| blocks per device | **4096** → 512 MiB data | Observed (live pen) |
| sector | **512 bytes**, 4 sectors per page | Observed |
| **allocation unit (AU)** | **4096 bytes = 8 sectors = 2 pages**, 32 AU per block | Observed (live pen device struct) |

The **physical part varies between production runs** (firmware carries a probe table:
a Samsung K9GAG08U0M ID `0x9551D3EC` in the update-image boot stage; a Hynix
HY27UF084G2B on a dumped live pen; Hynix H27UCG8T2A/H27UBG8T2B IDs get special
read-retry handling). What matters for emulation is the logical geometry above plus
the ID bytes the loaded boot image's probe expects (§7.5) — the firmware never
addresses the chip except through this logical model.

The **AU** is the granularity in which the flash-translation and filesystem layers
address the medium: one read/program transaction moves one AU (8 × 512-B sectors)
plus one 8-byte tag. It is the single most important number in this document; the
address decode in §4 hangs off it.

---

## 3. The runtime geometry descriptor

The NAND driver state lives in boot-SRAM BSS (beyond the end of the loaded boot
image — see `memory-map-and-boot.md`): a **driver state block at `0x08008CA8`** and a
**device geometry object at `0x08008CC4`**. Both are filled at boot by the probe
routine (reset + read-ID, §7.5) from a static per-chip config table embedded in the
boot image. Everything downstream — the flash-translation layer, the filesystem
medium, the address decode — consults this object.

Authentic values, **Observed** on a live pen (memory dump of `0x08008CC4`, 32/32
words verified):

| offset | value | meaning |
|---|---|---|
| dev+0x00 | 2 | chip/CE count field (Inferred) |
| dev+0x04 | `0x40041` | flags word; bit18 set; wear-leveling bit `0x10000000` clear → linear block resolver |
| dev+0x08 / +0x0c | 2 / 2 | plane / die count |
| dev+0x10 | `0x800` | **page size = 2048 B** |
| dev+0x14 | `0x100` = 256 | **row stride per block** (= sectors per block, 128 KiB / 512) — the block multiplier in every row computation |
| dev+0x18 | 1 | AU multiplier |
| dev+0x1c | **`0x1000`** | **AU size = 4096 B** (`AU = dev[0x18] · dev[0x1c]`) |
| dev+0x20..+0x3c | function ptrs | operation leaves: probe-ish, data read, page+tag program, page+tag read, erase, copy-back, … |

Driver state block `0x08008CA8` (Observed, live pen):

| offset | value | meaning |
|---|---|---|
| state+0 | 4 | ECC mode byte (feeds the parity/record-stride math, §4.2/§6) |
| state+3 | 0 | read-retry available (1 only for the Hynix H27U… parts) |
| state+4 | **0** | **randomizer/scrambler enabled — OFF** (§6.3) |
| state+8 | scratch | scrambler column cursor (set per op even when off) |

Also seeded by the probe: a global row-address-cycle count (**3** for this device
class) used when emitting address cycles (§5.2).

> **Caveat — the row-cycle count is a §5.6-class seed the boot recipe must not skip
> (Observed the hard way in emulation):** the boot blob's *static* value of this
> global (blob offset `0x79E0`) is **2**, not the probe's 3. A from-entry boot that
> skips the probe therefore emits only 2 row cycles, and every row ≥ 256 is silently
> truncated to 16 bits: the NFTL mount scan then sees duplicate chain heads (blocks
> 256 apart alias each other), head arbitration frees the "losers", and the **system
> bins get erased**. Either run the probe (option (a) below), which seeds 3, or
> pre-seed the global to **3** explicitly (option (b)).

> **Warning to implementers (Observed the hard way):** the values above must match
> real hardware, not a self-consistent fiction. `dev[0x1c]` **is `0x1000`**, not
> `0x200`. An emulator can appear to work with `dev[0x1c] = 0x200` (AU = 1 sector) if
> it also serves rows as flat 512-B sector indices and inflates the partition table's
> `AddrCnt` values 8× — the two errors cancel. But then every served descriptor and
> the codepage sector-cache geometry disagree byte-for-byte with a real pen, and
> adopting any one authentic value breaks the whole stack. Use the authentic
> geometry everywhere and the AU decode of §4.
>
> **Emulator implication:** these structs are *not* part of the loaded boot image
> (they are BSS above it). If emulation starts past the boot probe, they are zero —
> page size 0, every NAND op silently no-ops. Either (a) execute the boot-image
> probe at reset with the controller model answering RESET/READ-ID (§7.5) — the
> authentic, preferred way — or (b) pre-seed both structs and the row-cycle-count
> global with the values above.

---

## 4. ★ The address model and decode (the critical part)

### 4.1 Operation arguments

Every low-level NAND primitive takes a **(row, column)** pair (plus a chip-enable
index and data/tag descriptors, §7):

```
op(ce, row, col, datadesc, tagdesc)
```

The higher-level device leaves compute the row from (block, page-arg) as:

```
row = dev[0x14] · block + page_arg          # dev[0x14] = 256
```

**Observed:** `page_arg` is the **AU index within the block, 0..31** — *not* a
sector index and *not* a 2-KiB-page index. Each block therefore occupies a window of
**256 consecutive row addresses of which only the first 32 are used** (one row per
4-KiB AU); rows 32..255 of a block's window are never addressed. Concretely, for
block 134 the firmware reads AU 0 at row `0x8600`, AU 1 at row `0x8601`, … AU 31 at
row `0x861F`; block 135 starts at row `0x8700`.

> **Observed correction (NFTL FS area):** the "only 32 rows used" picture holds for
> the raw/bin region, but the flash-translation layer addresses its **1-MiB logical
> blocks** (spans of 8 physical blocks — see `nand-image-layout.md` §4) with the
> unit byte running the **full 0..255 range under the span's *base* block**: the
> §4.2 decode is linear, so rows `base<<8 | 32 .. base<<8 | 255` land in physical
> blocks `base+1 .. base+7` (the row "carries" from `base<<8|0xFF` into the next
> block). Consequently the model's **tag store must be keyed by the raw row
> (`block<<8 | unit`)**, never by a flattened sector base — flattening makes row
> `base<<8|32` collide with row `(base+1)<<8|0` (a neighbour block's page-0 tag) and
> destroys the span at the next mount scan (demonstrated in emulation; this was the
> cause of the historical "B: enumerates 0 entries" failure).

Erase and the block-scan tag read address a whole block via its window base
`row = 256 · block`. Copy-back gets two rows, `256·src_block + off` and
`256·dst_block + off` with the same intra-block offset.

The **column** argument is a **byte offset into the ECC-encoded record stream** of
the row (it is emitted on the NAND bus as the 2 column address cycles, §5.2). The
data of one AU is laid out on the physical medium as eight consecutive
**ECC records** of `eccsize` bytes each — 512 data bytes followed immediately by that
sector's ECC parity — then one short tag record (8 tag bytes + parity):

```
eccsize = 512 + parity(mode)                    # parity never enters the SRAM window
parity(mode) = 7·mode + 7        (mode < 4)     # e.g. mode 2 -> 21, eccsize = 0x215
             = 14·mode − 14      (mode ≥ 4)
```

Callers use the column two ways (Observed at both call sites):

- **sub-sector select** (single-sector reads, e.g. the codepage path):
  `col = k · eccsize` addresses the k-th 512-B sector *within* the AU;
- **tag-only reads** (the mount-time block scan): `col = nsec_per_AU · eccsize`,
  i.e. the offset *past* all data records — the transaction then reads only the tag
  record. (Here the column is a skip-size, not a sub-sector; `k` is effectively 0.)

The full-AU data path passes `col = 0` and transfers all 8 records + tag in one
command sequence.

### 4.2 The decode formula

A request `(row, col)` maps to the flat NAND **data** image (2048-byte pages
concatenated; spare kept separately) as follows. This is byte-exact-verified
(Observed: emulation traces of the mount + FAT reads, and an independent
codepage-content check):

```
AU_secs      = (dev[0x18] · dev[0x1c]) >> 9          # = 8 (authentic geometry)
block        = row >> 8                              # row / dev[0x14]
au           = row & 0xFF                            # AU index, 0..31
sub          = col // eccsize                        # 512-B sub-sector inside the AU
                                                     #   (0 when col==0; clamp to AU_secs-1)

phys_sector  = 256·block + AU_secs·au + sub          # flat 512-B-sector index
byte_offset  = phys_sector · 512                     # offset into the flat data image
```

One transaction then covers `transfer_bytes / 512` consecutive sectors starting
there (8 for a full AU, 1 for a sub-sector read), plus the row's 8-byte tag.

**Tags are keyed by the raw row, never by sub-sector**: the tag served/stored for
`(row, col)` is the tag of row `block<<8 | au`, independent of `sub` (one 8-B tag
per program unit). Key the tag store by this **raw row value**, not by the
flattened sector base `256·block + AU_secs·au` — in the NFTL FS area `au` runs
0..255 (§4.1 correction box), so the flattened key aliases neighbour blocks'
page-0 tags (Observed).

Erase decodes as `block = row >> 8` (low 8 bits ignored) and clears the whole
128-KiB block (data → `0xFF`, tags dropped).

### 4.3 Worked examples (Observed trace values)

| firmware intent | row | col | decode | flat byte offset |
|---|---|---|---|---|
| block 134, AU 0 (partition boot sector) | `0x8600` | 0 | sector `256·134 + 0` | `134·0x20000` |
| block 134, AU 1 (FAT sectors 8..15) | `0x8601` | 0 | sector `256·134 + 8` | `134·0x20000 + 0x1000` |
| block 134, AU 3 | `0x8603` | 0 | sector `256·134 + 24` | `134·0x20000 + 0x3000` |
| codepage row, 2nd sector of AU | row | `1·eccsize` | base + 1 sector | … `+ 0x200` |
| block scan of block *N* (tag only) | `N<<8` | `8·eccsize` | tag of sector `256·N` | (tag store) |

### 4.4 The classic mistake

Do **not** treat `row` as a flat 512-B sector index (`block = row//256`,
`sector = row%256`). That decode is correct *only* under the inauthentic
`dev[0x1c] = 0x200` fiction. At the authentic geometry it serves AU *k* as sector
*k* instead of sectors `[8k, 8k+8)` and returns 512 of the 4096 requested bytes —
the mounted FAT is subtly garbage (the boot sector at AU 0 still reads correctly,
so the failure appears far downstream as an empty directory scan). The formula in
§4.2 is identity when `AU_secs == 1`, so a single decode gated on the live
`dev[0x18]·dev[0x1c]` product is correct for both geometries.

**Inferred (open):** how this sparse row space maps onto raw silicon pages of the
physical part (one AU is plausibly one physical 4-KiB page on the real chip
generation, with the "2048-B page" being the logical/image unit) is not established —
and is irrelevant to an emulator, which only has to serve the decode above
consistently for read, program, erase and copy-back.

---

## 5. NFC register block `0x0404A000`

### 5.1 Registers

| offset | name | R/W | semantics (Observed) |
|---|---|---|---|
| `+0x100..+0x14F` | **CMD_LIST[0..19]** | W | Command-list FIFO: 32-bit micro-op words at `+0x100, +0x104, …`. Execution starts on GO and stops at the first word with **bit0 (LAST)** set. Re-used phase by phase: a data phase writes a single word at `+0x100` and re-triggers GO. |
| `+0x150` | **DATA_RD0** | R | Read-back bytes 0–3 (LE) captured by a read-to-register micro-op: the NAND status byte (`& 0xFF`) after command `0x70`; ID bytes 1–4 after command `0x90`. |
| `+0x154` | **DATA_RD1** | R | Read-back bytes 4–7 (ID bytes 5–8). |
| `+0x158` | **CTRL / STATUS** | R/W | *Write* **GO**: `0x40000200 \| 1 << (10 + ce)` — bit30 = go/enable, bits[13:10] = chip-enable mask, bit9 always set. *Read*: **bit31 = sequencer ready/done** (polled `while ((reg & 0x80000000) == 0)`). **bit14** is a sticky status bit the firmware clears (read-modify-write `& ~0x4000`) before every data phase and never reads back — model as don't-care. |
| `+0x15c` | **TIMING0** | W | NAND timing word, written once at probe (value `0xF5AD1` for this device class). Ignore. |
| `+0x160` | **TIMING1** | W | Second timing word (`0x40203`). Ignore. |

> **Register-bank alias (Observed):** the mask ROM of the pen's SoC drives the same
> NFC IP one bank lower — command list at `+0x00..`, CTRL/STATUS at `+0x58`, timing
> at `+0x5c/+0x60`, identical micro-op encoding and GO word. If mask-ROM code is
> ever executed, alias `+0x00/+0x58/+0x5c/+0x60` to the `+0x100/+0x158/+0x15c/+0x160`
> handlers.

### 5.2 Micro-op word encoding

```
bits[18:11] = payload: the byte driven on the NAND bus (command/address ops),
              or count−1 (data ops; the field extends above bit18 for counts > 256)
bit0        = LAST (end of command list)
low bits    = op type:
  0x64   command cycle (CLE):     word = cmd_byte<<11 | 0x64
  0x62   address cycle (ALE):     word = addr_byte<<11 | 0x62 ; the FINAL address
                                  cycle of a program op uses 0x63
  0x118  data NAND -> ECC -> L2:  word = (nbytes−1)<<11 | 0x119   (always LAST)
  0x128  data L2 -> ECC -> NAND:  word = (nbytes−1)<<11 | 0x129   (always LAST)
  0x58   data read NAND -> DATA_RD regs: word = (nbytes−1)<<11 | 0x59   (1..8 bytes)
  0x200  wait R/B                 (0x201 = wait + LAST)
  0x400  flag: wait-R/B attached to a command cycle (e.g. 0x18464 = cmd 0x30 + wait)
  0xc00  short timed delay        (e.g. 0x27401 = delay count 0x4E + LAST)
```

Observed literal words: `0x64` = cmd 0x00 · `0x18464` = cmd 0x30+wait ·
`0x40064` = cmd 0x80 · `0x8065` = cmd 0x10+LAST · `0x30064` = cmd 0x60 ·
`0x68064` = cmd 0xD0 · `0x38064` = cmd 0x70 · `0x48064` = cmd 0x90 ·
`0x7f864` = cmd 0xFF · `0x1a864` = cmd 0x35 · `0x42864` = cmd 0x85 ·
`0x3859` = read 8 ID bytes.

**Address cycles:** 2 **column** cycles (the op's `col` argument, LE) followed by
**3 row cycles** (the `row` of §4, LSB first; the count comes from the probed config).
Erase emits **0 column cycles** — row cycles only.

### 5.3 Data-phase transaction

A data micro-op's byte count is `payload_len + parity(mode)` — the record including
ECC parity. The engine strips/appends the parity; **only `payload_len` bytes cross
the SRAM window**. One page/AU operation is a sequence of per-record phases: for each
record { clear bit14 → write ECC engine config (§6) → write the single data micro-op
at `+0x100` → GO → move bytes through the window → poll ready → wait/check ECC }.

**The record size is not a fixed 512 (Observed):** always take `payload_len` from the
ECC config word (§6, bits[18:7]). The boot loader's metadata/scan reads use
**1024-byte** records, and the runtime FAT driver *programs* **1024-byte** records;
only the AU data path and the tag record use 512/8. See §7 for how a 1024-B record
crosses the 512-B SRAM window.

---

## 6. ECC engine `0x0405B000`

| offset | name | R/W | semantics (Observed) |
|---|---|---|---|
| `+0x00` | **ECC_CTRL / STATUS** | R/W | *Write*: engine config word, then the same word `\| 8` (bit3 = start). *Read*: **bit6** = op complete (write-1-to-clear: firmware writes back `\| 0x40`); **bit24** = encode/write-path done; **bit25** = decode/read-path done; **bit26** = decode pass, no errors (W1C); **bit27** = uncorrectable (W1C); neither 26 nor 27 set = correctable errors pending in the correction FIFO. |
| `+0x04..` | **ECC_CORRECT[i]** | R | Correction FIFO, one 32-bit entry per correction register; entry count = `mode<4 ? mode·4+4 : mode·8−8`. Entry: bits[9:0] = bit position, bits[21:10] = error flags; the firmware applies them as XOR bit-flips in the caller's buffer. |

**Engine config word:**

```
bits[18:7]  = payload length in bytes (512 for a data sector, 8 for the tag)
bits[24:21] = ECC mode/strength  (parity bytes = §4.1 formula)
base flags  = read:  0xc100012      write: 0x0100015
              raw 1-byte write (read-retry path): 0x100094  (no ECC)
```

Wait protocol after a data phase: poll `+0x00` until bit6, then until the direction
bit (bit25 read / bit24 write), write back `| 0x40`. Then classify: bit26 → clean;
bit27 → uncorrectable (the firmware then checks for an all-`0xFF` erased page —
≤ 3 zero bits are forced back to `0xFF` — else triggers Hynix read-retry / fails);
neither → drain the correction FIFO.

**Emulator:** always answer `0x40 | 0x1000000 | 0x2000000 | 0x4000000`
(complete + both directions done + pass) — the firmware then never touches the FIFO
or retry paths. Serve 0 for `+0x04..` anyway. No parity computation is ever needed
(§8).

---

## 7. L2 buffer controller `0x04010000` and the SRAM window

| offset | name | R/W | semantics (Observed) |
|---|---|---|---|
| `+0x00` | **BUF_CTRL** | R/W | Per-buffer control: RMW `(old & ~0xF700) \| (buf&7)<<8 \| (op&15)<<12 \| 0x800`, then bit11 cleared — bit11 is a strobe. `op 0` = attach/reset the buffer; `op 1` = **flush/commit** a partially-filled buffer to the peripheral (fired once per record on the program path, after the CPU staged the bytes). Init value `0x620024`. |
| `+0x10` | **BUF_STATUS** | R | Bits[19:16] = **buffer-4 fill level in 64-byte chunks** (0..8). Read path waits `!= 0` per 64-B chunk (raw-read variant waits `== 8`); program path waits `== 0` (drained). |

**Buffer 4 = `0x08006800`, 512 bytes, circular** (Observed: the copy loop reads
`0x08006800 + ((i·64) & 0x1FF)`). It is *not* a page buffer: every record moves as
one ≤512-byte transaction through this window; transfers longer than 512 bytes wrap
circularly (a 514-byte raw read leaves its last 2 bytes at window offsets 0–1). The
8-byte tag record is staged/served at window offset 0. The window is ordinary
boot-SRAM address space (buffers 0–4 at `0x08006000/0x6200/0x6400/0x6600/0x6800`,
plus 64-B buffers at `0x08006A00..`); keep it mapped as plain RAM and have the model
read/write it directly.

> **1024-B records cross the window as two 512-B slabs (Observed in emulation):**
> on the program path the firmware stages a 1024-B record (the runtime FAT driver's
> record size, §5.3) as: memcpy 512 B into the window → **poll BUF_STATUS drained
> (`== 0`)** → memcpy the next 512 B → poll drained again → fire the **flush strobe
> once** for the record. One drain poll per slab, one flush per record. A model must
> **capture each slab at its drain poll**, not only at the final flush: both slabs
> occupy the same 512 window bytes, so a capture-at-flush model records the last
> slab twice and **every FS program is corrupted with `data[:512] == data[512:]`**
> (the write path then never round-trips — e.g. discovery's directory entries
> vanish on re-read).

---

## 8. Primitive operations

All primitives share the `(ce, row, col, datadesc, tagdesc)` convention of §4.1.
The data descriptor carries {destination/source buffer, total transfer bytes,
per-sector stride}; the tag descriptor carries {tag buffer, tag length (8), ECC
mode}. A page/AU transaction consists of `nsec = transfer_bytes/512` data records
plus, when a tag descriptor is present, one final short tag record.

### 8.1 READ page/AU (+ tag)

```
1  [+0x158] &= ~0x4000
2  CMD_LIST: cmd 0x00 · 2 col cycles (col) · 3 row cycles (row) · cmd 0x30 + wait R/B · wait+LAST
3  GO (ce) · poll [+0x158] bit31
4  per record s = 0 .. nsec  (record nsec = the 8-byte TAG when tagdesc != NULL):
   a  clear bit14; ECC config = len<<7 | mode<<21 | 0xc100012 ; then |8
   b  CMD_LIST[0] = (len+parity−1)<<11 | 0x119 ; L2 BUF_CTRL(4, 0) ; GO
   c  data records: 8 × { poll L2 level != 0 ; memcpy 64 B from 0x08006800+((i·64)&0x1FF) }
      tag record:   memcpy taglen bytes from 0x08006800
   d  poll bit31 · ECC wait + classify (§6) · (descramble — inert, §8.7)
```

Returns bit31 set on hard failure, bit30 on corrected-with-retries.
**Image mapping:** serve `transfer_bytes` from `byte_offset(row, col)` (§4.2), and
the row's tag from the tag store. **Tag-only reads** (the block scan) are the same
sequence with a single short record and `col` pointing past the data records — serve
just the tag.

### 8.2 PROGRAM page/AU (+ tag)

```
1  row bounds check · clear bit14
2  CMD_LIST: cmd 0x80 · 2 col · 2 row cycles (0x62) · last row cycle (byte<<11)|0x63 (LAST)
3  GO · poll bit31
4  per record (last = TAG):
   a  ECC config = len<<7 | mode<<21 | 0x100015 ; |8
   b  CMD_LIST[0] = (len+parity−1)<<11 | 0x129 ; GO
   c  memcpy caller → 0x08006800 (512 B, or the 8 tag bytes) ; poll L2 level == 0
   d  L2 BUF_CTRL(4,1) flush strobe · poll bit31 · BUF_CTRL(4,0) · ECC wait (bit24)
5  CMD_LIST[0] = cmd 0x10 + LAST · GO · poll bit31
6  status read (§8.4): bit0 set = program FAIL
```

For records longer than the 512-B window (the FAT driver's 1024-B records, §5.3),
step 4c loops per 512-B slab — memcpy, drain poll — and step 4d's flush strobe fires
once after the last slab; capture per slab, not per flush (§7).

**Image mapping:** capture each 512-B record into
`byte_offset(row, 0) + 512·record_index`, the tag record into the tag store keyed by
the row base. Programs must overlay reads (last-write-wins).

### 8.3 ERASE block

```
CMD_LIST: cmd 0x60 · 3 ROW cycles only (0 column cycles; row = 256·block) ·
          cmd 0xD0 · wait R/B + LAST     → GO · poll bit31 · status (bit0 = fail, ≤16 retries)
```

**Image mapping:** `block = row >> 8`; whole 128-KiB block reads `0xFF` afterwards,
all its tags dropped. Erased blocks must shadow any pre-placed static content.

### 8.4 STATUS

```
CMD_LIST: cmd 0x70 · short delay (0xc00-type) · read 1 byte → DATA_RD0 (0x59|LAST) · GO+wait
return [+0x150] & 0xFF
```

Status byte: bit7 = not-write-protected/ready (firmware treats clear as fatal),
bit6 = ready, bit0 = fail. **Emulator: return `0xC0`.**

### 8.5 RESET and READ-ID (probe)

- **RESET:** 4 × (per CE) { clear bit14 · cmd 0xFF · timed wait + LAST · GO+wait }.
- **READ-ID:** cmd 0x90 · address cycle 0x00 · delay · read 8 bytes → `+0x150/+0x154`
  · GO+wait. The boot probe compares `[+0x150]` against the boot image's static chip
  config (4 CEs, all must match, else fatal hang), then programs TIMING0/1, fills the
  §3 structs and the row-cycle-count global, and evaluates the config's randomizer
  flag. **Emulator: answer with the ID word the loaded boot image expects** (e.g.
  `0x9551D3EC` = bytes `EC D3 51 95` for the Samsung-configured update image);
  `+0x154` = 0.
- A separate Hynix-ID match (H27UCG8T2A / H27UBG8T2B) would arm the read-retry
  machinery — not taken for the IDs above; never model it.

### 8.6 COPY-BACK (page-to-page copy)

```
CMD_LIST (one GO): cmd 0x00 · col+row(src) · cmd 0x35 · wait ·
                   cmd 0x85 · col+row(dst) · cmd 0x10 · wait+LAST   → status (≤50 retries)
```

The data never crosses the SRAM window. **Image mapping:** copy one program unit —
`AU_secs` sectors *and the unit's tag(s)* — from the decoded source offset to the
decoded destination offset. Used by the flash-translation layer's block folds; a
copy-back that loses the tag breaks the next mount.

### 8.7 Read-retry / randomizer (safe to omit)

Hynix read-retry (commands 0x36/0x37/0x16 with raw 1-byte register writes through
the window) only runs when the Hynix ID matched *and* an uncorrectable page was hit —
unreachable in an ECC-clean emulator. The data randomizer (a 1-KiB XOR table keyed
by a per-op column cursor `(col & 0x200) + (row & 0xFF)·4`) is compiled in but
**disabled by the probed config** (state+4 = 0): plain data crosses the SRAM window.
Both: don't model.

---

## 9. Spare/OOB and ECC — what an emulator must store

- The physical spare is 64 B/page, but the firmware's only spare payload is the
  **8-byte tag per program unit** moved as the tag record of read/program
  transactions (its content/format is `nand-image-layout.md` §NFTL). Model the tag
  store as a map **raw row → 8 bytes** (`block<<8 | unit`, the sub-sector column
  stripped — see §4.2; never flatten the key), default `0xFF…` (erased/free).
- **ECC parity never enters the SRAM window** and is computed/checked entirely
  inside the engine. With the §6 always-pass status, the emulator neither computes
  nor stores parity: **treat all data as ECC-clean**. The only behavioural remnant
  worth keeping in mind: a genuinely erased (all-`0xFF`) unit must read as `0xFF`,
  which falls out naturally from the erased-block layering.
- Bad-block handling is a content-level concern (tag markers) — a clean emulated
  chip has no bad blocks.

---

## 10. The emulator model

Back the chip with a **flat data image** (4096 blocks × 128 KiB, `0xFF`-filled where
unwritten; layout per `nand-image-layout.md`) plus a **tag map** and two overlay
sets: `written_sectors` (programs win over static content) and `erased_blocks`
(erase shadows static content with `0xFF` and drops the block's tags). Read layering:
overlay → erased-set → static image → blank `0xFF`.

Two viable seams:

**A. Register-level model (hook-free, preferred).** Map `0x0404A000`, `0x0405B000`
and `0x04010000` as MMIO; keep `0x08006000..0x08006FFF` plain RAM. State:
`cmdlist[20]`, `engine_word`, `l2_level`, latched `row`/`col`, `cur_cmd`,
`record_idx`, `pend_prog`, `datard[8]`. Handlers:

- `+0x100..` write → store micro-op. `+0x15c/+0x160` write → ignore.
  `+0x150/+0x154` read → `datard`.
- `+0x158` write with bit30 → execute the list: command cycles set `cur_cmd`
  (0x30 → latch read, `record_idx = 0`; 0x80 → latch program; 0x60…0xD0 → erase
  `row >> 8`; 0x90 → `datard = ID`; 0x70 → `datard[0] = 0xC0`;
  0x00/0x35 + 0x85/0x10 → copy-back src→dst; 0xFF/0x36/0x37/0x16 → no-op).
  Address cycles: first 2 bytes → `col`, rest (LSB first) → `row`. A `0x119` data op
  → deposit `payload_len` bytes (from the engine word, bits[18:7]) at
  `0x08006800 + (i & 0x1FF)`: ≥ 512 → the next data record from
  `byte_offset(row,col) + 512·record_idx`, then `record_idx += payload_len/512`
  (the boot loader's metadata/scan reads use 1024-B records, which wrap circularly
  in the window — §5.3/§7); < 512 → the row's tag; set
  `l2_level = 8`. A `0x129` data op → `pend_prog = payload_len`, `l2_level = 0`.
  Wait/delay ops → skip.
- `+0x158` read → constant `0x80000000` (ready).
- `0x0405B000` write → latch `engine_word`; read → constant `0x7000040` (§6).
- Program capture — **one 512-B slab per drain poll (Observed-required, §7)**: on a
  `0x04010010` read with `pend_prog` set (the program path's drain poll), read
  `min(pend_prog, 512)` bytes at `0x08006800`: ≥ 512 → program the next 512-B
  sector at the decoded offset (`record_idx++`), `pend_prog -= 512`; < 512 → set
  the row's tag, `pend_prog = 0`. Do **not** defer capture to the `0x04010000`
  op==1 flush strobe — a 1024-B record's two slabs share the window and a
  capture-at-flush model programs the last slab twice
  (`data[:512]==data[512:]`, every FS program corrupted). The flush strobe then
  needs no data action.
- `0x04010010` read → `l2_level << 16`; rule: **8 while a read deposit is
  outstanding, else 0** (satisfies the read `!=0`/`==8` waits and the program-drain
  `==0` wait simultaneously); cleared by the next GO or BUF_CTRL write.
- Pin-share / clock-gate / divider registers: read-back stubs.

Every spin loop is enumerable and satisfied by these constants (ready bit, L2 level,
ECC status, status byte `0xC0`, the probe's ID match) — the model is purely reactive,
no timers. Combined with running the boot probe once at reset (§3 warning), the
firmware's entire NAND stack — boot loader, flash-translation layer, filesystem, and
the updater's independent raw page ops — runs unmodified.

**B. Function-level seam (simpler, firmware-version-bound).** Intercept the four
driver primitives (read / program / erase / copy-back) at their entry points, decode
`(row, col)` with §4.2, and serve from the same image/overlays. Requires seeding the
§3 structs by hand and re-finding entry points per firmware build; the register model
avoids both.

**Simplifications that are safe** (all Observed as sufficient in emulation): no ECC
parity anywhere; always-ready/always-pass status; no randomizer; no read-retry; no
timing emulation; treat the 2-column/3-row address emission as already decoded
values; single chip-enable.

---

## 11. Gaps / open questions

- **Physical-silicon page mapping (Inferred only):** how the sparse row space
  (32 used rows per 256-row block window; 4-KiB AU records with inline parity) maps
  onto the real part's raw pages is unresolved — irrelevant to the emulator, but it
  means this doc's "geometry" is the *interface* geometry, and raw dumps of a
  desoldered chip would need their own decode.
- **ECC mode per region:** the mode nibble varies by descriptor (data vs tag vs
  info-page reads) and by the state byte (mode 4 coerced to 2 on one path);
  an ECC-clean emulator never depends on it, but a parity-accurate model would need
  the per-descriptor modes pinned down.
- The exact semantics of a few device-struct fields (`dev+0x00`, plane/die usage for
  multi-CE parts) are Inferred; the pen uses a single-CE code path in practice.
- TIMING0/1 bit fields are unmodeled (write-only constants).
