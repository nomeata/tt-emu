# NAND image layout — on-flash content the firmware expects

This document describes **what bytes live where** on the tiptoi 2N ("MT") pen's
512 MiB NAND: every structure the firmware's boot and mount paths read, and how the
layers stack — boot blob, system bins, metadata, flash-translation layer (NFTL), and
the two FAT filesystems. It is the specification for **building a NAND image the
unmodified firmware will boot from and mount**.

Companion documents (by name, same directory):

- `nand-and-nfc-controller.md` — the chip/controller model, the runtime geometry
  descriptor, and the **row/AU address decode** (§4 there). This doc uses its units and
  never repeats the decode.
- `memory-map-and-boot.md` — the boot chain (mask ROM → boot blob/SPL → PROG) and what
  is loaded to RAM.

**Units** (from `nand-and-nfc-controller.md` §2): sector = 512 B; page = 2048 B
(4 sectors); **AU (allocation unit) = 4096 B = 8 sectors = 2 pages**; block =
128 KiB = 64 pages = 32 AU = 256 sectors; device = 4096 blocks. Row numbers follow
the `block<<8 | au` convention of the controller doc. Each page carries an 8-byte
tag in its spare area (one tag per program unit).

Facts are tagged **Observed** (read from factory artifacts, a live pen, or firmware
behaviour under emulation) or **Inferred** (reconstruction choices that are
self-consistent but not confirmed against a physical dump).

---

## 1. Overall image map

The image has three content regions plus metadata, low to high:

```
block 0        boot pages (SPL image, mask-ROM page format)      ─┐
               + metadata pages at the TOP of block 0 (§2)        │  "low region"
blocks 1..~5   ASA bad-block-table replicas (factory pens, §2.5)  │  raw, no NFTL
blocks ~64..   the "bins": PROG (main firmware) + codepage        ─┘
blocks fs_start..4095   the FS area: ONE NFTL medium carrying two
                        partitions, A: (system) then B: (user),
                        each a bare FAT16 volume (§3–§5)
```

Everything below `fs_start` is addressed **raw/linearly** (boot loader and bin
loader); everything from `fs_start` up is addressed **through the NFTL layer** (§4).
`fs_start` itself is a field in the zone/partition table (§2.4) — it is
**host-chosen at factory time** (the block cursor after the last bin), not a
constant.

### 1.1 Factory layout (Observed, from factory-programmer behaviour)

| block(s) | content | spare tags |
|---|---|---|
| 0, low pages | SPL/boot blob (`0x7e80` B) in the mask-ROM boot-page format | (boot format) |
| 0, pages 59–63 | metadata: per-bin maps ×2, bin-info array, bin-info header, **zone/partition table** (§2) | 4-B magics `0x12121212`/`0x34343434`/`0x56565656`/`0x5a5a5a5a` |
| 1–5 (+1 spare) | ASA (bad-block bitmap) replicas, magic `"ANYKAASA"` | u32 copy counter 1..5 |
| ≥64 (host cursor) | bins, **each 128-KiB chunk written TWICE** (RB/UB block pairs): PROG (28 chunks) then codepage (7 chunks) → 70 blocks, e.g. 64–133 | u32 `0` (data page), `0x11235813` (all-FF page), `0x12345678` (boot-overflow data) |
| `fs_start` (= cursor after bins, e.g. 134) … 4095 | FS area: NFTL medium → partitions A + B → FAT16 volumes; **freshly formatted = all-0xFF** | 8-B NFTL tags on written pages only (§4.1) |

### 1.2 Emulator layout (the recipe target, §6 — Inferred/simplified, boots Observed)

A clean emulated chip has no bad blocks and never self-updates, so single copies
suffice and the ASA can be omitted. The metadata is served at its **row numbers**
(§2.1) rather than reconstructed byte-for-byte inside block 0.

| block(s) | content |
|---|---|
| 0 | metadata rows only (§2.1); rest erased |
| 1 | SPL/boot blob, single copy |
| 8–35 | PROG (`0x380000` B = 28 blocks, flat, single copy) |
| 36–42 | codepage (`0xd6ccc` B = 7 blocks, flat) |
| 134–613 | **partition A** span (480 blocks): FAT16 superfloppy image, written blocks only |
| 614–1637 | **partition B** span (1024 blocks): FAT16 superfloppy image, written blocks only |
| all other blocks | erased (`0xFF` data, blank tags) |

`fs_start = 134` and the bin start block 8 are **Inferred** (safe reconstruction;
the real pen's values are host-chosen and unverified) — but the firmware reads
`fs_start` from the zone table in the image itself, so any self-consistent choice
works.

---

## 2. Metadata (top of block 0)

Five small structures, written by the factory programmer as the **top pages of
block 0** and refreshed by every firmware self-update. The boot loader and the
mount read them via fixed **metadata row numbers** derived from the device
geometry.

### 2.1 Positions, rows, magic tags

| structure | factory position (Observed) | read at row (Observed in boot/mount code) | 4-B spare magic |
|---|---|---|---|
| per-bin log2phy map, bin *i* (of n) | block 0, page `61−n+i` (n=2 → pages 59, 60) | per-bin **map row** named in the entry table (§2.3); emulator uses rows 200+i (Inferred, free choice) | `0x12121212` |
| bin-info array | block 0, page 61 | row `dev[0x14]−3` = **253** (entry table form, §2.3) | `0x34343434` |
| bin-info header | block 0, page 62 | row `dev[0x14]−2` = **254** (header form, §2.3) | `0x56565656` |
| **zone/partition table** | block 0, page 63 | row `dev[0x14]−1` = **255** | `0x5a5a5a5a` |
| **factory bad-block bitmap** | ASA replicas, blocks 1–5 (§2.5) | row **2** | — |

`dev[0x14]` = 256 (row stride per block — see `nand-and-nfc-controller.md` §3).
Reads at these rows fetch a **0x1000-byte payload**. The exact correspondence
between "row 255" and "page 63 of block 0" in the authentic AU row space is an
open point (§8); an emulator sidesteps it by serving these specific row numbers
with the payloads below — Observed to satisfy both the boot loader and the mount.

The 4-byte magics double as the payload prefix for the map/bin-info pages (the
page data begins with the same u32 as its spare tag). The metadata pages the NFTL
layer itself writes later use tag `[2:4] = 0xfffa` (§4.1) — a different mechanism.

> **Row 2 — the factory bad-block bitmap (Observed in emulation; a §6 must-have):**
> before building the partitions, `mtd_init` reads a **0x1000-byte bitmap at
> row 2** — 1 bit per block, **set = bad** — the runtime home of the ASA data
> (§2.5). An **erased (0xFF) page marks every block bad and the mount loops
> forever**; serve a **zero-filled** 0x1000-byte payload at row 2 = no bad blocks.
> §2.5's "omittable without any ASA" holds for the *boot loader*, not for the
> mount.

### 2.2 Zone/partition table (row 255) — what the mount reads ★

This is the structure `fs_storage_mount_init` parses to find the FS area and size
the two partitions. Layout (all multi-byte fields **little-endian**; an earlier
big-endian reading of the same bytes was a misgrouping — Observed):

**Information header** (0x10 B at offset 0):

| off | type | field | notes |
|---|---|---|---|
| +0x00 | u32 | **TotalLen** | length of header + zone records = `0x10 + 0x10·nzones`; **also the offset of the fake-zone map region** — the mount reads word[0] and seeks there |
| +0x04 | u32 | **fs_start_block** | first block of the FS area ("fs Bootblock"); the medium is built over `[fs_start, 4096)` |
| +0x08 | u16 | reserved-size arg | consumed as a reserved-sector count; 0 works |
| +0x0a | u8 | **nzones** | count of Zone_Group records |
| +0x0b | u8 | 0 | |

**Zone_Group records** (0x10 B each, at 0x10, 0x20, …):

| off | type | field | notes |
|---|---|---|---|
| +0x00 | u32 | StartAddr | written by the factory; **NOT consumed by the mount** (B's base is derived from A's AddrCnt, §3) |
| +0x04 | u32 | **AddrCnt** | partition span. **UNIT: allocation units (AU, 4096 B)** — see box below |
| +0x08 | u8 | Subarea_Flag | 1 |
| +0x09 | u8 | Open_Flag | |
| +0x0a | u8 | Type | 0 MMI, 1 MMIBACKUP, 2 UNSTANDARD, 3 UNSTANDARDBACKUP, 4 STANDARD, 5 FAKE |
| +0x0b | u8 | **Symbol** | drive letter − 'A' (0 = A:, 1 = B:) — **the mount's lookup key** |
| +0x0c | u8 | Nand_NO | fake-zone ordinal |
| +0x0d | u8 | Partition_NO | running index |
| +0x0e/+0x0f | u8 | Nand_Char / Medium_Char | protection byte ×2 |

**Fake-zone / bad-block map** (at offset `TotalLen`): compensates the linear
address space for reserved/bad blocks. The FS area (`4096 − fs_start` blocks) is
split into groups of 0x2000 blocks (→ exactly **1 group** on this pen); per group
one u16 = the number of blocks **withheld** from the medium's logical capacity.
Record stream:

```
u16  fake_group_count            (1)
then per record:
  u16  len        = 2·ngroups + 4   (the mount memcpys len−4 bytes of groups)
  u16  zone_idx   (0)
  u16  groups[ngroups]              (reserved/bad blocks per group; 0 = none)
```

Medium capacity = `256 · (4096 − fs_start − Σgroups)` sectors. With no bad blocks
(emulator): `groups[0] = 0` → full capacity. (Observed semantics from the mount's
medium builder; the factory writes good-block-walk results here.)

> **★ AddrCnt units.** The mount looks up a Zone_Group by Symbol, takes its raw
> `AddrCnt`, and scales it by `f = (dev[0x18]·dev[0x1c]) / 512` — the AU size in
> sectors — before creating the partition: `span_sectors = f·AddrCnt`. With the
> authentic geometry (`dev[0x1c] = 0x1000`, AU = 4096 B) **f = 8**, so:
>
> - `span_sectors = 8·AddrCnt`, `span_blocks = AddrCnt / 32` (32 AU per block)
> - A: `AddrCnt = 0x3C00` → 480 blocks (60 MiB span)
> - B: `AddrCnt = 0x8000` → 1024 blocks (128 MiB span)
>
> The **byte values** `0x3C00`/`0x8000` are Observed (factory-programmer output,
> read back LE by the firmware). The AU unit is forced by the Observed live-pen
> device struct. Beware the classic trap (also flagged in
> `nand-and-nfc-controller.md` §3): with the inauthentic `dev[0x1c]=0x200`
> fiction, f = 1 and an 8×-inflated AddrCnt appears to work because the two errors
> cancel — but then nothing matches real hardware. Emit AddrCnt in AU and use the
> authentic geometry.

Observed factory table (3 zones — a synthetic FAKE zone whose AddrCnt is the A+B
**physical block** span, then A, then B):

```
Information: TotalLen=0x40  fs_start=<host cursor>  nzones=3
Zone[0] FAKE: Start=0       AddrCnt=0x2F0 (blocks)  Type5
Zone[1] A   : Start=0       AddrCnt=0x3C00 (AU)     Type2 UNSTANDARD  Symbol 0
Zone[2] B   : Start=0x3C00  AddrCnt=0x8000 (AU)     Type4 STANDARD    Symbol 1
```

A **2-zone table (A and B only) also mounts** — the lookup matches on Symbol, and
the FAKE record is not consulted by the mount (Observed under emulation). Exact
emulator payload in §6 step 5.

### 2.3 Bin-info: header, entry table, per-bin maps

These let the **boot loader** (and the self-updater) locate the bins by name. Two
encodings exist; the one below is what the boot-stage reader consumes (Observed:
the boot loader executes it; serving it boots):

**Header (row 254)**, 0x1000-B payload:

| off | type | field |
|---|---|---|
| +0x04 | u32 | entry count (loop bound) |
| +0x08 | u32 | start block of the bin region |
| +0x0c | u32 | total block count of all bins |

**Entry table (row 253)** — one 0x24-B record per bin, at `+0, +0x24, …`:

| off | type | field |
|---|---|---|
| +0x00 | u32 | bin size in **512-B sectors** (`nblocks·256`) |
| +0x08 | u32 | **map row** — the row holding this bin's log2phy map |
| +0x14 | char[15] | bin name, NUL-terminated, matched by strcmp (`"PROG"`, `"codepage"`); no match is fatal |

**Per-bin map (at the entry's map row)** — one u32 per 128-KiB chunk *j* of the bin:

```
{ u16 origin_physical_block ; u16 backup_physical_block }
```

The loader reads chunk *j* from `origin`; `backup` is the second (RB/UB) copy,
consulted only when the origin block is marked bad in the ASA bitmap (Observed on
a live pen: consecutive chunks map to origins 2 apart — the dual-copy pairing).
With a single-copy image, set **both u16 = the same physical block** and never 0.

Factory-side equivalent (Observed from the factory programmer, page 61/62 forms):
bin-info records are `{u32 len_bytes; u32 load_addr; u32 map_page; u32 start_block;
u32 0; char name[16]}` with a `{3,0,0,nbins}` header page — same stride and name
offset; the field-for-field correspondence to the row-253/254 form is not fully
reconciled (§8). Emulators should serve the row form above.

### 2.4 System-bin region

- **PROG** (main firmware, `0x380000` B = exactly 28 blocks) then **codepage**
  (font/character data, `0xd6ccc` B → 7 blocks), laid **flat** (no relocation, no
  compression) from the bin start block. The runtime addresses this region through
  the **linear** resolver: physical = start + logical, no wear-levelling
  (Observed: the device flags word has the wear bit clear).
- Factory pens hold each chunk twice (RB/UB); a single copy with a maplist whose
  two u16 agree is equivalent on a bad-block-free chip (Inferred, boots Observed).

### 2.5 ASA (Anyka Special Area) — factory pens only

Blocks 1..5 (+1 tracked spare): replicated bad-block tables. Per block: page 0 =
header `"ANYKAASA"` + a small directory, pages 1.. = bad-block bitmap (one bit per
block, MSB-first within each byte) ×2 copies, last page = header copy; every page's
spare u32 = a running copy/"times" counter — the mounter picks the replica with the
highest counter. Consumed by the boot loader's bad-block fallback (§2.3) and the
self-update path. **An emulated chip with no bad blocks boots without any ASA**
(Observed); model it only if emulating factory programming or self-update.
**Caveat (Observed):** omitting the ASA *blocks* satisfies only the boot loader —
the mount still consumes the bad-block **bitmap via row 2** (§2.1) and loops
forever on an erased answer; always serve a zero-filled page at row 2.

---

## 3. The two partitions: A: (system) and B: (user)

The FS area is **one** NFTL medium; partitions A and B are contiguous sector
ranges carved from it:

- `part_A = [0, 8·AddrCnt_A)` sectors, `part_B = [8·AddrCnt_A, 8·(AddrCnt_A+AddrCnt_B))`.
- **B's base = A's span** — the Zone_Group StartAddr field is ignored; contiguity
  is by construction (Observed in the mount code).

| | physical blocks (fs_start=134) | span | drive | role |
|---|---|---|---|---|
| **A:** Symbol 0, Type 2 UNSTANDARD | 134–613 | 480 blocks (AddrCnt 0x3C00 AU) | drive 0, `a:` | **SYSTEM** |
| **B:** Symbol 1, Type 4 STANDARD | 614–1637 | 1024 blocks (AddrCnt 0x8000 AU) | drive 1, `b:` | **USER — the `.gme` games live here** |

Make no mistake about the split (Observed, three independent ways — mount order,
USB LUN wiring, path constants):

- **A: = system drive.** Holds `VOIMG/` (system voice archive), `Language/`
  (update/battery prompt WAVs), `SYSTEM/profile.dat`, the firmware log, and the
  discovery-written index **`a:/oidfilelist.lst`**. Never visible over USB in
  normal operation. If A's FAT fails to mount, the firmware **formats A in place**
  (and hangs only if even that fails) — A is the mandatory medium the pen creates
  for itself.
- **B: = user drive.** The USB mass-storage LUN exposes **only partition B** to
  the PC (volume label `tiptoi`), so user-copied `.gme` files land on B: by
  construction; firmware updates (`*.upd`) and `LanguageInfoMT.txt` are also
  dropped there. The firmware never auto-formats B — a failed B mount just sets an
  error flag and drive `b:` stays unregistered. Nothing in the boot path ever
  writes B:.
- **Game discovery** scans `B:\` recursively for `*.gme` first, then repeats the
  scan on A:, appending full paths to `a:/oidfilelist.lst` (written on A:). So a
  game on either drive is found, but B: is the authentic home.

Each partition contains a **bare FAT16 volume at its partition-relative sector 0**
(§5). The FAT layer sees only the NFTL logical sector space — no tags, no ECC, no
block boundaries.

---

## 4. The NFTL layer

Between the raw blocks of the FS area and the FAT volumes sits a flash-translation
layer. Its **entire persistent state is the per-page 8-byte spare tags**; every
RAM structure (maps, chains, free pool) is rebuilt from tags at each mount.

> **★ The NFTL logical block is 1 MiB = 8 physical blocks (Observed in emulation —
> this closed the historical "B: enumerates 0 entries" issue).** The mapping unit
> is a **1-MiB logical block** spanning 8 consecutive physical 128-KiB blocks.
> Row addressing inside a logical block is **linear across the whole span** under
> the span's *base* physical block: rows `base<<8 | 0 .. base<<8 | 255` — the unit
> byte reaches **255**, and the controller decode carries rows past unit 31 into
> physical blocks `base+1 .. base+7` (the row "carries" from `base<<8|0xFF` into
> the next block; `nand-and-nfc-controller.md` §4). Head tags carry the **1-MiB
> block number**: for a span with base physical block `base`,
> `logical = (base − fs_start) / 8`. Only the span **base** is a chain head; the
> read resolver reaches the other 7 blocks by the linear offset within the span,
> so they need no head tag of their own. Placed spans therefore carry their tags
> in the *base* block's row window; and an emulator's tag store must be keyed by
> the **raw row** `block<<8 | unit` — a flattened sector base aliases a span's
> unit-32.. tags onto neighbour blocks' page-0 tags and destroys the span.
>
> **The 7 follower blocks must be reserved, not left free.** `mtd_init` reads one
> page-0 tag per *physical* block; a follower's page-0 row is blank, so it reads
> **free** and the COW allocator hands it out — overwriting the placed span's
> static content the moment the firmware writes the partition. On **A:** (written
> every boot: the log, `SYSTEM/profile.dat`, `oidfilelist.lst`) that corrupts the
> factory voice/prompt files; B: is never written, so its followers are harmless
> either way. Record every follower in the **factory bad-block bitmap** (row 2,
> §2.1): `mtd_init` then classifies it *reserved* — kept out of the free pool and
> out of the head→map inversion — while the resolver still reads its content by
> offset from the span base. (Giving a follower its own `[4:6]=logical` head tag
> does **not** work: it collides with the base at that logical number and the
> mount's duplicate-head arbitration frees one of them.)

### 4.1 The spare tag (8 bytes, one per program unit)

| bytes | field | semantics |
|---|---|---|
| [0] | seq | `seq & 0x7f` = version counter (ordering/display only); **bit 0x80 = this page's copy is OBSOLETE** — a fold treats such a page as blank and discards its data |
| [1] | sector index | index within the program unit; **not load-bearing** — write 0 |
| [2:4] | **chain-next / status** | `0xfffd` = chain end (single/last block — a valid head with no older copy); a value `< 0x8000` = physical block number of the next-older chain member; `0xffff` = FREE/erased; `0xfffa` = NFTL-internal metadata page; `0xfffc` = bad; `0xfff7` = strong-danger |
| [4:6] | **logical block \| 0x8000** | low 15 bits = the **LOGICAL block number** (medium-relative, in **1-MiB units** — `(base − fs_start)/8`, see the box above); bit 15 = head-valid. The mount scan inverts this into `map[logical] = physical span base` |
| [6:8] | own/id | **ignored by every consumer** (the firmware writes `0xffff` here) |

Load-bearing for a static image: `[2:4]`, `[4:6]`, and bit 7 of `[0]` (must be
**clear** on placed content, or the first garbage-collection fold silently discards
it). `[1]` and `[6:8]` are don't-cares. (Observed: all four tag-writing paths and
all scan consumers traced; the `[4:6]=logical` split additionally confirmed by the
live scan building both partitions from it.)

### 4.2 Mount-time scan

`mtd_init` reads the **head tag of every logical block** in the partition range
(with the 1-MiB logical block of the box above, that is one tag per 8-block span,
read at the span base's row window — placed spans carry tags only there, and the
mount accepts this; Observed under emulation):

- tag `[2:4] == 0xffff` (blank) → the block joins the **free pool** (COW
  allocations draw from here);
- `0xfffa` → NFTL metadata (a persisted-map cache; erased and rebuilt each mount —
  never needed in a fresh image);
- `0xfffc`/`0xfff7` → bad/danger bookkeeping;
- otherwise the block is *used*: a block is a **head** iff no other block's
  `[2:4]` points at it; heads are inverted into the logical→physical map via
  `[4:6] & 0x7fff`. Two heads claiming one logical block are arbitrated by chain
  length (longer wins, loser freed).

**A fresh/erased FS area is a legitimate state**: all-0xFF data + blank tags = an
empty medium with a full free pool. Tags are written lazily, by the first write to
each block. (This is exactly what the factory format step leaves behind — it only
erases.)

### 4.3 Writes: append and copy-on-write

The FAT layer's 512-B sector writes arrive as 2-KiB-page programs. For logical
block L, page p:

- **Append**: if some block of L's chain still has page p (and everything above
  its write pointer) erased, the page is programmed there in place. Tag:
  `{[0]=0, [2:4]=that block's existing chain-next, [4:6]=L, [6:8]=0x0001}`.
- **Copy-on-write**: if every chain block already programmed page p, a fresh block
  N is popped from the free pool and page p is written there with tag
  `{[0]=old_seq+1, [2:4]=H (the old head — making N the new head), [4:6]=L}`
  (`|0x8000` and `[2:4]=0xfffd` when L had no head). If p > 0, page 0 of N is
  first stamped with the same tag `|0x80` (obsolete marker) so the next scan can
  classify N. **No erase happens** — the old head H stays in the chain serving all
  pages ≠ p.
- **Fold** (chain > 10 blocks, low free pool, or read-danger): all live pages of
  the chain are merged into one fresh block (per page, tag
  `{[2:4]=0xfffd, [4:6]=L|0x8000}`; in-flash **copy-back** is used where
  eligible), then every old chain block is **erased** and returned to the free
  pool.

Consequences for an image builder:

1. The identity mapping `logical L ↔ physical span [fs_start+8L, fs_start+8L+8)`
   (L in 1-MiB units) created by static
   placement holds **only until the first COW write to L** — after that L's head
   is wherever the allocator put it. This is why an emulator must model the flash
   at the physical program/erase level (`nand-and-nfc-controller.md` §10 —
   overlay + erased-set + copy-back, keyed by physical row) and must **not** try
   to track logical content: the firmware maintains all NFTL state itself through
   those seams, and its own writes then round-trip and rescan correctly.
2. Every block not deliberately placed **must read fully erased** (0xFF data,
   blank tag) — those blocks are the free pool the firmware writes into.
3. Placed content must carry a valid head tag on **every** written program unit
   (`[2:4]=0xfffd`, `[4:6]=logical|0x8000` with logical the **1-MiB block number**,
   seq bit 7 clear), keyed at the unit's raw row `base<<8 | unit` in the *span
   base's* row window (box above; recipe in §6 step 5) — a blank tag would
   classify the span as free and its content would be ignored *and eventually
   overwritten*; rows of a placed span beyond the placed content must read
   blank (the append probe scans for them).

### 4.4 Linear resolver

The pen's chip flags select the **linear** block resolver for the whole FS area
(no wear-levelled zone map): partition-relative block → absolute block is a pure
offset add. All wear behaviour comes from the tag/chain machinery above, not from
an address-scrambling layer.

---

## 5. The FAT filesystems

Each partition holds a **bare FAT16 "superfloppy"**: the VBR/BPB at
partition-relative sector 0. **No MBR, no partition table** inside the partition —
this is the format the firmware itself writes when it formats A, and the reason a
real pen enumerates on a PC as `sda` with a filesystem directly on it. (An
MBR-at-0 + FAT-at-LBA image is *also* accepted via a fallback parse, but it is the
non-native layout — don't use it.)

The mount's sector-0 checks (Observed, instruction-level):

| requirement | detail |
|---|---|
| dispatch heuristic | u32 at VBR offset **0x1c6** must be `0` or `> 0x100` (values 1..0x100 force the MBR-only parse and the superfloppy is never tried). The firmware's own formatter explicitly zeroes these 4 bytes; **zero them in built images** |
| FS type label | `"FAT"` at offset 0x36 (FAT12/16) or 0x52 (FAT32) — the *only* superfloppy-path check |
| bytes/sector | u16 at 0x0b must be **512** (hard reject) |
| cluster count | must be **≥ 4085** (FAT12-sized volumes rejected). Pick sectors/cluster accordingly: ≤ 8 for a 30 MiB volume, ≤ 16 for 64 MiB; 4 is safe for both |
| 0x55AA at 0x1fe | not checked on the superfloppy path, but keep it (MBR fallback, USB hosts, convention) |

### 5.1 A: content (system)

A:'s factory content is the firmware container's own **`to_udisk` payload** — the
7 files the pen's updater writes to A: after reformatting it (records at
container header +0x28/+0x2c = directory map, +0x30/+0x34 = file TOC; the payload
sits in the container tail, remapped `voice\…` → `VOIMG/…`, `Language\…` →
`Language/…`). Extract it straight from the container and drop it onto A:. All of
it is **optional for booting** (each has a graceful fallback: missing FAT →
auto-format, missing voices → silence, missing prompts → skipped):

```
A:/VOIMG/Chomp_Voice.bin        system-voice archive: u32 offset table [0..48]
                                (word[0]=0xC4 = table size, word[48] = file size)
                                + 48 concatenated RIFF/WAVE (PCM mono 16 kHz 16-bit)
                                — every beep/jingle/system prompt
A:/Language/Update{DUTCH,FRENCH,GERMAN}.wav        "update running" prompts
A:/Language/BatLowUpdate{DUTCH,FRENCH,GERMAN}.wav  "battery low" prompts
```

Created by the firmware itself at runtime (leave room; do not pre-create):
`A:/SYSTEM/profile.dat` (0x21B0-byte settings record), **`a:/oidfilelist.lst`**
(the discovery index of all found `.gme` paths, rewritten each boot; vestigial
`musiclist.lst`/`voicelist.lst` siblings), `A:/Firmware log file.bin`.

An **empty formatted A:** (or even an erased A: — the firmware formats it) boots
and discovers games; the authentic files only add audible system feedback.

**Long file names need VFAT LFN entries.** The firmware opens the voice bank and
the prompts by their exact long paths (`A:/VOIMG/Chomp_Voice.bin`,
`A:/Language/UpdateGERMAN.wav`), and its directory search matches the 8.3 short
name *only* when the query already fits 8.3 (case-insensitively). A name that is
too long, mixed-case-but-over-8.3, or has spaces therefore needs a run of VFAT
long-name entries (attr `0x0F`, 13 UTF-16 units each, stored highest-sequence
first before the 8.3 entry, carrying the 8.3 checksum) plus a unique `NAME~n`
short alias — without them every such `fs_open` misses the name and returns −1
even though the file's data and FAT chain are present and correctly resolved.

### 5.2 B: content (user)

```
B:/<name>.gme                   the game file(s) — this is what the pen plays
B:/LanguageInfoMT.txt           optional: language table (update prompts, names)
```

Volume label `tiptoi` (what users see on the PC). B: is where the PC-visible USB
drive lives and where firmware updates are dropped; the firmware treats it as
read-only outside USB/update sessions.

---

## 6. ★ Emulator recipe — building a bootable image

Inputs: the boot blob/SPL (`0x7e80` B), `PROG` (`0x380000` B), `codepage`
(`0xd6ccc` B) — see `memory-map-and-boot.md` for where these come from — plus a
host directory of A: files (may be empty) and one of B: files (the `.gme`(s)).

Constants used below (Inferred placement, Observed to boot):
`BOOT_BLOCK=1`, `SYS_START=8`, `FS_START=134`, `A_BLOCKS=480`, `B_BLOCKS=1024`,
`AU_PER_BLOCK=32`, `BLK=0x20000`.

**Step 1 — base image.** 4096 blocks × 128 KiB of `0xFF`; all tags blank
(`FF×8`). Sparse/lazy storage is fine — unwritten means erased.

**Step 2 — boot blob.** Place the SPL at block `BOOT_BLOCK`, padded with 0xFF.
(If emulating the mask ROM's boot-page reads, serve them from the SPL bytes
directly; the mask-ROM page format is out of scope here.)

**Step 3 — system bins.** Place `PROG ‖ codepage` flat from block `SYS_START`:
PROG → blocks 8–35, codepage → blocks 36–42. Tag every placed page of block *b*
in this range with the identity head tag
`{seq=1, [1]=0, [2:4]=0xfffd, [4:6]=(b−8)|0x8000, [6:8]=b}`.

**Step 4 — FAT images.** Build two FAT16 superfloppies (e.g.
`mkfs.fat -F 16 -s 4 -R 4 -r 512 -f 2 -n <label>` + `mcopy -s` of the host dir):

- A: total 60 MiB (≤ `A_BLOCKS` blocks — hard limit), label anything, content §5.1;
- B: e.g. 64 MiB (≤ `B_BLOCKS`), label `tiptoi`, content §5.2;
- **zero the u32 at VBR offset 0x1c6** in both; verify cluster count ≥ 4085.

**Step 5 — place the FAT images.** A: at block `FS_START` (VBR = block 134,
sector 0), B: at block `FS_START + A_BLOCKS` (= 614). For each 128-KiB slice of an
image: if it is all-0x00 or all-0xFF, **skip it** (an unallocated cluster on a
real pen is erased with no tag; FatLib never reads unallocated clusters, and the
free pool needs those blocks). Otherwise store the slice at its physical block *b*.
**Tagging (corrected — Observed working; §4 box):** tags follow the **1-MiB span**,
not the physical block. For a written AU at physical block *b*, AU index *a*, let

```
base = FS_START + 8·⌊(b − FS_START) / 8⌋      # span base block
unit = 32·(b − base) + a                       # 0..255, AU offset in the span
```

and write the head tag `{seq=1, [1]=0, [2:4]=0xfffd,
[4:6]=((base−FS_START)/8)|0x8000}` keyed at the **raw row** `base<<8 | unit`.
The logical number is the **1-MiB block index** relative to `FS_START` for
**both** partitions (B's first span, base 614, is logical 60) — exactly what the
identity rule produces. (The pre-correction recipe — per-physical-block tags
`(b−FS_START)|0x8000` on every written page — made the mounted B: enumerate 0
entries and FS writes mis-scan; do not use it.)

Only the span **base** block carries a head tag. **Record every placed
follower block** (`base+1 … base+7` of each placed span) in the row-2 bad-block
bitmap (step 6) so the mount reserves it instead of recycling it into the free
pool (§4 box) — this is what keeps A:'s factory content intact across the
firmware's own boot-time writes.

**Step 6 — metadata rows** (0x1000-byte payloads, zero-filled unless stated):

- **Row 2 — factory bad-block bitmap:** 0x1000 bytes, 1 bit per block MSB-first,
  **set = withheld from the free pool** (§2.1). Mandatory: leaving it erased
  (0xFF = every block bad) makes the mount loop forever. Zero every bit **except**
  the placed spans' follower blocks (step 5, §4 box), which are set so the mount
  reserves the static content instead of COW-recycling it.
- **Row 255 — zone/partition table:**

  ```
  u32 @0x000 = 0x200            TotalLen / offset of the fake-zone region
  u32 @0x004 = 134              fs_start_block
  u8  @0x00a = 2                nzones
  u32 @0x014 = 0x3C00           Zone_Group[0].AddrCnt  (A = 480 blocks · 32 AU)
  u8  @0x01b = 0                Zone_Group[0].Symbol   ('A')
  u32 @0x024 = 0x8000           Zone_Group[1].AddrCnt  (B = 1024 blocks · 32 AU)
  u8  @0x02b = 1                Zone_Group[1].Symbol   ('B')
  u16 @0x200 = 1                fake_group_count
  u16 @0x202 = 6                record len (= 2·1 + 4)
  u16 @0x204 = 0                zone_idx
  u16 @0x206 = 0                groups[0]: reserved/bad blocks (none)
  ```

  (Optionally fill Type/flags bytes per §2.2 for fidelity; the mount matches on
  Symbol and reads AddrCnt + fs_start + the fake map only.)

- **Row 254 — bin header:** `u32@4 = 2` (bins), `u32@8 = 8` (start block),
  `u32@0xc = 35` (total blocks).
- **Row 253 — bin entry table:** record 0 (offset 0): `u32@0 = 28·256`,
  `u32@8 = 200` (map row), `"PROG"` at +0x14; record 1 (offset 0x24):
  `u32@0 = 7·256`, `u32@8 = 201`, `"codepage"` at +0x14.
- **Rows 200/201 — maplists:** row 200 = 28 entries `{u16 8+j, u16 8+j}`;
  row 201 = 7 entries `{u16 36+j, u16 36+j}`. Both u16 identical (single copy);
  never 0.

**Step 7 — runtime write model.** Implement program/erase/copy-back layering per
`nand-and-nfc-controller.md` §10: programs overlay static content; an erase
**shadows** static content and tags (must win — otherwise a recycled placed block
resurrects stale FAT sectors and the scan sees two heads for one logical block);
copy-back moves data+tag verbatim. That is all — every higher NFTL/FAT structure
is firmware RAM state rebuilt through these seams.

Boot outcome: the boot loader finds PROG via rows 254/253/200, the mount reads
row 255, builds the medium over blocks 134–4095, and registers **both** drives —
A (`a:`) and B (`b:`) — when both FAT images are valid (Observed under emulation).
The firmware's own writes (log, profile, `.lst`) COW into free blocks and read
back consistently (Observed). B: enumeration requires the 1-MiB-span tagging of step 5
(and raw-row tag keying); with it, B: enumerates and plays placed content — the `.gme`
can live on B: alone (Observed; §7.5). The full runtime chain and its failure
signatures are in §7.

---

## 7. ★ Runtime: mount → discovery → tap — how a book actually starts

Building the image (§6) is necessary but not sufficient: the firmware only plays a
book after a specific runtime chain has run over that image. This section is the
behavioural contract the emulator must let happen (all firmware-side — the emulator
presents hardware and taps, nothing else). Statechart state numbers follow the boot
doc's convention: 1 = splash, 3 = standby, 12 = mount, 13 = book.

### 7.1 The mount: what registers drives `a:` and `b:` (Observed)

`fs_storage_mount_init` (from `app_init_main`), in order:

1. Reads the zone table (row 255) and the factory bad-block bitmap (row 2 —
   must answer **zero-filled**, not erased; §2.1) and builds the ONE FS-area
   medium over `[fs_start, 4096)`.
2. Carves partition A = sectors `[0, 8·AddrCnt_A)` and B = `[8·AddrCnt_A,
   8·(AddrCnt_A+AddrCnt_B))` from the medium (AddrCnt in AU; §2.2).
3. **Loads the codepage bin** (by physical row, below the FS area). A valid load is
   a hard mount precondition (empty answer → mount-failure branch → LED-blink
   hang). It matters *twice*: the loaded NLS tables also perform every later
   **ANSI→UTF-16 device-path conversion** — a garbled codepage load produces
   garbled paths and every subsequent `open`/`opendir` fails *silently* (§7.4).
4. **FAT-scans A** at partition-relative sector 0 (§5 rules). On failure the
   firmware **formats A in place** and proceeds. Registers the volume as
   **drive 0 = `a:`**.
5. **FAT-scans B.** On failure it only sets an error flag — **B is never
   auto-formatted, and drive 1 = `b:` stays unregistered**; the boot continues
   normally with no other symptom.

So **B: mounts iff** the image provides: a Symbol-1 `Zone_Group` record with a sane
AU `AddrCnt` (§2.2), and a valid FAT16 superfloppy VBR at B's partition-relative
sector 0 = physical block `fs_start + AddrCnt_A/32` (= 614 with the §6 constants),
whose placed spans carry valid 1-MiB head tags (`logical = (base − fs_start)/8`,
i.e. B's first span, base 614, is logical 60 — §4 box, §6 step 5).

**Signature of an unregistered `b:`** (Observed mechanism): path resolution maps
`toupper(path[0]) − 'A'` into the registered-drive array and rejects out-of-range
indices **before any device I/O** — so *zero reads in B's physical block range,
ever*, is what "B: didn't mount" looks like. It is not a scan that skipped B:; it
is every `B:/…` open/opendir being rejected at the drive-letter check.

### 7.2 Game discovery: autonomous, at standby entry, once per boot (Observed)

Discovery is **not** triggered by a tap. It runs inside the **standby(3) entry
action** when the statechart transitions splash→standby (once per boot — re-entering
standby from book/USB does not re-run the entry hook). The entry action, in order:

1. Sets the game-context byte `+0x1d = 2` (context base `0x080089a4`; the "fresh
   standby" marker, §7.3).
2. **Unconditionally** allocates the 0x240-byte booklist iterator, stores its
   pointer at **`0x081da080`** ("booklist head") and zeroes it.
   → *Diagnostic:* `*0x081da080 == 0` means **the standby entry action never
   executed at all** (the run never truly entered standby via the statechart);
   non-zero with count 0 (u16 at iterator +0) means discovery was skipped or
   found/wrote nothing.
3. Opens/creates serial + log files on A: (real FAT writes — A: must be mounted
   and writable through the normal write path).
4. **Gate:** if game-context byte `+0x1e != 2` (the USB-session marker; it stays 0
   when GPIO8 = 0), runs the discovery scan:
   - opens **`a:/oidfilelist.lst`** read/write, creating it if absent;
   - recursively enumerates root **`"B:"`** for `*.gme` (wildcard applied by the
     FAT enumeration; subdirectories recursed, `./`/`../` skipped), then patches
     the root's drive letter to `'A'` and enumerates **A:** the same way;
   - writes a 0x424-byte header + one **0x214-byte record per file** — 3
     bookkeeping u32s + the UTF-16 absolute path (`L"B:/name.gme"` /
     `L"A:/name.gme"`), nothing else (no product-id, no OID data);
   - sets the booklist count from the **in-RAM** record counter, flushes, and
     **keeps the file handle open** — every later record fetch is a real
     `seek(0x424 + i·0x214)` + `read(0x214)` on that same handle.

Properties that matter for the emulator:

- The `.lst` is a **rebuilt-per-boot cache**. The tap-time mount consults *only*
  this boot's scan; a pre-existing `.lst` on disk never supplies the count. The
  **write and the read-back are both on the critical path** — the whole chain runs
  through the real FAT/NFTL write model.
- **Silent degradations** (no crash, no error output): `.lst` unopenable → the
  scan runs in a probe mode that writes nothing (count 0); `b:` unregistered → the
  B: pass no-ops (§7.1); no `.gme` found → count 0. Count 0 surfaces only later,
  as error voice 0x2D on the mount tap.
- A second discovery site exists in the standby idle handler: **GPIO11 == 1**
  triggers a rescan **plus a soft reboot** — keep GPIO11 = 0 *after boot* (see
  the GPIO doc). GPIO11 is the power button; at the *boot-time* app-init sample
  it should read 1 — that is the authentic normal-boot descent, §7.3.1a.

### 7.3 The tap sequence: product, product, content (Observed)

> **Note:** this three-tap sequence describes the *tap-at-standby* route. A real
> pen normally never takes it — it auto-descends to book(13) at every power-on via
> the `+0x24` latch and needs only **two** taps (mount + content). See §7.3.1a for
> the resolved normal-boot mechanism and what the emulator should model.

Loading and playing a book takes **three taps** (values read off the GME file
itself, so this works for arbitrary GMEs):

- **product OID** = the u32 at GME header offset **0x14** (must be ≤ 0x3E7 = 999);
- **content OID** = any OID in the GME's script range (e.g. its first script OID).

**Tap 1 — product OID at fresh standby → book mode (no mount yet).** The global
classifier's *first-load* branch fires for any tapped code ≤ 0x3E7, gated on five
bytes of firmware-produced state (`akoid_buf` = the OID working buffer, pointer at
game-context `+0x20` — see the OID sensor doc):
`akoid_buf[0] == 1` (pen-down, set by the decode), game-context `+0x1d == 2` (set
by standby entry — **but see §7.3.1: the firmware itself flips this byte to 8 on
the first standby heartbeat, so it does *not* "arise naturally" at tap time**),
`akoid_buf[0x21] == 0xFF` ("no product loaded", set by the OID
subsystem init and *preserved* only if the boot never runs the spurious root-exit —
the frame-seed's purpose), the OID capture-state byte `0x08008C0D != 0xFF`
(firmware-initialised), plus `akoid_buf[0xB3] == 0` (production-test flag; stays 0
unless the test-mode button chord is held). It posts events 0x104A + 0x1058 →
**state 12** (its entry
scans `B:/` for `*.bnl` retail archives — an empty result is harmless) → 0x1059 →
**book(13)**. Book entry zeroes the script context, clears `akoid_buf[0x21]`, sets
game-context `+0x1d` back to 2, and
plays the book-open system voice. **The GME is not mounted by this tap.**

If the gate *fails*, the tap is **silently dropped**: the classifier passes the
event through, the standby leaf's handler does not accept event 0x1060 (it is not
in the small event set that handler reacts to), and nothing else consumes it — the
statechart simply stays in standby(3). A run where taps decode perfectly
(`akoid_buf+4` gets the OID) but the pen never leaves standby is this gate failing,
and in practice the failing byte is `+0x1d` (Observed — both in the reference
emulation and reproduced independently).

**Tap 2 — the same product OID, in book(13) → the real mount.** The classifier
gate now fails (`[0x21] == 0`), so the event reaches the book handler's OID
dispatch: product band → **linear probe over the booklist** — for each recorded
path: open the file; accept iff magic **0x238B at +0x08**, language field at
**+0x59** matches the pen language (an all-zero field passes; default GERMAN), and
product-id at **+0x14 == the tapped value**. On success the handle is kept (ptr at
`0x08121ed0`), the header is parsed (current product → `0x081da08c`, content-OID
range, media XOR key from hdr@0x71, cover selectors from hdr@0x94). On a count-0
or no-match booklist: error voice 0x2D.

**Tap 3 — a content OID → the script runs and media plays.**

### 7.3.1 The `+0x1d` standby window — why tap 1 needs help

Game-context byte `+0x1d` (base `0x080089a4`, i.e. `0x080089C1`) is the
"fresh-standby" marker the tap-1 gate tests. Its lifecycle (Observed):

- **Standby entry writes 2.**
- The standby leaf's event handler accepts a small set of housekeeping events
  (the periodic **0x1046 heartbeat** among them — the global heartbeat handler
  deliberately lets it through to the current leaf). On the **first** accepted
  event after standby entry, when the USB-session byte `+0x1e != 2` (it is 0 on
  battery) and the resume byte `+0x24 == 0` (see §7.3.1a — on a real pen this
  byte is normally **1**), the handler
  re-arms the sensor and **sets `+0x1d = 8`** ("idle mode"). Every *further*
  accepted heartbeat increments the ~300-count auto-off counter. **Nothing inside
  standby ever sets `+0x1d` back to 2.**
- With the heartbeat at ~100 ms, the `+0x1d == 2` window after standby entry is
  therefore **one heartbeat wide (≤ ~100 ms of emulated time)**. Any realistically
  paced tap arrives at `+0x1d == 8`, fails the gate, and is dropped (see above).
  This is a property of the unmodified firmware, not an emulator timing bug —
  reordering event delivery cannot widen the window. A physical pen never needs to
  win this window, because on a normal power-on it does not idle at standby at all —
  it auto-descends to book(13) via `+0x24 == 1` (§7.3.1a). The one-heartbeat window
  only matters on the `+0x24 == 0` boot shape the emulator reproduces.

**What the reference emulation does (Observed, proven end-to-end):** present
`+0x1d = 2` **at the classifier's first-load gate** — concretely: when execution
reaches the gate's pen-down test (instruction address **`0x080380F4`**) with
`akoid_buf[0] != 0` (a tap is actually being classified) **and** the current
product id `*0x081da08c == 0` (nothing mounted), write game-context `+0x1d = 2`.
This is a one-byte state presentation, scoped so tightly it is inert everywhere
else: at tap 2 / in-book / in-game taps the byte is already 2 (book entry set it)
and the gate outcome is decided by `akoid_buf[0x21]` anyway.
**Never touch `akoid_buf[0x21]`** — it is the tap-1/tap-2 discriminator (0xFF at
standby makes tap 1 open book mode; 0 at book(13), written by book entry, makes
tap 2's gate *fail*, which is exactly what routes the event to the OID dispatch
and the mount). Forcing `[0x21] = 0xFF` per-tap breaks the tap-2 mount (Observed).

Alternatives, for the record:

- *Win the race*: arm the tap frame **before** standby entry (pen already on the
  page during boot). The 40 ms OID poll can then decode and post 0x1060 ahead of
  the first ~100 ms heartbeat. Authentic, but a phase race — fragile under model
  timing changes (Inferred).
- *Resume descent*: ship **`B:/FLAG.bin`** in the image. Splash then sets
  `+0x24 = 1`, and at standby the first accepted heartbeat posts 0x1058 itself —
  the pen descends to book(13) **autonomously, with no tap and no `+0x1d` gate**
  (this is real-pen-observed post-update behaviour). Tap 1 becomes unnecessary;
  the sequence shrinks to product-tap (mount) + content-tap. — This is the same
  descent a normal boot takes anyway (§7.3.1a); FLAG.bin is just the second,
  post-update setter of the same `+0x24` byte.

### 7.3.1a How a real pen normally reaches book mode (no FLAG.bin)

The `+0x24` "resume" byte has **two** authentic setters, and the second one fires
on essentially **every** physical power-on:

1. **Splash's `B:/FLAG.bin` branch** (post-firmware-update resume) — the path the
   emulator uses.
2. **The early app-init latches `+0x24 = (GPIO11 == 1)`** right after configuring
   the power-hold pin. **GPIO11 is the power button.** On a physical power-on the
   user's finger is still on the button when this sample runs (a fraction of a
   second after power comes up — the same press that powered the SoC), so a real
   pen boots with `+0x24 = 1` virtually every time. (Observed: firmware disasm of
   the app-init latch; confirmed by a live-pen RAM dump showing `+0x24 == 1` on a
   boot with no FLAG.bin.)

Consequence — the authentic normal-boot chain, end to end:

```
power-on (button held) → app-init latches +0x24=1 → splash → standby entry
  (writes +0x1d=2, allocs booklist, runs the discovery scan → oidfilelist.lst)
→ first accepted standby heartbeat (~100 ms): handler sees +0x24==1
  → posts 0x1058 itself → book_mount(12) → 0x1059 → book(13)  [power-on jingle]
→ book entry: +0x1d=2, akoid_buf[0x21]=0, capture on
→ FIRST physical tap of a product OID → OID dispatch product band
  → linear probe over this boot's booklist → GME mounts        [one tap!]
→ content taps play.
```

So the real pen **never idles at standby waiting for tap 1**: the `+0x1d`
one-heartbeat window (§7.3.1), the classifier first-load branch, and the
"three-tap" sequence of §7.3 are all properties of the `+0x24 == 0` boot shape —
which a physical pen only exhibits if the button is released before the early-boot
sample (in which case it idles ~30 s at standby, unresponsive to taps, and powers
off). The familiar real-pen behaviour — jingle at power-on, first cover tap plays
the book — is exactly the `+0x24 == 1` descent.

**Emulator implication (the authentic replacement for the FLAG.bin shortcut and
the scoped `+0x1d` write):** present **GPIO11 = 1 during the app-init sample**
(power button held through boot) and **release it (0) afterwards**. The release
matters: later, in standby idle mode, the same GPIO11 read means "power button
pressed" and triggers a discovery **rescan + soft reboot** (this is the §7.2
"keep GPIO11 = 0" rule — it applies *after* boot, not to the boot-time sample).
With that one pin modeled, the unmodified firmware auto-descends to book(13) on
every boot and needs only product-tap (mount) + content-tap.

Two adjacent findings, for completeness (both Observed in firmware code):

- **Discovery is NOT gated on prior USB activity.** The standby-entry scan runs on
  every cold boot; its only suppression is `+0x1e == 2`, which means "a PC/USB
  session is being entered / active *right now*" (set by the USB-detect/connect
  path, never persisted). There is no "content changed since last boot" flag, in
  RAM or on A:; the persisted `a:/oidfilelist.lst` is never used as a booklist
  source (the count always comes from the fresh in-RAM scan). Content added over
  USB is picked up because a PC session ends with the pen powering off or
  rebooting — the next cold boot rescans unconditionally.
- **"Nobody has ever seen B:" is a naming artifact, not a hidden drive.** The USB
  mass-storage stack exports exactly one LUN, and it **is** partition B: — the
  "tiptoi" drive every user sees on the PC. A: (system: `VOIMG/`, the `.lst`
  indexes, logs) is never exported. "B:" is only the firmware-internal drive
  letter; discovery scans it on every boot regardless of USB history.

### 7.3.2 Pacing (Observed in the reference emulation)

Present taps as discrete, spaced events — pen down, frame served, pen lifted
(GPIO9 back to idle-high) — and re-serve the same frame until the firmware's own
decode has latched it (`akoid_buf+4 == N`, or code word `0x400000|N` at
`akoid_buf+8`) before counting a tap as delivered. Then:

- **Tap 1** (product): only once the statechart leaf is **standby(3)** and the
  boot has settled (reference: system tick ≥ 0x100, or ~2 M emulated instructions
  after IRQ delivery starts on a direct-to-standby boot). With the §7.3.1 gate
  presentation in place there is no upper deadline except the ~300-heartbeat
  auto-off (~30 s emulated).
- **Tap 2** (product again): only once **all three** hold —
  1. the statechart leaf is **book(13)**;
  2. the **book-entry tail has run after tap 1**: book entry plays the book-open
     voice and only *then* clears the pen-down flag and re-enables capture (the
     tail instruction is at `0x08034850` — a convenient program-counter marker).
     A tap presented while the entry voice is still playing gets its pen-down flag
     wiped before dispatch and is lost. This is also where audio matters twice:
     the voice must reach a real **EOF** (deliver DMA-done completions *paced*, on
     the order of thousands of emulated instructions apart — instant back-to-back
     completions can starve the decoder so EOF never comes), because the tap-2
     dispatch **stops audio before mounting** and spins until playback drains;
  3. a settle gap since tap 1's decode (reference: **~3 M emulated instructions**)
     with the pen lifted in between.
- **Tap 3** (content): same conditions relative to tap 2 — leaf still book(13),
  pump idle again after the mount, settle gap elapsed.

### 7.4 Failure signatures — fast diagnosis (Observed mechanisms)

| symptom | meaning |
|---|---|
| `*0x081da080 == 0` at "standby" | the standby **entry action never ran** — the run is not actually in statechart standby (frame seed / timer-IRQ / event-delivery problem), so discovery was never invoked |
| booklist allocated, no `a:/oidfilelist.lst` open ever attempted | discovery gate closed: game-context `+0x1e == 2` (USB marker — check GPIO8 = 0) |
| `.lst` open attempted with garbled path bytes (or fails), 0 reads on **both** drives' data | codepage NLS misload — path conversion broken; verify the codepage load served the real bin bytes (the cached NLS header at `0x081db748` must equal the codepage bin's bytes at file offset 0x2E0) |
| `.lst` created, header only, count 0; **zero I/O ever in B's block range** | drive `b:` unregistered — B FAT invalid/absent at physical `fs_start + AddrCnt_A/32` (§7.1); and no `.gme` was on A: |
| booklist count 0 → tap 2 yields voice 0x2D | discovery ran but wrote no records (combine with the rows above) |
| tap 1 decodes (`akoid_buf+4` = the OID) but the statechart stays at standby(3) | first-load gate failed and the 0x1060 was silently dropped. **Expected cause: `+0x1d == 8`** — the normal steady state one heartbeat after standby entry (§7.3.1); apply the scoped gate presentation. If `+0x1d` reads 2, check `akoid_buf[0x21] == 0xFF` (root-exit clobber → frame seed missing), then `0x08008C0D != 0xFF` / `akoid_buf[0xB3] == 0` |
| tap 2 never reaches the mount | book-entry audio never reached EOF — the pre-mount audio-stop spins (audio completion model) |
| discovery's writes reach NAND but a re-read shows **no new directory entry** (`.lst`/log created "successfully", persists 0 games); or a mounted drive enumerates 0 entries over placed content | NAND-model corruption of the FS write/scan path: 1024-B program records captured only at the final flush (`data[:512]==data[512:]` — `nand-and-nfc-controller.md` §7), or the tag store keyed by a flattened sector base instead of the raw row / per-physical-block head tags instead of 1-MiB spans (§4 box, §6 step 5) |

### 7.5 Verification status

- **Observed end-to-end** (reference emulation): image with the `.gme` reachable on
  **A:** → mount → standby-entry discovery (B: pass no-op, A: pass finds it) →
  booklist ≥ 1 → product-tap ×2 + content-tap → header match → media decodes and
  plays. The reference run keeps exactly **one** non-hardware state presentation in
  this chain: the scoped `+0x1d = 2` write at the classifier gate (§7.3.1). The
  frame is otherwise decoded, classified, routed, mounted and played entirely by
  unmodified firmware.
- **Observed:** two-FAT image (A: system + B: game) → both drives register; B's
  VBR at physical 614 is read and accepted; mount returns success.
- **Observed:** the discovery B: pass enumerates placed B: content when the image is
  tagged as the NFTL expects — 1-MiB-span head tags keyed by raw row (§6 step 5), **not**
  per-physical-block head tags with flattened keys that alias neighbour page-0 tags (§4
  box). With correct tagging the end-to-end chain runs with the game on B: alone. (A `.gme`
  on A: is found through the same path, as on a real pen.)

---

## 8. Gaps / open questions

- **Real-pen values of `fs_start` and the bin start block** — host-chosen at the
  factory, never read off a physical pen; 134/8 are self-consistent choices, and
  the firmware derives everything from the image's own zone table, so this is
  cosmetic until a physical dump is compared.
- **Row ↔ block-0-page correspondence for the metadata** (row 255 vs "page 63"):
  the factory tool addresses pages of block 0; the runtime reads fixed row
  numbers whose decode in the authentic AU row space is unresolved. Serving the
  row numbers works; reconstructing the metadata byte-exactly *inside* block 0
  awaits a physical dump.
- **Bin-info encodings**: the factory page-61/62 record form vs the row-253/254
  form the boot loader consumes are not field-for-field reconciled (plausibly the
  same bytes under the unit question above).
- **Real pen's zone-table AddrCnt bytes**: `0x3C00`/`0x8000` are from a factory
  programmer run; whether a production pen carries the same values (and hence a
  60 MiB A: span under AU units) is unverified.
- **ECC parity bytes and the mask-ROM boot-page format** are out of scope here
  (controller-level; see `nand-and-nfc-controller.md` §6/§9 — an emulator stores
  no parity).
- **ASA and RB/UB dual copies** are documented but not modelled; needed only for
  bad-block or self-update emulation.
