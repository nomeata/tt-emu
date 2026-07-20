# tiptoi 2N ("MT") firmware — application internals for the live debugger view

Unlike the sibling documents in this directory, which describe the pen's *hardware*, this
file describes one specific *firmware*: the 2N "MT" application image (**version N0038MT,
build date 20131009**, shipped in `Update3202MT.upd`), the image the emulator boots. It
specifies the firmware's internal software structures — the QHsm statechart, the GME
script interpreter, and their runtime state in RAM — precisely enough that the emulator's
TUI can render a rich live view (state hierarchy, transition log, interpreter state,
GME `$`-registers, an OID/script debugger) **without modifying or hooking the firmware**:
everything below is read-only polling of RAM plus, optionally, read-only PC watchpoints.

Because these are addresses *inside one firmware build*, the TUI must enable this view
only after positively recognising the image (§1). On any other firmware every address
below is meaningless.

Facts are marked **Observed** (byte-read from the image, read from decompiled/disassembled
firmware code, seen in a live-pen RAM dump, or demonstrated in emulation) or **Inferred**
(deduction; reason given). Unmarked statements are Observed.

Address convention (matches `memory-map-and-boot.md`): all addresses are runtime physical
addresses. The application image ("PROG") is loaded flat at **0x08009000**; image file
offset = address − 0x08009000. The resident boot blob / HAL lives below it at
0x08000000–0x08009000 and stays mapped for the firmware's whole life. Little-endian
throughout.

---

## 1. Firmware recognition / fingerprint

The PROG image **begins with a plain-text version block** — it is the very first thing at
the load address:

| address | bytes | content |
|---|---|---|
| 0x08009000 | `4E 30 30 33 38 4D 54 00 00 00` | `"N0038MT"` + 3 NULs — firmware version id |
| 0x0800900A | `32 30 31 33 31 30 30 39` | `"20131009"` — build date |
| 0x08009014 | `54 69 70 74 6F 69 00 …` | `"Tiptoi"` — product name |
| 0x0800901E | `5A 43 33 32 30 32 4E 00` | `"ZC3202N"` — target SoC |
| 0x08009028 | UTF-16LE | `"tiptoi"` (wide) |

**Recommended fingerprint:** the 26 bytes at 0x08009000 must equal
`"N0038MT\0\0\0" + "20131009" + "\0\0" + "Tiptoi"`. Since the image is loaded verbatim,
the same bytes are at offset 0 of the PROG file/NAND section, so the check can be done at
load time before the CPU runs. **Observed** (image bytes).

Secondary confirmations (belt-and-braces, all Observed):

- ASCII `"Update3202MT.upd"` at **0x080ECF12** (the firmware's default update-file name).
- If identifying an *update file* rather than a loaded image: an `.upd` carries the
  trailer `"ANYKA_ID"` + `"RAV_N0038"` at EOF−0x40; the `N0038` digits are the same
  version id the firmware compares against its own when deciding whether to self-update.
- Runtime sanity check before enabling live reads (confirms boot init has run and the
  image really is at the expected base): read the u32 at **AO+0x18 = 0x0800888C**; it
  must equal **0x08121D44** (the state-descriptor table pointer, §2.2). Until app init
  has run this is 0.

---

## 2. The statechart (QHsm)

### 2.1 Model

The application is a **table-driven hierarchical state machine** (QHsm/QP-style) with
**70 states** (ids 0–69) and a **stack of active states**. Two pseudo-states are
dispatched on *every* event but are never on the stack:

1. **state 9 `global_pre`** — sees every event first; routes the system events
   (heartbeat 0x1046, sw-timer 0x30, OID phases 0x105E/0x105F/0x1060). The decoded-tap
   event 0x1060 goes to the cover/product classifier, which opens books.
2. **the active leaf** — the top of the state stack: the actual UI/game state.
3. **state 10 `global_post`** — sees whatever the leaf left unconsumed.

Application shape — a shallow system layer with one deep content subtree:

```
root(0) ─┬─ splash(1) ── standby(3) ─┬─ book_mount(12) ── book(13) ── [56 game/activity states 14..69]
         └─ fw_update(2)             ├─ usb_detect(8) ─┬─ usb_msc(5)
                                     │                 └─ charging(6)
                                     └─ poweroff_prep(7) ── system_off(4)
```

root(0) → splash(1) → standby(3) are *sibling replacements* of the bottom stack frame, so
after boot the stack bottom is standby(3). A cover tap pushes book_mount(12), which
immediately pushes book(13); in-book taps push one of the 56 game/activity leaves on top
of book. Exit events pop back down. **Observed** (full static decode of all 70 states).

### 2.2 Runtime structures to read live

#### The active object (AO) @ **0x08008874** — statically allocated, fixed address

| field | address | width | meaning |
|---|---|---|---|
| AO+0x00 | 0x08008874 | u8 | scratch: id of the state currently being *dispatched* (cycles 9 → leaf → 10 per event — do **not** use as "current state") |
| AO+0x01 | 0x08008875 | u8 | consumed flag, cleared per hierarchy move |
| AO+0x04 | 0x08008878 | u16 | **stack depth** (0 = only the bottom frame) |
| AO+0x14 | 0x08008888 | u32 | **stack pointer SP** → base of the top frame; `*(u8*)SP` = **current leaf state id** |
| AO+0x18 | 0x0800888C | u32 | → state-descriptor table = **0x08121D44** |
| AO+0x1C | 0x08008890 | u32 | → event-action table = 0x0800B8D4 (68 entries) |
| AO+0x20 | 0x08008894 | u32 | → transition-action table = 0x0800B544 |
| AO+0x24 | 0x08008898 | u32 | **last event id** dispatched |

All **Observed** (code + live-pen RAM; on the live pen: depth 3, leaf 0x45 = state 69).

#### The state stack @ **0x08007E80** (base), frames of **0xC bytes**

`SP` is initialised to 0x08007E80 with state 0 at the bottom. Frame *i* sits at
`0x08007E80 + i*0xC`; the current leaf frame is at `base + depth*0xC` (= SP).

| frame offset | width | meaning |
|---|---|---|
| +0x0 | u8 | **state id** |
| +0x4 | u32 | fn pointer, called when a child is pushed **on top of** this frame (suspend hook) — usually 0 |
| +0x8 | u32 | fn pointer, called when the frame above pops back **to** this frame (resume hook) — usually 0 |

Mechanics **Observed** (dispatch code); the suspend/resume *roles* of +4/+8 **Inferred**
from call sites (called at push-time on the old top frame, and at pop-time on the frame
being returned to).

**To render the current hierarchy:** read `depth` (u16 @0x08008878) and walk
`state[i] = *(u8*)(0x08007E80 + i*0xC)` for `i = 0..depth`. That byte chain *is* the
parent chain, bottom (usually standby 3) to leaf. States 9/10 never appear in it.
Sanity-check `SP == 0x08007E80 + depth*0xC`; if not (mid-transition or pre-init), skip
this refresh.

#### The state-descriptor table @ **0x08121D44**

An array of 70 u32 pointers (in RAM, populated once at app init), one per state id.
Each points to a 0x10-byte descriptor record in ROM (records live in
0x080B0378–0x080B07B8, plus state 9's at 0x0800B55C):

| descriptor offset | meaning |
|---|---|
| +0x0 | entry action fn (run on entering the state; 0 = none) |
| +0x4 | exit action fn (run on leaving; 0 = none) |
| +0x8 | unhandled-event fallback fn |
| +0xC | **mapper** fn — the event→(result,status) router that defines the state's transitions |

**Observed.** The firmware stores **no state names and no static parent links** — the
hierarchy exists only as the live stack. Names and static parentage below are editorial
labels assigned during reverse engineering (**Inferred** as labels; the *behaviour* each
label summarises is Observed).

### 2.3 State id → name table

System / idle / power states:

| id | name | static parent | notes |
|---|---|---|---|
| 0 | `root` | — (initial bottom frame) | boot decision: → 1 or 2 |
| 1 | `splash` | sibling of 0 | battery gate, update-resume check |
| 2 | `fw_update` | sibling of 0 | self-update from B:\*.upd |
| 3 | `standby` | sibling of 1 | idle hub; runs the book-discovery scan on entry; idle >300 heartbeats → inline power-off |
| 4 | `system_off` | pushed by state 10 on 0x1062 | full peripheral shutdown |
| 5 | `usb_msc` | sibling of 8 | USB mass-storage session |
| 6 | `charging` | sibling of 8 | charge animation/counting |
| 7 | `poweroff_prep` | child of 3 | graceful off, posts 0x1062 |
| 8 | `usb_detect` | child of 3 | USB/charger settle & branch |
| 9 | `global_pre` | pseudo (never on stack) | system-event router, OID classifier |
| 10 | `global_post` | pseudo (never on stack) | 0x1062 re-push; fallback trigger |
| 11 | `orphan_overlay` | — | **dead** — unreachable in this build |
| 12 | `book_mount` | child of 3 | transient: scans B:/\*.bnl, posts 0x1059 |
| 13 | `book` | child of 12 | GME book mode; default handler = the OID dispatcher |

Game / activity leaves (children of book 13 unless noted; entered by launch events
0x1015–0x1045 posted by the GME OID dispatcher according to GME game type + product id):

| id | name | id | name |
|---|---|---|---|
| 14 | `st14_gme_script_game` (GME types 1,2,3,5,10) | 42–45 | `prod0b_game_mode0..3` |
| 15 | `st15_study_step` (type 4/40) | 46–49 | `prod09_game_mode0..3` |
| 16 | `st16_study_list_a` (type 6) | 50 | `selection_board_mode` (hub → 51–57) |
| 17 | `st17_study_list_b` (type 7) | 51–57 | `hub_subgame_A..G` (children of 50) |
| 18 | `st18_study_return` (type 8) | 58 | `gme_gametype17` |
| 19 | `st19_study_mode_x` (type 16) | 59 | `gme_gametype18` |
| 20 | `st20_game_step_confirm` (child of 30) | 60 | `gme_gametype16_p2` |
| 21 | `st21_minigame2` (child of 30) | 61 | `gme_gametype19` |
| 22 | `st22_minigame3_linecut` (child of 30) | 62 | `gme_gametype20` |
| 23 | `st23_game4_quiz` (child of 30) | 63 | `gme_gametype21` |
| 24 | `st24_game5` (child of 30) | 64 | `game_reading_prod0e` |
| 25 | `st25_game7_findtarget` (child of 30) | 65 | `game_prod0f` |
| 26 | `st26_game_ask_question` (child of 30) | 66 | `gme_gametype22` |
| 27 | `st27_minigame6` (child of 30) | 67 | `gme_binary_subgame` |
| 28 | `st28_special_game1` (child of 30) | 68 | `gme_gametype23` |
| 29 | `st29_special_game2` (child of 30) | 69 | `gme_separate_binary` |
| 30 | `game_hub` (built-in minigame hub → 20–29) | | |
| 31 | `st31_book_aux_mode` (type 253) | | |
| 32 | `discovery_mode_hub` (product 7 → 33–41) | | |
| 33–41 | `study_reading_game1..9` (children of 32) | | |

A TUI can reasonably render 14–69 collapsed as "game: *name*" — they all share one
template: pop to parent on 0x100C/0x1049, pop-all to standby on 0x104A.

### 2.4 Event vocabulary (for annotating the log)

The most useful ids (event = u16; `AO+0x24` holds the last one dispatched):

| id | meaning |
|---|---|
| 0x1000 | init-after-transition signal (dispatched to the new leaf after every move) |
| 0x30 {slot} | software-timer tick (GME timers, USB settle) |
| 0x1046 | OID-poll heartbeat |
| 0x105F / 0x1060 | OID partial / **OID decoded (a tap)** |
| 0x1058 / 0x1059 | open product → push book_mount(12) / mount done → push book(13) |
| 0x1015–0x1045 | game-launch events (book → game leaf; see §2.3 mapping) |
| 0x100C / 0x1049 | back/exit — pop one level |
| 0x104A | abort — pop-all to the stack bottom (standby) |
| 0x105D / 0x105C / 0x1009 | USB attach → usb_detect(8) / → usb_msc(5) / → charging(6) |
| 0x105E | USB detach |
| 0x1014 | splash done → standby |
| 0x1047 / 0x1062 | poweroff_prep / refresh-or-off (state 10 pushes system_off 4) |

---

## 3. Detecting state transitions (live log)

Transitions only happen inside the event pump, synchronously with event dispatch.

**Pure-RAM method:** poll the triple `(SP = u32@0x08008888, depth = u16@0x08008878,
leaf = *(u8*)SP)` and log on change. Classify by depth delta: same depth + new leaf =
sibling replacement; +1 = push (entering a child); −n = pop; depth→0 with old depth >1 =
pop-all. Read `AO+0x24` (u32 @0x08008898) in the same refresh to annotate *which event*
caused it. Polling at the TUI frame rate can miss transient states (book_mount(12) is
sub-millisecond); polling once per dispatched event (if the emulator exposes an
event-loop callback) is exact.

**Exact method (recommended): read-only PC watchpoints** — the firmware stays unmodified;
the emulator merely observes the PC:

| PC | function | what you learn at that instant |
|---|---|---|
| 0x080F2B38 | `event_loop_dispatch` | every hierarchy move: arg2 (r1) = status — 3 sib, 4 push, 5 pop-all, ≥6 pop (status−5) levels; arg1 (r0) = target state id for sib/push |
| 0x080F2C78 | `sm_dispatch_event` | each (state,event) dispatch — r0 = state id, event follows; useful for a verbose event trace |
| 0x080F2D70 | `sm_dispatch_to_hierarchy` | one pumped event begins (state 9 → leaf → state 10) |

Entry/exit actions: after a move, the new leaf's entry fn (`descriptor+0`) runs and the
old one's exit fn (`descriptor+4`) has run; watch those addresses (per state, via the
descriptor table @0x08121D44) if the log should show "entered/exited X" at action
granularity. **Observed** (all of the above are the actual dispatch functions).

---

## 4. The GME interpreter

The book(13) default event handler is the **GME OID dispatcher** (`gme_oid_dispatch`
@0x0803629C). It routes each decoded tap (event 0x1060) either to the product-mount path
(OID ≤ 0x3E7 = 999, the product band) or to the content path (script lookup + execution).
The interpreter re-reads script/media data from the `.gme` file on demand; only the
state listed below is resident in RAM.

### 4.1 Two context structures

- **`game_ctx` @ 0x080089A4** — static, fixed address. Bytes of interest:
  `+0x1D` u8 mode marker (2 = standby/book fresh, 8 = idle-counting, 3 = off-prep),
  `+0x1E` u8 USB/charger phase (2 = USB session).
- **`akoid_buf`** — the tap/GME context, heap-allocated: read the pointer at
  **`game_ctx+0x20` = 0x080089C4** (u32; observed live value ≈ 0x08146C30). All
  `akoid_buf+X` reads below dereference this pointer first; treat pointer value 0 as
  "not initialised yet". **Observed.**

Key `akoid_buf` fields (offsets from the pointed-to struct):

| offset | width | meaning |
|---|---|---|
| +0x04 | u16 | **last decoded OID value** (latched by the classifier on every 0x1060) |
| +0x18 / +0x1A | u16 | **first / last content OID** of the mounted GME (from the play-script table header) |
| +0x21 | u8 | first-load gate: 0xFF = no product opened yet, 0 = in book mode |
| +0x58 | u8 | replay flag (game modes set 100 to re-inject the saved OID) |
| +0x74 | u16 | OID latched at the first (book-opening) tap |
| +0x125 | u8 | play-script dispatch counter (round-robin selector for opcode 0xFFE0) |
| +0x12A / +0x12C / +0x12D / +0x12E | u8 | playlist play-mode state: busy / play-all mode / play-all cursor / play-from-hi cursor |
| +0x141 / +0x142 | u8 | last playlist pick (round-robin / random) |
| +0x4A4 | u8 | "tap is not the current cover" bookkeeping flag |
| +0x4A8 | u8 | **product mounted** flag (1 = a GME is mounted) |
| +0xDD0 / +0xDD2 | u8 / u16 | deferred **Jump** (opcode 0xF8FF) pending flag / target script line — taken when audio stops |

### 4.2 Mounted-GME state (all fixed addresses)

| address | width | meaning |
|---|---|---|
| **0x081DA08C** | u32 | **current product id** (= GME header@0x14; 0 = none) |
| **0x08121ED0** | i32 | **file handle of the mounted `.gme`** (−1 = none) |
| 0x081DA086 | u8 | status bits: bit7 = GME mounted, bit1 = welcome jingle played, bit4 = low-batt voice pending |
| 0x081DA094 / 0x081DA098 | u32 | play-script table / media table **file offsets** (from GME header 0x00/0x04) |
| 0x081DA1AC / 0x081DA1B0 / 0x081DA1B4 / 0x081DA1B8 | u32 | additional-script (timer) / game / register-init / additional-media table file offsets |
| 0x08121A80 | u8 | media **XOR key** (derived from GME header@0x1C via a 256-byte ROM LUT @0x080381DA; typical value 0xAD) |
| 0x08008C00 | u8 | XOR-enable: 1 = GME media currently being read (decrypted), 0 = plain (system voice) |
| 0x08121ECC | u8 | **GME timer handle** (opcode 0xFE00; 0xFF = no timer armed) |

The **path of the mounted GME**: the booklist iterator (§4.6) keeps the current record's
path at `iterator+0x30` (UTF-16, up to 0x103 chars); after a successful mount it is the
mounted file's path. **Observed.**

### 4.3 The `$`-registers

| item | value |
|---|---|
| register file | **u16 array @ 0x081DA350** |
| register count | **u32 @ 0x081DA0A0** (loaded from the GME's register-init block, header@0x18, at mount / reset) |
| mapping to tttool | firmware register index *i* = tttool register `$i` exactly; values are unsigned 16-bit |

Registers are (re)initialised at product mount by `gme_reset_registers` @0x08035CA8 and
persist across taps while the book is mounted. The TUI should render
`$0 … $(count−1)` from `0x081DA350`, refreshed after every dispatched event (or
continuously — reads are cheap). **Observed.**

### 4.4 OID → script routing (the debugger's "tap X → script line Y")

Content path (tapped OID > 0x3E7, book mounted):

1. Range check: `first ≤ OID ≤ last` with first/last = u16 @ `akoid_buf+0x18/+0x1A`.
   Out of range ⇒ error voice, no script.
2. `gme_oid_to_playscript` @0x080359A4: **script index = OID − first**. It seeks the
   `.gme` to `(*0x081DA1BC) + index*4` (0x081DA1BC holds the file offset of the per-OID
   script-pointer array = play-script table + 8) and reads the u32 script offset;
   `0xFFFFFFFF` = this OID has no script.
3. At the script offset it reads the u16 **line count → u32 @ 0x081DA09C** and the line
   offsets → **u32 array @ 0x081DA1C0** (file offsets of each script line).
4. Lines are evaluated **in order**; the first line whose conditions all hold executes
   (conditions via `gme_check_condition` @0x08035624; actions via `gme_exec_command`
   @0x08034DA0; then the line's playlist plays).

The most recently parsed line's decoded content is resident in RAM (refreshed per line
evaluated):

| address | content |
|---|---|
| 0x081DA0A9 / 0x081DA0B2 / 0x081DA0C2 / 0x081DA0D2 / 0x081DA0DA | condition arrays: op / lhs / rhs / operand-type flags / values |
| 0x081DA0A8 | u8 action count (≤ 8 — the firmware clamps to 8 actions/line) |
| 0x081DA0EA / 0x081DA0FA / 0x081DA10A / 0x081DA112 | action arrays (8 entries): register index (u16) / opcode (u16) / operand-is-const (u8) / operand value (u16) |
| 0x081DA122 / 0x081DA126 | u16 playlist length / u16[] playlist (media indices) |
| 0x081DA4E4 / 0x081DA564 | u32[] resolved media file offsets / sizes for the playlist |

The *index* of the line being executed lives only in interpreter locals. Recover it
either by watching the `fs_seek` targets against the line-offset array @0x081DA1C0, or —
simplest — a PC watchpoint on **`gme_exec_command` @0x08034DA0** (args in r0–r3:
register index, opcode, is-const, operand — one hit per executed action, in order) and on
**`gme_check_condition` @0x08035624`** (one hit per condition test). Together with the
tap OID (`akoid_buf+4`) and the YAML (§5) this yields a full "tap OID X → conditions
evaluated → line Y ran → actions/playlist" trace. **Observed** (mechanics); the
watchpoint recipe is emulator technique, not firmware behaviour.

Action opcodes (16-bit LE), for disassembling the parsed action arrays:

| opcode | tttool name | effect | | opcode | tttool name | effect |
|---|---|---|---|---|---|---|
| 0xFFF9 | Set | `reg = v` | | 0xFFE8 | Play n | play `playlist[n]` |
| 0xFFF0 | Inc | `reg += v` | | 0xFFE0 | RandomVariant `P*` | round-robin `playlist[ctr % len]` |
| 0xFFF1 | Dec | `reg −= v` | | 0xFFE1 | PlayAllVariant | play-all mode from 0 |
| 0xFFF2 | Mult | `reg *= v` | | 0xFB00 | PlayAll | play-all from hi byte |
| 0xFFF3 | Div | `reg /= v` | | 0xFC00 | Random a b | play `playlist[heartbeat % (a−b+1) + b]` |
| 0xFFF4 | Mod | `reg %= v` | | 0xFF00 | "Timer" `T` | **random-to-register**: `reg = tick % (m+1)` |
| 0xFFF5 | And | `reg &= v` | | 0xFE00 | Timer | arm periodic GME timer, period m×100 ms-units, handle @0x08121ECC |
| 0xFFF6 | Or | `reg \|= v` | | 0xFEFF | — | cancel the GME timer |
| 0xFFF7 | XOr | `reg ^= v` | | 0xF8FF | Jump | deferred jump to script line (taken when audio stops) |
| 0xFFF8 | Neg | logical-not | | 0xFD00 | Game | launch game record (posts a launch event) |
| | | | | 0xFAFF | Cancel | exit game |
| | | | | 0xFEE0–7 | — | set sound/rate profile 0–7 (byte @0x081DB904) |
| | | | | 0xFFA1 | CoinFlipPlay | play `playlist[m]` iff the event-dispatch counter (@akoid+0x125) is even, else no-op; roll recorded @akoid+0xDDF/+0xDE0 for replay walks |

Condition opcodes: 0xFFF9 Eq, 0xFFFA Gt, 0xFFFB Lt, 0xFFFC Eq-alias, 0xFFFD GEq,
0xFFFE LEq, 0xFFFF NEq — pure u16 compares. **Observed** (full opcode switch decoded).

Related counters the opcodes draw on: system tick u32 @ **0x08008D24** (one ++ per 20 ms
timer IRQ — see `interrupts-and-timers.md`), heartbeat counter u32 @ **0x081DA014**
(one ++ per OID-poll tick, event 0x1046). The GME timer fires as event 0x30 with the
slot handle @0x08121ECC as its argument and re-evaluates the GME's *additional script*
table (header@0x0C) — show an armed timer as "GME timer: slot *h*, additional script".

### 4.5 Media playback ("now playing")

All audio funnels through one player. For a "last play_media" display:

- **PC watchpoint on `play_media` @0x080AB7B4** — GME media play; the resolved (offset,
  size) pair for playlist entry *k* is also readable at `0x081DA4E4[k]` / `0x081DA564[k]`.
- **PC watchpoint on `fwl_play_voice_by_id` @0x080AB9AC** (r0 = voice id) — *system*
  voice prompts from `A:/VOIMG/Chomp_Voice.bin` (48 entries). Useful ids: 0x13 welcome
  jingle (first book entry), 0x14 power-off jingle, 0x17/0x1A battery warn/final,
  0x2B "not a valid tap / nothing mounted", 0x2D "product not found — use tiptoi
  Manager", 0x09–0x12 spoken digits 0–9.
- Pure-RAM discrimination: u8 @0x08008C00 = 1 while the current source is GME media,
  0 for a system voice; the playlist mode/cursor bytes `akoid_buf+0x12C/0x12D/0x12E`
  and picks `+0x141/+0x142` show play-all/random progress. **Observed.**

### 4.6 The booklist

Built once per boot at standby entry (and on the disc-change GPIO): a recursive scan of
`B:` then `A:` for `*.gme`, written to `a:/oidfilelist.lst` and iterated from RAM (see
`nand-image-layout.md` §7 for the on-disk flow). Runtime object:

| item | address | meaning |
|---|---|---|
| iterator pointer | u32 @ **0x081DA080** | → heap object (0 before standby entry) |
| `iter+0x00` | u16 | **number of `.gme` files found** this boot |
| `iter+0x04` / `+0x06` | u16 | cursor pair (current record) |
| `iter+0x30` | wchar16[0x104] | **current record path** (UTF-16; after a mount = the mounted GME's path) |

Product mount is a linear probe over this list: open each candidate, accept the first
with GME magic 0x238B @hdr+0x08, matching language @hdr+0x59, and **product id
@hdr+0x14 == the tapped OID** (`akoid_buf+4`). A TUI "books" panel = count + per-record
paths; the mounted one = handle @0x08121ED0 ≠ −1 plus product id @0x081DA08C. **Observed.**

---

## 5. Symbolic names via a tttool YAML

For GMEs built with tttool (`tttool assemble book.yaml`), the YAML is the natural symbol
table for the debugger. What it provides and how to join it to live state:

| YAML element | live counterpart | join |
|---|---|---|
| `product-id:` | u32 @0x081DA08C | equality — this is also how the TUI knows *which* YAML applies to the mounted GME |
| `scripts:` entries (named or numeric codes) | tapped OID @`akoid_buf+4` | tttool assigns each named script an OID code; the name↔code map comes from tttool's codes file (`*.codes.yaml`, written at assemble time) or `tttool export` of the GME. Numeric script keys are the OID codes directly. |
| script lines (order within a script) | executed line (§4.4) | line index = position in the YAML script, same order as the firmware's line-offset array @0x081DA1C0 |
| named registers `$foo` | register file index (§4.3) | tttool numbers named registers deterministically at assemble time; recover the authoritative name→index map from tttool (e.g. `tttool export` round-trip, which renders registers as `$rN`). **Inferred** (mapping rule is tttool-internal; the round-trip is the safe source). |
| media file names | playlist media indices | media indices follow tttool's media-table order; `tttool export`/`tttool media` lists index → filename |
| `welcome:` | power-on playlist (GME header@0x71), played at mount | — |

Suggested debugger rendering once joined: on a tap, show
`OID 4716 ("acht") → script line 2 of 3 — [$counter==8] ⇒ Set($counter,0), Play(0) → "acht.ogg"`,
with the condition values substituted live from the register file and the chosen line
highlighted. Without a YAML, fall back to raw numbers ($0…, OID values, media indices) —
everything in §4 works unnamed.

---

## 6. TUI live-read checklist

"Per event" = refresh after each dispatched event if the emulator exposes that callback,
else at frame rate (accepting missed transients). All reads are plain RAM loads; nothing
here alters firmware behaviour.

| # | read | address / recipe | meaning | cadence |
|---|---|---|---|---|
| 1 | fingerprint | 26 bytes @0x08009000 == `N0038MT…Tiptoi` | enable this whole view | once, at image load |
| 2 | init-done gate | u32 @0x0800888C == 0x08121D44 | AO tables populated — reads 3–8 valid | poll until true |
| 3 | statechart leaf + chain | depth u16 @0x08008878; bytes @0x08007E80 + i·0xC, i=0..depth | current state hierarchy | per event / frame |
| 4 | transition log | change in (u32 @0x08008888, leaf byte, depth) + last event u32 @0x08008898; exact: PC watch 0x080F2B38 | log entries "event E: A → B (push/pop/sib)" | per event (poll) or exact (watch) |
| 5 | mounted GME | i32 @0x08121ED0 (handle ≠ −1), u32 @0x081DA08C (product id), path @ *(u32@0x081DA080)+0x30 | "book" panel header | on transition into/out of 12/13, or per second |
| 6 | registers | count u32 @0x081DA0A0; u16[count] @0x081DA350 | `$0…$N` values | per event / frame |
| 7 | last tap + routing | u16 @ *(u32@0x080089C4)+4 (OID); range @+0x18/+0x1A; line table u32 @0x081DA09C + u32[]@0x081DA1C0; exact line/actions: PC watch 0x08034DA0 / 0x08035624 | GME debugger: tap → script line → actions | per 0x1060 event |
| 8 | interpreter play state | @akoid: +0x125, +0x12A/C/D/E, +0x141/2, +0xDD0/+0xDD2; playlist @0x081DA122/26; media @0x081DA4E4/0x081DA564; XOR flag u8 @0x08008C00 | playlist mode/cursor, deferred jump, media vs voice | per event |
| 9 | last play_media / voice | PC watch 0x080AB7B4 (GME media) and 0x080AB9AC (system voice, r0 = id) | "now playing" line | on hit |
| 10 | GME timer | u8 @0x08121ECC (0xFF = none) | armed-timer indicator | per second |
| 11 | booklist | u32 @0x081DA080 → u16 count @+0, paths via records | discovered books panel | after standby entry (state 3 appears at stack bottom) |
| 12 | counters | u32 @0x08008D24 (tick), u32 @0x081DA014 (heartbeat) | time/entropy display | per frame |

**Caveat on heap pointers:** items 7, 8 and 11 dereference pointers
(0x080089C4 → akoid_buf, 0x081DA080 → iterator) that are 0 until their owner has run
(splash / standby entry). Always null-check; the fixed-address items (3–6, 9–10, 12) are
safe once item 2 passes.

---

## 7. Gaps / limits

- **State names** are reverse-engineering labels, not firmware strings; the identities of
  leaves 19 and 31 are partially characterised, and the GME-type leaves 58–69 are named
  by their dispatch type only.
- The **executing script-line index** is not resident in RAM (locals only) — exact display
  needs the PC-watch recipe of §4.4.
- **tttool register/media name mapping** is assemble-time tttool behaviour (**Inferred**);
  use tttool's own export/codes output as the authoritative map rather than re-implementing
  the assignment rule.
- **Embedded-binary GMEs** (states 67/69) run their own ARM code; §4's interpreter view
  covers them only up to the launch. Registers/playlist panels are meaningless while such
  a binary runs. The load/launch path itself is **fully exercised** — see §8.
- All addresses are specific to **N0038MT / 20131009**; any other build (including the
  older non-MT 3202 image) relocates them — hence the hard gate in §1.

---

## 8. Main-binary (embedded-code) GMEs

Most GMEs are **play-script** GMEs: the firmware's GME *interpreter* (§4) walks
their tables. A **main-binary GME** is different — it carries a native ARM blob
that the firmware **loads and executes**, and that blob drives the game by
calling back into the firmware through a ~90-entry `system_api` table. This is a
2N/ZC3202N-only feature. The emulator runs such a GME **with no code changes and
no hooks** — the unmodified firmware does all of it — demonstrated end-to-end by
`tests/test_main_binary.py` against `tests/data/main_binary/minimal_mb.gme`.

### 8.1 The header fields that select it

| GME hdr off | field | firmware use |
|---|---|---|
| 0xA4 | **separate-binary flag** (1 byte) | read as `*0x081DA088`; `== 1` means "this product ships a separate main binary → launch it" |
| 0xA8 | **main-binary table** (ZC3202N) | `gme_read_main_binary_table` reads it → `{binOff, binLen}` → state 69 |
| 0x98 | additional game-binaries table | non-zero → state 67 (`gme_binary_subgame`) |
| 0xA0 / 0xC8 | ZC3201 / ZC3203L binary tables | not the MT pen's slots |

Binary-table layout: a 16-byte header (the skipped 0th slot) then, at `+0x10`,
the real entry `{u32 binOff, u32 binLen, char name[8]}`. **Observed** (loader
reads at table+0x10; blob then follows). The header `0xA4` flag being 1 is
mandatory — otherwise the firmware treats the whole offset region as absent.

### 8.2 The load / launch path (all Observed, addresses N0038MT)

On the authentic power-on descent + product-tap flow (§4, `nand-image-layout.md`
§7.3.1a) the firmware mounts the product and then, **on its own**, with no special
content tap:

1. **`gme_oid_dispatch` @0x0803629C** (book(13) default handler, runs every
   dispatch): if `*0x081DA088 == 1` (separate flag, hdr@0xA4) **and** the
   product-mounted flag `akoid_buf[0x4A8] == 1` **and** `is_audio_playing()==0`,
   it posts **event 0x1045** and sets `akoid_buf[0x4A8]=2`.
2. **0x1045 → push state 69** `gme_separate_binary`. Its entry runs
   **`gme_read_main_binary_table` @0x080AADD8**: `fs_seek(gme, 0xA8)` → table
   offset; `fs_seek(table+0x10)` → reads `binOff` (→`*0x080AADD4`) and `binLen`;
   logs `"Firmware address = %x,File len=%d."`. State 69 then posts **0x101B**.
3. **0x101B → push state 67** `gme_binary_subgame`. Its handler
   `gme_oid_dispatch_alt` @0x080AAF70 (event-action slot 15) calls
   **`gme_launch_binary_build_sysapi` @0x080AA934**, which:
   - `gme_alloc_binary_region` @0x08038C5C returns the load address
     **0x08132000** and a region size of **0x10000 (64 KiB)**;
   - fills a ~90-word `system_api` struct on the stack from a ROM pointer pool;
   - `fs_seek(gme, binOff); fs_read(gme, 0x08132000, binLen)` — loads the blob;
   - **`(*(code*)0x08132000)(&system_api)`** — jumps in with `r0 = &system_api`.

State 67's handler calls the launcher **on every event-pump cycle**, so the blob
is re-loaded and re-run each tick — the blob *is* the game's per-tick handler
(it must return; `bx lr` re-enters the firmware). The blob is entered in ARM
state at the region base; a one-instruction `b main`-style thunk at 0x08132000
is the usual entry (`startup.s`).

### 8.3 The `system_api` table (offsets verified against N0038MT)

`r0` points at the struct; each field is `offset = slot*4`. The pointers are all
ordinary firmware functions the emulator is already running, so a blob's callback
lands in real firmware code with no special support:

| off | field | firmware fn (N0038MT) |
|---|---|---|
| +0x0C | `is_audio_playing` | 0x0800B024 |
| +0x14 | `open`  | 0x0800DE20 (`fs_open`) |
| +0x18 | `read`  | 0x0800DEC8 (`fs_read`) |
| +0x1C | `write` | 0x080AD4E4 |
| +0x20 | `close` | 0x080AD514 |
| +0x24 | `seek`  | 0x0800DED8 |
| +0x2C | `play_sound(fh,off,len)` | 0x080AB7B4 (`play_media`, §4.5) |
| +0x34 | `fpAkOidPara` | → the game-context akoidpara base |
| +0x38 | `p_filehandle_current_gme` | → `0x08121ED0` (the mounted GME handle, §4.2) |

A blob plays bundled media #i by reading the media table (hdr@0x04) for the
`(offset,len)` pair and calling `play_sound(*p_filehandle_current_gme, off, len)`
— the same `play_media` path as a script GME, so the media/codec/XOR chain (§4.5)
applies unchanged.

### 8.4 What the emulator must present

**Nothing beyond a normal firmware run.** The load region 0x08132000 is inside
the main-RAM mapping (`memory-map-and-boot.md` §2: `[0x08000000,0x08400000)`),
already present; the blob's callbacks are firmware functions already executing;
the mount + power-on descent + product-tap flow (§4) already reaches book(13). No
hook, no extra peripheral, no special load step is required — the firmware
detects, loads, and jumps to the blob itself. A debugger/TUI can *observe* the
path with read-only PC watchpoints on 0x080AADD8 (loader), 0x080AA934 (launcher)
and 0x08132000 (blob entry); `run_session(on_prepared=…)` is the wiring point.

**Evidence** (`tests/test_main_binary.py`, firmware unmodified): a minimal blob
that writes a marker word and calls `is_audio_playing` + `play_sound` produces
the state chain `splash(1)→standby(3)→mount(12)→book(13)→69`, hits 0x080AADD8 /
0x080AA934 / 0x08132000, and leaves `MARK[0]=0xDEADBEEF` + the post-call
sentinels at 0x08141F00 — i.e. the embedded code ran and its `system_api` calls
reached firmware and returned.

> **Note — address conventions.** The addresses above are for the **N0038MT**
> image this emulator boots (loader 0x080AADD8, launcher 0x080AA934). Other
> tiptoi firmware images/generations (e.g. the non-MT Update3202 or the ZC3201
> variant) place the loader/launcher at different addresses (around 0x080A0894 /
> 0x080A03F0); the load address 0x08132000 and the `system_api` slot layout are
> the same across them.
