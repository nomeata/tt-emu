# Audio DAC + DMA — output path, ring buffer, done-interrupt, pacing

The tiptoi pen plays sound through a **DAC internal to the SoC**, fed by a small
**peripheral DMA engine** that streams PCM out of a software ring buffer in RAM, followed
by an **external speaker amplifier** enabled via a GPIO. This document describes that
output path at the level needed to implement audio in an emulator: capture the exact
sample bytes the **unmodified** firmware plays (at the right rate and format), and drive
the DMA-completion interrupt so the firmware's own refill chain runs.

Facts are tagged **Observed** (byte-verified in the firmware's code, or seen in a live RAM
dump from a real pen) or **Inferred** (deduced; reason given). Addresses are runtime
addresses in the firmware's address space (see `memory-map-and-boot.md`).

Related: `interrupts-and-timers.md` (top-level IRQ line 0 = DMA done, delivery and pacing
rules, the system tick), `gpio-buttons-led.md` (amp-enable / mute / headphone-detect
pins), `system-control-and-clock.md` (clock registers the DAC rate divider lives in).

---

## 1. Output chain overview

```
decoder (OGG/WAV/…)                                 software
  └─ mixer: ×volume (Q10), mono→stereo dup          software
      └─ PCM ring buffer in RAM (24 KB, §5)         software
          └─ DMA engine 0x04010000                  hardware  ← emulator boundary
              └─ internal DAC 0x04080000            hardware
                  └─ external amp (GPIO16 enable)   hardware
                      └─ speaker
```

- The decode/mix/ring stages are pure software; the firmware executes them unmodified.
  The **hardware boundary the emulator must model is the DMA engine**: everything
  upstream of it has already been applied to the bytes the DMA reads.
- Completion of each DMA transfer raises **top-level IRQ line 0**
  (`0x040000cc`/`0x04000034` bit 0 — see `interrupts-and-timers.md`); the firmware's own
  interrupt handler chains the next chunk from interrupt context. **Observed.**
- The speaker is only audible when the external amplifier is enabled (**GPIO16 = 1**) and
  the mute strobe is released (**GPIO13 = 0**); headphone detect (**GPIO7**, input,
  1 = plugged) forces the amp off. See `gpio-buttons-led.md`. Modelling these is optional
  fidelity for a capture-only emulator. **Observed** (pin operations; pin roles partly
  Inferred from sequencing).

### Sample format (what the DMA transfers)

| property | value |
|---|---|
| encoding | **signed 16-bit little-endian PCM (S16LE)** |
| channels | **always 2, interleaved L R L R …** — mono sources are written twice (L=R) by the mixer |
| volume | **already applied** (Q10 scale, §5) — the DMA-side bytes are post-volume |
| sample rate | **per track**, set by retuning the DAC clock (no software resampler): GME audio content is typically **22050 Hz**; built-in system voice files are 16000/32000/44100 Hz; the DAC bring-up default is 8000 Hz (transient, before the first track retune) |

**Observed** (mixer code writes S16 stereo with Q10 scaling; rate values from media
headers and the rate-divider programming).

An implementer capturing to WAV should honour the rate active at the time of each chunk;
in practice a whole playback session runs at one rate, so tagging the WAV with the last
non-8000 rate seen is sufficient (§7).

---

## 2. The DMA engine — MMIO `0x04010000`

A small general-purpose peripheral DMA engine with one register window. It serves both
memory↔peripheral transfers (DAC playback, microphone capture) and memory↔memory copies.
All **Observed** from the firmware's submit routine and its interrupt handler.

| register | role |
|---|---|
| `0x04010000` | **Control.** Written `0x00620024` once at HAL init. **bit16 = kick/GO** for peripheral-port transfers: the submit routine sets it (`|= 0x10000`) after programming the other registers; the DMA-done interrupt handler clears it (`&= ~0x10000`) — that clear is the interrupt ACK. |
| `0x04010004` | **Source address.** For memory the register holds **`phys & 0x3ffff`** — an 18-bit offset into the DMA engine's 256-KiB physical-RAM aperture (based at the RAM base `0x08000000`, see the note below); peripheral-port codes as `port_addr \| 0x08080000`. |
| `0x04010008` | **Destination address**, same encoding. For playback: **port 1 = DAC**, i.e. the value `0x6200 \| 0x08080000 = 0x08086200`. |
| `0x0401000c` | **Word count + START/BUSY.** Written `((len_bytes / 4) & 0x7ff) \| 0x2000`: an 11-bit word count with **bit13 = START/BUSY**. The submit routine polls `while (reg & 0x2000)` *before* programming (slot free); memory-to-memory transfers also spin on it afterwards for completion. Must read with bit13 **clear** when the engine is idle, or the firmware hangs in the pre-submit poll. |
| `0x04010010` | Per-port status word (read by an internal poll helper). |
| `0x0401001c` | Status word checked **first** by the DMA-done interrupt handler as a spurious-IRQ test: **must read 0** when the ISR runs, or the handler returns without ACKing and line 0 storms (see `interrupts-and-timers.md`). |
| `0x04010014` / `0x04010018` | **Pager frame lock bitmasks** — read **and** written by the nandboot demand-pager (not the DAC). One bit per pager frame, split at frame 22: `0x14` covers frames 0–22 (bit `frame+9`), `0x18` covers frames 23–36 (bit `frame−23`). The pager's victim scan consults them to bias/skip frames. The firmware writes them (mostly 0); the emulator RAM-backs them so reads return what was written. **Observed** (pager hash `0x08001314`). |
| `0x04010124`–`0x040101b4` | **Pager per-frame usage clock** — a 37-entry array (one word per pager frame), **read-only** to the firmware (read ~30 k×/boot, written 0×). The demand-pager evicts the frame with the **smallest** entry, i.e. the least-recently-used. This is a *hardware* frame-usage tracker; the emulator models it (§2.1). |

### 2.1 The block is shared with the demand-pager (frame-usage tracker)

Beyond the DAC/DMA registers, this MMIO block hosts the nandboot demand-pager's
frame bookkeeping: the lock bitmasks (`0x14`/`0x18`) and the per-frame usage-clock
array (`0x124`). The pager's frame allocator (`0x08001314`, scans its 37 frames)
picks its eviction victim as the frame whose `0x04010124[frame]` entry is smallest
— the least-recently-used. On silicon the hardware stamps a frame's entry when a
page is mapped into it. The emulator reproduces this: `AudioDma.touch_frame(idx)`
advances a per-frame clock, driven from `mmu_boot` when the pager records a page in
a frame (its eviction-table write at `0x0800878c`).

This matters for **audio**: without the model every entry reads 0, the pager can't
rank frames, and its eviction collapses onto ~5 frames. The audio-decode working
set (~22 hot code pages) then thrashes through them — thousands of re-faults per
page — and the book/product audio never drains within a tap budget. Modelling the
array spreads eviction across all 37 frames and removes the thrash.

**Peripheral port codes** (argument < 10 to the submit routine selects a port instead of a
memory address): 0 → `0x6000` (ADC/mic capture), **1 → `0x6200` (DAC out)**, 2 → `0x6400`,
3 → `0x6600`, 4/9 → `0x6800`, 5 → `0x6a00`, 6 → `0x6a40`, 7 → `0x6a80`, 8 → `0x6ac0`.
**Observed** (port-address mapper table).

**Submit protocol** (the firmware's `hal_dma_submit(src, dst, len)`, runtime
`0x08003530`):

1. Poll `0x0401000c` until bit13 clear.
2. Write source → `0x04010004`, destination → `0x04010008`.
3. Write `(len/4) | 0x2000` → `0x0401000c`.
4. Record a **transaction-tag byte at `0x08007e78`**: `0x76` = destination-port-1 = DAC
   playback, `0x75` = source-port-0 = mic capture. The DMA-done ISR dispatches on this
   tag.
5. For a port transfer: set `0x04010000 |= 0x10000` (kick) and **return immediately** —
   port transfers are asynchronous, completion arrives as the interrupt.

For audio, the caller is `hal_ao_submit_tick` (runtime `0x08003a74`): dequeue the next
ring chunk (§5) → translate the chunk's virtual address to physical → `hal_dma_submit(phys,
1, len)` → mark the chain active (flag byte, §4).

> **The 18-bit source encoding — a physical-address aperture.** The DAC engine
> is a **physical**-address bus master. `0x04010004` holds `phys & 0x3ffff`: an
> 18-bit offset into a **256-KiB physical-RAM aperture based at the RAM base
> `0x08000000`** (a hardware property, proven at runtime — reconstructing
> `0x08000000 | (reg & 0x3ffff)` resolves every submit; every mapped page of the
> firmware's DMA pool sits in this one window). The firmware allocates its
> DMA-able pool (PCM ring, system-voice buffer, teardown-flush buffer) from a
> physically-contiguous region and translates each buffer to physical with
> `hal_virt_to_phys` before submitting; the emulator reads the reconstructed
> physical address, translating physical→where-the-bytes-live through the same
> map (§7). This resolves any source uniformly and needs no firmware ring/struct.

---

## 3. The completion interrupt (top-level line 0)

DMA completion asserts **top-level IRQ line 0** (`INT_PENDING 0x040000cc` bit0, gated by
`INT_ENABLE 0x04000034` bit0). Delivery mechanics, the enable-register handling, and the
vector stub are in `interrupts-and-timers.md`. The firmware's handler is
`hal_audio_irq_handler` (runtime `0x080039d4`), reached from the IRQ vector stub whenever
bit0 is pending and enabled. Handler behaviour (**Observed**, disassembly):

```
if (spurious-check on 0x0401001c != 0) return;      // not ours — no ACK
[0x04010000] &= ~0x10000;                           // clear kick/GO  ← the line-0 ACK
tag = *(u8*)0x08007e78;                             // 0x75 mic / 0x76 DAC
if (tag == 0x75) { notify recorder; return; }
if (tag != 0x76) return;
if (*(u8*)0x08008c91) { *(u8*)0x08008c91 = 0; return; }  // "swallow one done" handshake (§6)
if (*(void**)0x08008c64) tail-call it;              // optional override callback (normally 0)
if (hal_ao_submit_tick() == 0)                      // dequeue + submit the NEXT chunk
    set flag 0x08008c60 bit0, clear bit2;           // ring drained: chain idle
```

So the steady state is **ISR-chained**: each DAC-done immediately submits the next ring
chunk from interrupt context — one interrupt per chunk, no main-loop involvement. The
main loop's audio tick only (re)kicks the chain at play-start or after the ring ran dry
(flag byte bit0). The emulator's only jobs are: complete the transfer, assert line 0 at
the right time (§4), keep `0x0401001c` reading 0, and treat the firmware's bit16-clear
write as the de-assert/ACK.

**Audio flag byte `0x08008c60`** (Observed): **bit0** = chain idle, needs a main-loop
kick; **bit2** = chain active. Stop paths block on bit2 (`while (flags & 4) wait;`) — if
the emulator never delivers line 0, every voice-stop and teardown deadlocks here.

---

## 4. ★ Pacing the completion — real-time audio and correct firmware time

A real DAC drains a chunk at the audio sample clock; the DMA "completes" only when the
last sample has been played. The emulator must reproduce that interval:

```
t_chunk = len_bytes / (channels × bytes_per_sample × rate)
        = len_bytes / (2 × 2 × rate)
        = len_bytes / (4 × rate)
```

For the standard chunk: **0x400 bytes = 256 stereo frames → 256 / 22050 Hz ≈ 11.6 ms**,
i.e. **~86 audio interrupts per second** during playback. Schedule the line-0 assert that
long after the kick write, in the **same time unit your timer model uses** (see
`interrupts-and-timers.md` §"Pacing": one timer IRQ = 20 ms, so one chunk ≈ 0.58 ticks).

Why it matters (**Observed** — both failure modes seen):

- **Too fast / instant:** the ISR-chained refill dequeues chunks faster than the decoder
  refills the ring → read pointer passes write pointer → underrun, the output state
  machine stalls, the chain-active flag sticks and stop paths hang.
- **Uncalibrated constant:** any fixed delay not derived from `len` and the current DAC
  rate skews audio duration versus firmware time by the corresponding factor (a 1 s sound
  plays in 0.7 s, timers meanwhile fire the "right" number of times, etc.).

> **★ Warning — do not touch the system tick from the audio model.** The firmware keeps
> one global time counter (`0x08008d24`, +1 per **timer** IRQ, unit 20 ms — see
> `interrupts-and-timers.md`). It is tempting to "advance time" per audio chunk so the
> decoder keeps up; **don't**. The tick has ~90 consumers (every timeout, auto-off,
> event pacing, even script entropy); a second writer driven from audio fast-forwards the
> firmware's clock by an order of magnitude while sound plays. Pace audio purely by
> *when you assert line 0*; let the firmware's own timer ISR be the tick's only writer.
> **Observed** (a legacy model that wrote the tick per chunk ran firmware time ~19× fast
> during playback).

---

## 5. The PCM ring, volume, and chunking

The firmware streams through **one software ring buffer** (a singleton: pointer at
`0x08008d2c`, structure body at `0x08008d30`). The emulator never needs to manage it —
the firmware does — but its layout is useful for capture, health checks, and debugging.
All **Observed** unless noted. Byte offsets from `0x08008d30`:

| offset | field |
|---|---|
| +0x14 (u16) | **volume multiplier, Q10** — `0x400` = unity (clamped there); boot default `0x108` (volume step 3 of 0..5, ≈ 26 %) |
| +0x20 | consumed-chunk counter |
| +0x24/25/26/27 | state bytes: open / pause / EOF-mark / fade flag |
| +0x34 | underrun **zero-fill flag**: when set, a partial final chunk is padded with silence to a full 0x400 bytes |
| +0x38 | **read pointer** (byte offset into the ring; advanced by the DMA-side dequeue) |
| +0x3c | **write pointer** (advanced by the mixer) |
| +0x40 | **ring size** — Observed **0x6000 (24 KB = 24 chunks ≈ 279 ms @ 22050 stereo S16)** during live playback under this firmware; never assume a constant — **read +0x40** (see the size note below) |
| +0x44 | **ring base** (pointer to the PCM buffer, allocated from a physically contiguous pool) |
| +0x48/+0x4a (u16) | source channels / source bits (from the media header) |

- **Volume** is applied by the mixer as it writes the ring: for each 16-bit input sample
  `s`, `out = (s * ring[+0x14] + rounding) >> 10`, mono duplicated to both channels. So
  **the ring content — and therefore every byte the DMA moves — is post-volume**; a
  capture at the DMA needs no volume handling. Health check: after boot,
  `ring[+0x14] == 0x108`. The user volume buttons change the Q10 value through a 6-entry
  gain table `{0x18, 0x60, 0xA8, 0x108, 0x138, 0x180}` (plus 0 = mute).
- **Chunk size:** the dequeue hands the DMA **0x400 bytes (256 stereo frames)** per
  transaction, advancing +0x38 with wraparound at +0x40. A final partial chunk is either
  zero-padded to 0x400 (flag +0x34) or submitted at its true length; when nothing is
  left the chain stops and flag `0x08008c60` bit0 is set.
- **Size note (corrected):** this firmware builds the ring with **size 0x6000
  (24 KB)** at runtime — **Observed** in emulation during live playback (and matching
  a live RAM dump of a pen on a later generation). An earlier static reading of
  0x3000 (12 KB) from one construction path does **not** reproduce in the running
  system. The layout and protocol are identical either way; only +0x40 (and the wrap
  point) differ. An emulator must not hard-code the size — **read +0x40**.

**DAC / rate registers** (needed only for read-back defaults and, optionally, authentic
rate derivation; details of the clock tree in `system-control-and-clock.md`):

| register | audio-relevant fields |
|---|---|
| `0x04080000` | bit0 = DAC enable (pulsed 0→1 around a rate change) |
| `0x04000008` | bit24 = DAC-clock enable; **bits[20:13] = rate divider code** (+ latch bit21). Observed data points (live playback in emulation): **22050 Hz → divider code 0x28**; the idle/bring-up 8000 Hz path programs **0x74**. (An earlier static data point "0x46 → 22050 Hz" does **not** reproduce — do not key rate detection on it.) |
| `0x04000064` | bit13 = DAC analog enable; **bits[16:14] = OSR index** into the table `{256, 272, 264, 248, 240, 136, 128, 120}` (rate ≈ master_clock/(div+1)/OSR, master clock ≤ 14 MHz) |
| `0x04000010` | bit9 = DAC power-down; **bit8 = rate-apply busy — the firmware sets it and spins until it reads back clear**, so model it self-clearing (or reads-as-written-except-bit8) |
| `0x04036000/+4` | audio clock block: after the firmware writes `[0] \|= 0x10000000; [4] \|= 0x10010`, it spins until `[4] & 0x80000` — the model must present bit19 set once enabled |
| `0x0400000c` bit5 | audio module clock gate |

---

## 6. Teardown handshake — the silence flush

Before powering the DAC down (stop, standby, USB entry), the firmware kills the analog
"pop": it fills a **0x800-byte** buffer with a constant sample, sets the **swallow flag
`0x08008c91 = 1`**, submits that buffer **directly** via `hal_dma_submit` (bypassing the
ring), and **spins until the flag reads 0** — which only the DMA-done handler does (§3:
tag 0x76 + swallow flag → clear flag, do **not** chain). **Observed.**

Consequences for the emulator:

- The completion interrupt **must** be delivered for direct submits too, or every
  stop/teardown path hangs in this spin.
- This submit's source is **not** the ring chunk pointer — a capture model that only
  records the ring dequeue would append 0x800 stale bytes here. Either skip capture when
  the submit didn't come through the ring dequeue, or accept ~46 ms of wrong (constant)
  samples at the end of a session (it is silence-intent anyway). **Observed** (the stale
  capture was seen in practice; harmless but audible as a click in strict comparisons).

---

## 7. Emulator model

The proven-working shape (a headless capture-to-WAV emulator; a real-time sink is the
same with a ring/callback instead of a file):

1. **RAM-back the register window** `0x04010000..0x0401001c`. Defaults: everything 0;
   in particular `0x0401000c` bit13 clear (else the first submit hangs) and `0x0401001c`
   = 0 always (else the ISR treats every interrupt as spurious).
2. **Read the chunk source at submit as a physical bus master.** Reconstruct the
   physical address `phys = window_base | ([0x04010004] & 0x3ffff)` (window base =
   the RAM base `0x08000000`, §2), then read physical memory at `phys`. A DMA
   master reads physical RAM directly; model that as a `read_phys(phys, len)`
   primitive in the memory layer. When the CPU-side MMU is not modelled, translate
   `phys`→the flat/virtual address holding those bytes through the firmware's
   active virt→phys map (the inverse of `hal_virt_to_phys`); when it is modelled,
   physical == the address the CPU already wrote to. Either way the DMA engine
   itself needs no knowledge of any firmware buffer or ring.
3. **On the START write to `0x0401000c`** (value has bit13 and a nonzero word count)
   while the destination is the DAC port (`0x04010008 == 0x08086200`):
   - read `word_count × 4` bytes from the recorded source pointer — these are final,
     post-volume S16LE stereo samples;
   - append them to the capture / feed the audio sink, tagged with the current sample
     rate;
   - write bit13 back clear (the transfer is "in flight"; the firmware only polls this
     before the *next* submit);
   - schedule the completion: assert line 0 (`0x040000cc` bit0) after
     `bytes / (4 × rate)` of emulated time (§4).
4. **On delivery of line 0** let the firmware's own handler run; treat its
   `0x04010000 &= ~0x10000` write as the de-assert. Do not clear the pending bit
   yourself, and never re-submit chunks yourself — the ISR chains.
5. **Sample rate.** Simplest: hook the firmware's rate-set routine and remember the
   requested rate, ignoring the transient 8000 Hz bring-up default so the capture keeps
   the track rate. Authentic: decode `0x04000008` bits[20:13] and `0x04000064`
   bits[16:14] through the OSR table (§5) — this yields the *achieved* rate; the
   difference is negligible for capture. GME content plays at 22050 Hz; system voices at
   16/32/44.1 kHz.
6. **What can be simplified.**
   - **Volume:** nothing to do — bytes are post-volume (§5).
   - **DAC registers (`0x04080000`, bias/analog fields):** RAM-backed is enough; only
     the busy/ready read-backs listed in §5 need behaviour.
   - **Amp/mute GPIOs:** optional; a capture is "audible" iff GPIO16=1 ∧ GPIO13=0, and
     GPIO7 should read 0 (no headphones) — see `gpio-buttons-led.md`. Ignoring them
     yields a correct WAV that merely includes what the speaker would have muted.
   - **Mic capture path (tag 0x75, port 0):** unused during playback; can be left
     unimplemented until recording is needed.
   - **Memory-to-memory DMA:** if the firmware uses it, completion is polled via bit13,
     no interrupt — either perform the copy synchronously at the START write and leave
     bit13 clear, or verify it is never exercised in your workload.

### Sanity checks for a working model

- Steady playback shows **one line-0 interrupt per 0x400-byte chunk**, ≈ 86/s at
  22050 Hz, and `0x08008c60` alternates with bit2 set while playing, bit0 set after
  drain.
- `ring[+0x14] == 0x108` after boot (default volume); captured WAV of a known jingle has
  the right duration at the tagged rate.
- Stop paths return promptly (no spin on `0x08008c60` bit2, no spin on `0x08008c91`).
- The system tick advances only with timer interrupts, never faster during audio (§4).

---

## 8. Open items

- ~~The physical window base behind the 18-bit `0x04010004` encoding~~ — **resolved:**
  it is a 256-KiB physical-RAM aperture based at the RAM base `0x08000000`
  (proven at runtime — the reconstructed physical address resolves every content
  submit to the exact same bytes); the DAC engine is a physical bus master, so
  source resolution needs no firmware ring/struct (§2/§7).
- **Inferred:** which init path sets `INT_ENABLE` bit0 initially (see
  `interrupts-and-timers.md` for the enable-register default that sidesteps this).
- The exact divider search arithmetic for arbitrary rates (only the inverse table lookup
  and the observed data points — 22050 Hz → 0x28 live, 8000 Hz → 0x74 bring-up — are
  needed for emulation).
- ~~**Open bug — a media playback started from an *idle* audio chain is silent.**~~
  **Resolved.** The symptom was idle-time-dependent (a tap within ~100 M instructions of the
  mount played; a tap after ~2 s of idle emitted `dac_flush_silence` keep-alives, with
  `mp_pump_state1`/`ao_pull_decoder` running 0× because the active-AO list was empty). Root
  cause was **not** in the audio path at all: it was the demand-paging hack that pinned the
  work/data window resident (commit `e225130`). The firmware remaps pages in that window into
  its own demand-paged frame pool at runtime — the audio player object at VA `0x081487a0` moves
  into a pool frame — so the identity pin fought the firmware's mapping: on eviction+refault the
  pinned-vs-paged split corrupted the page and the AO's decoder-source pointer (`[player+0x24]`)
  was lost, so `aud_player_play_tail` (`0x08032c3c`) early-returned. Dropping the pin (making
  `_make_data_resident` a no-op and routing CPU reads through the MMU) fixes it; a content tap
  after idle now decodes and plays. Regression guard:
  `tests/test_scripting::test_tap_after_idle_produces_audio` (passing).
- **Open — the demand-paging shadow gap the pin was masking.** Removing the pin exposed a
  pre-existing bug: some read-write pages the firmware demand-pages have no faithful backing
  store, so on eviction their live content is lost (the `shadow` capture/restore in `mmu_boot`
  only covers PROG-image-recoverable pages). On the richer TUI flow
  (`test_live_debugger_boot_book_tap`) and the second GME (WWW Bauernhof) a media-path pointer
  page is dropped on eviction → the firmware jumps to a corrupted pointer (a UTF-16 string
  mistaken for a pointer) and crashes. The faithful fix is the **authentic NAND swap/backing
  store**: model the pager's per-frame dirty bit (`0x04010014`/`0x18`) and its swap store
  (`pager_swap_out` `0x08006ff8`) so dirty pages are written to and restored from NAND swap
  exactly as the hardware does, deleting the `shadow` stand-in entirely. Repro:
  `test_live_debugger_boot_book_tap`, `test_second_gme_mounts_and_plays` (both xfail).
- **Open — multi-tap media sequencing.** `test_scripting_end_to_end` now gets past the idle bug
  (the `acht` digit plays) but `expect_play('neun')` on the second digit tap catches the prior
  `acht` clip — the new playback's media detection races with the previous one's tail (xfail).
