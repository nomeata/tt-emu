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
      └─ PCM ring buffer in RAM (12 KB)             software
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
| `0x04010004` | **Source address.** Memory addresses are written as `phys & 0x3ffff` (an 18-bit offset into a physical window — see the caveat below); peripheral-port codes as `port_addr \| 0x08080000`. |
| `0x04010008` | **Destination address**, same encoding. For playback: **port 1 = DAC**, i.e. the value `0x6200 \| 0x08080000 = 0x08086200`. |
| `0x0401000c` | **Word count + START/BUSY.** Written `((len_bytes / 4) & 0x7ff) \| 0x2000`: an 11-bit word count with **bit13 = START/BUSY**. The submit routine polls `while (reg & 0x2000)` *before* programming (slot free); memory-to-memory transfers also spin on it afterwards for completion. Must read with bit13 **clear** when the engine is idle, or the firmware hangs in the pre-submit poll. |
| `0x04010010` | Per-port status word (read by an internal poll helper). |
| `0x0401001c` | Status word checked **first** by the DMA-done interrupt handler as a spurious-IRQ test: **must read 0** when the ISR runs, or the handler returns without ACKing and line 0 storms (see `interrupts-and-timers.md`). |

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

> **Caveat — the 18-bit source encoding (Inferred / open).** The firmware writes
> `phys & 0x3ffff` into `0x04010004`, i.e. the engine addresses a 256 KB physical window
> whose base has not been pinned down. A practical emulator therefore should **not** try
> to decode `0x04010004` back to a CPU address; instead capture the chunk's virtual
> pointer at submit time (§7).

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
| +0x40 | **ring size = 0x3000** (12 KB = 12 chunks ≈ 139 ms @ 22050 stereo S16) |
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
- **Generation note (benign):** a later firmware generation builds the same structure
  with **ring size 0x6000 (24 KB)** instead of 0x3000. The layout and protocol are
  identical; only +0x40 (and the wrap point) differ. An emulator must not hard-code the
  size — read +0x40 if it ever needs it. **Observed** (live RAM dump of a pen running the
  later generation vs. this firmware's construction code).

**DAC / rate registers** (needed only for read-back defaults and, optionally, authentic
rate derivation; details of the clock tree in `system-control-and-clock.md`):

| register | audio-relevant fields |
|---|---|
| `0x04080000` | bit0 = DAC enable (pulsed 0→1 around a rate change) |
| `0x04000008` | bit24 = DAC-clock enable; **bits[20:13] = rate divider code** (+ latch bit21). Observed data point: 22050 Hz → divider code 0x46 |
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
2. **Record the chunk source at submit.** Because `0x04010004` holds only `phys &
   0x3ffff` (window base unresolved, §2), grab the source as a CPU pointer instead:
   either hook the ring dequeue's return (chunk ptr = `ring[+0x44] + ring[+0x38]` just
   before it advances) or — the more hook-free variant once the window base is confirmed —
   decode the register. Track the *last* recorded pointer plus a flag saying whether the
   current submit came from the ring (to handle §6 correctly).
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

- **Inferred:** the physical window base behind the 18-bit `0x04010004` encoding —
  confirm at runtime (read the register after a live submit on hardware) to make the
  source decoding fully hook-free.
- **Inferred:** which init path sets `INT_ENABLE` bit0 initially (see
  `interrupts-and-timers.md` for the enable-register default that sidesteps this).
- The exact divider search arithmetic for arbitrary rates (only the inverse table lookup
  and the 22050 → 0x46 data point are needed for emulation).
