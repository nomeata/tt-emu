# USB device controller (MUSB) and USB mass-storage mode (tiptoi 2N "MT")

The pen's SoC contains a **Mentor Graphics MUSB** USB 2.0 device controller at MMIO base
`0x04070000` (plus a small Anyka FIFO/DMA/PHY extension in the same window). When a PC is
plugged in, the firmware runs a **USB 2.0 High-Speed Bulk-Only-Transport (BOT) / SCSI
mass-storage device** on it, exporting the **user partition (partition B, see
`nand-image-layout.md`)** as LUN 0 — this is how GME files and firmware updates get onto a
real pen.

**Scope for an emulator — two tiers:**

1. **Default contract (§1) — mandatory.** A normal book-play session never enters USB
   mode. The emulator must still present *correct dead-bus defaults* at the MUSB window,
   because the firmware touches it even when no cable is present. Getting this wrong hangs
   or corrupts an otherwise USB-free run.
2. **USB-PC mode (§2–§6) — optional/advanced.** Modeling the registers plus a *scripted
   virtual host* lets the unmodified firmware enumerate and serve reads/writes of the
   user partition, i.e. hook-free content provisioning through the firmware's own storage
   stack. It must be opt-in and must not perturb the default boot.

Facts are tagged **Observed** (read from firmware disassembly or a live-pen RAM dump) or
**Inferred** (deduced; reason given). Addresses are runtime addresses. Cross-references:
`gpio-buttons-led.md` (GPIO8 USB-detect, GPIO15 power-hold, GPIO6 power-path),
`interrupts-and-timers.md` (top-level IRQ line 6 = MUSB), `nand-image-layout.md`
(partition B / NFTL / FAT).

---

## 1. Default (non-USB) contract — what every emulator must do

Two independent defaults keep the USB stack dormant; **both are required**:

### 1.1 USB-detect (GPIO8) = 0

`GPIO_IN` bit 8 (`0x040000BC`, see `gpio-buttons-led.md`) is the VBUS/cable-detect input.
It **must read 0** (unplugged) for a standalone book session. With bit 8 = 0 the firmware's
plug-classify state is never entered and the entire enumeration/BOT machinery stays cold.
(Observed — the connect handler and ~40 other handlers gate on this bit.)

### 1.2 MUSB window reads default to 0, never 0xFFFFFFFF

Even with GPIO8 = 0, the firmware **reads MUSB status registers**. The window must be
modeled at least as: **all reads return 0 by default; writes are accepted (RAM-backed
scratch is fine)**. A blanket "unmapped MMIO reads 0xFFFFFFFF" fallback is the worst
possible value here (Observed failure modes):

- **INTRUSB (`0x0407000A`) all-ones = bus-Reset (bit 2) + SOF (bit 3) both set = "a PC
  host is enumerating".** If the classify state is ever reached (e.g. an emulator user
  asserts GPIO8), all-ones instantly mis-classifies a phantom PC and the firmware drops
  into the mass-storage service loop, which then **spins forever waiting for a host that
  does not exist** (no CBW ever arrives, GPIO8 never drops). It also makes the
  charger-vs-PC distinction impossible.
- **The USB ISR is reachable outside USB mode.** IRQ line 6 (see
  `interrupts-and-timers.md`) dispatches to the firmware's USB ISR, which reads
  INTRUSB/INTRTX/INTRRX *before* any software gate. Its benign early-out rests on a
  static BOT-phase byte (`0x08122718`, .data initializer = 3); if that byte ever changes,
  all-ones INTRUSB drives the ISR's bus-reset branch on **every IRQ** (re-runs the bus
  reset, stomps a power-path flag). Reads of 0 make the ISR a true no-op.
- **`usb_wait_host_sof` (`0x08041CE4`) polls** the IRQ-pending register for line 6 and
  then INTRUSB bit 3 / POWER bit 4; stuck-high values fake "host alive".

Minimum viable default model: back the whole `0x0407xxxx` page with zero-initialized RAM
(reads return last written value, initially 0) — that automatically satisfies POWER
(`0x04070001`), INTRUSB (`0x0407000A`), the PHY control word (`0x04070348`), and every
other probe. Do **not** assert IRQ line 6 without a real (scripted) USB event. (Observed
requirements; the RAM-backed shortcut is Inferred-safe because outside USB mode the
firmware only ever writes-then-forgets these registers.)

That is the entire mandatory contract. Everything below is the opt-in feature.

---

## 2. Entry to USB mode

### 2.1 Plug detection and PC-vs-charger classification (Observed)

1. **GPIO8 goes 1** → the statechart enters the plug-classify state (state 8).
2. On entry the firmware writes **POWER = 0x21** (arm the PHY, enable High-Speed) and then
   samples **INTRUSB** over a settle window of up to **0x28 ticks**.
3. **Reset (bit 2) or SOF (bit 3) seen inside the window** → the peer is a PC (logs
   "`usb connect pc!!`") → internal event `0x105C` → **state 5 (USB-PC)**, the
   mass-storage mode documented here.
4. **No bus activity** in the window → the peer is a dumb charger → charger state
   (state 6, out of scope here).
5. **GPIO8 back to 0** at any point → event `0x100C` → standby.

### 2.2 State 5 (USB-PC) structure (Observed)

- **Entry action:** audio amp disabled (gameplay suspended), a session flag set.
- **Tick handler:** drives the **power-hold latch GPIO15 = 1** (pen stays powered while
  plugged, see `gpio-buttons-led.md`), refreshes the content update-list, then calls the
  **blocking MSC service loop**.
- **MSC service loop** (`usb_power_switch`, `0x0803D1D4` — misnamed in the firmware; it is
  the whole session):
  1. Allocates a **32 KiB transfer buffer** + 8 KiB DMA scratch; opens the USB device
     object; registers the **partition-B medium object as LUN 0**; brings up the PHY and
     descriptor tables; advertises on the bus.
  2. Spins polling the BOT phase byte, serving enumeration and transfers via the ISR
     path: phases 5/6 drive a read/write activity animation; every 0x2BC0 iterations it
     **flushes the medium** (log "`flush`"); after ~1,200,000 idle iterations it switches
     the power path to USB (GPIO6, battery latch released).
  3. **Exit conditions:** `GPIO8 == 0` (cable unplugged, debounced) → clean break; BOT
     phase 3/7 with "usb out" → break. **Exception:** phase 7 (the vendor "ANYKA"
     command, §5.5) reflashes and then **parks in a deliberate infinite loop** — never
     returns.
  4. Cleanup: flush, free the device object, PHY off, buffers freed.
- **After unplug:** the firmware **re-scans the B: FAT** (a failed B: scan is handled
  benignly) and **re-scans A:, reformatting A: if its scan fails**, re-registers the
  mounts, then posts `0x100C` → standby, back on battery (GPIO15 re-latched). So whatever
  the PC wrote to the B: image is re-mounted by the firmware itself. (Observed; the
  format-on-fail targets A:, not B:.)

---

## 3. MUSB register model (`0x04070000`)

Standard MUSB peripheral-mode indexed model: common registers at +0x00…+0x0E, one
per-endpoint CSR bank surfaced through the **INDEX** register at +0x10…+0x18, EP0 FIFO at
+0x20, plus Anyka extensions at +0x330…+0x348. (Observed offsets — every row below is read
or written by the firmware.)

### 3.1 Register map

| offset | name | width | firmware use |
|---|---|---|---|
| `+0x00` | **FADDR** | 8 | device address; bus reset writes 0, SET_ADDRESS writes the assigned address |
| `+0x01` | **POWER** | 8 | bit5 HSEnab, bit4 HSMode (read = "HS negotiated"), bit3 Reset, bit1/0 suspend/enable. Written 0x21 = arm+HS-enable, 0x20 during SOF wait, 0 = off |
| `+0x02` | **INTRTX** | 16 | TX-endpoint interrupt flags: bit0 = EP0, bit2 = EP2-IN completion. **Read-clear** |
| `+0x04` | **INTRRX** | 16 | RX-endpoint interrupt flags: bit1 = EP1-OUT arrival. **Read-clear** |
| `+0x06` | INTRTXE | 16 | TX interrupt enable; bus reset writes 5 (EP0+EP2) |
| `+0x08` | INTRRXE | 16 | RX interrupt enable; bus reset writes 2 (EP1) |
| `+0x0A` | **INTRUSB** | 8 | bus interrupts: bit0 Suspend, bit1 Resume, **bit2 Reset**, **bit3 SOF**. **Read-clear**. The PC-vs-charger classifier reads bits 2/3 |
| `+0x0B` | INTRUSBE | 8 | bus reset writes 0xF7 |
| `+0x0E` | **INDEX** | 8 | selects which endpoint's CSRs appear at +0x10…+0x18; written before every CSR access |
| `+0x10` | TXMAXP/RXMAXP | 16 | max packet size, programmed per endpoint on bus reset |
| `+0x12` | **CSR0 / TXCSR** | 8/16 | EP0 (INDEX=0) and TX-endpoint (INDEX=2) control/status — bits in §3.4 |
| `+0x16` | **RXCSR** | 8/16 | RX-endpoint (INDEX=1) control/status — bits in §3.4 |
| `+0x18` | **RXCOUNT** | 16 | byte count waiting in the selected RX FIFO |
| `+0x20` | **FIFO0** | 8 | EP0 FIFO, byte-wise access |
| `+0x330` | ext: EP0 byte count | 32 | Anyka L2 extension: staged EP0 transfer length |
| `+0x338` | ext: bulk byte count | 32 | staged bulk PIO transfer length |
| `+0x33C` | ext: direction latch | 32 | bit0 = RX, bit2 = TX (bulk PIO) |
| `+0x340` | ext: trigger | 32 | bit0 = EP0 go, bit2 = bulk-TX go |
| `+0x348` | ext: PHY control | 32 | bit0 = force full-speed (set at FS, cleared at HS) |

### 3.2 Endpoints (Observed)

| EP | INDEX | dir | type | wMaxPacketSize | role |
|---|---|---|---|---|---|
| EP0 | 0 | control | — | 64 | SETUP, descriptors, status stages |
| EP1 | 1 | OUT | bulk | 512 HS / 64 FS | receives the 31-byte CBW and WRITE data |
| EP2 | 2 | IN | bulk | 512 HS / 64 FS | sends READ data and the 13-byte CSW |

On every bus reset the firmware reconfigures both bulk endpoints, choosing 512-byte FIFOs
if POWER bit4 (HSMode) is set, else 64 — i.e. FIFO sizing tracks the negotiated speed —
and resets FADDR = 0, INTRTXE = 5, INTRRXE = 2, INTRUSBE = 0xF7.

### 3.3 Data staging: RAM shadows and DMA (Observed mechanics)

FIFO traffic does not all go through +0x20. The firmware stages transfers in fixed RAM/L2
windows and uses the extension registers as doorbells:

- **EP0 RX (SETUP / control-OUT):** received bytes appear in a shadow buffer at
  **`0x08006A40`**; the firmware copies from there, length = RXCOUNT.
- **EP0 TX:** bytes staged via **`0x08006400`/`0x08006600`** then written byte-wise to
  FIFO0; **+0x330 = byte count**, **+0x340 |= 1**.
- **Bulk PIO** (length not a multiple of 64): word-staged in the `0x08006400`/`0x08006600`
  windows; **+0x33C |= 1 (RX) / |= 4 (TX)**, **+0x338 = count**, **+0x340 = 4** for TX.
- **Bulk DMA** (length a multiple of 64): the shared SoC DMA engine at `0x04010000` (the
  same block the audio path uses) with channel codes **2 = EP1-RX** and **3 = EP2-TX**,
  given a physical DRAM address. A host model must service these submits by copying
  between the DRAM buffer and the virtual endpoint. (Channel-code naming Inferred from
  the submit arguments; the 64-byte-aligned fast path itself is Observed.)

**Caveat (Inferred/verify):** the shadow addresses `0x08006400/0x08006600/0x08006A40` are
literal-pool derived; re-confirm them against the firmware image being run before relying
on them.

### 3.4 CSR bit contract — exactly what the firmware sets/tests (Observed)

These are the standard MUSB peripheral-mode bits; a virtual host must honor precisely
these and nothing more.

**CSR0** (INDEX=0, +0x12) — control endpoint:

| bit | name | firmware behaviour / host-model duty |
|---|---|---|
| 0 (0x01) | RxPktRdy | *host sets* after staging SETUP/OUT bytes; firmware then reads RXCOUNT (an 8-byte packet = a SETUP) |
| 1 (0x02) | TxPktRdy | *firmware sets* to arm an IN reply: `\|= 0x02` for a full 64-byte packet, `\|= 0x0A` (TxPktRdy+DataEnd) for the last/short packet; a zero-length status stage writes 0x0A with +0x330 = 0. Host consumes the staged bytes and clears the bit |
| 2 (0x04) | SentStall | tested → EP0 flush/recovery |
| 4 (0x10) | SetupEnd | tested → firmware writes 0x80 (ServicedSetupEnd) |

**RXCSR** (INDEX=1, +0x16) — EP1 bulk-OUT:

| bit | name | behaviour |
|---|---|---|
| 0 (0x01) | RxPktRdy | *host sets* when OUT data (CBW / WRITE payload) is staged; firmware clears it (`&= 0xFE`) after draining |
| 6 (0x40) | SentStall | tested → clear, flush, error CSW |

**TXCSR** (INDEX=2, +0x12) — EP2 bulk-IN:

| bit | name | behaviour |
|---|---|---|
| 0 (0x01) | TxPktRdy | *firmware sets* to arm a packet; *host clears* after consuming = "TX done", which advances the stream / confirms the CSW |
| 1 (0x02) | FIFONotEmpty | tested set → firmware stalls the endpoint |
| 2 (0x04) | UnderRun | tested → cleared; if the read stream ended, send CSW |
| 5 (0x20) | SentStall | tested → clear, flush, error CSW |

**Interrupt registers:** INTRUSB/INTRTX/INTRRX are read together by one helper with
**read-clear semantics** — the host model must latch a bit, assert IRQ line 6, and clear
the bit on the firmware's read, or the dispatcher double-fires. INTRUSB bits 0–2 make the
bulk handler report "bus event"; INTRTX bit0/bit2 and INTRRX bit1 are the per-endpoint
completion strobes.

---

## 4. Enumeration — what the device presents (all Observed, descriptor bytes verified)

### 4.1 Identity

The pen deliberately looks like a generic MP4-player thumb drive, not a "tiptoi":

| field | value |
|---|---|
| idVendor / idProduct | **0x2546 / 0xE301** |
| bcdUSB / bcdDevice | 2.00 / 0x0100 |
| bMaxPacketSize0 | 64 |
| class/subclass/proto | 0/0/0 at device level; interface = **0x08 MSC / 0x06 SCSI-transparent / 0x50 Bulk-Only** |
| strings | 1 = `"OID     "`, 2 = `"OID Player"`, 3 = `"USB 2.0"`; LANGID 0x0409 |
| SCSI INQUIRY | vendor `"MP4     "`, product `"MP4 Player      "`, rev `"V1.0"` |

### 4.2 Descriptors served

Device descriptor (18 bytes): `12 01 00 02 00 00 00 40 46 25 01 E3 00 01 02 03 01`.

Configuration (wTotalLength 0x20): one interface, self-powered flag + MaxPower 0xC8
(400 mA), two bulk endpoints:

```
09 02 20 00 01 01 00 C0 C8      configuration
09 04 00 00 02 08 06 50 01      interface: MSC / SCSI / BOT, 2 endpoints
07 05 01 02 00 02 00            EP1 OUT bulk, 512
07 05 82 02 00 02 00            EP2 IN  bulk, 512
```

The endpoint `wMaxPacketSize` bytes are patched to 0x40 when the negotiated speed is
full-speed. A device-qualifier (`0A 06 00 02 00 00 06 40 01 00`) and an
OTHER_SPEED_CONFIGURATION header are also served, so HS enumeration is answered correctly.

### 4.3 Control-request handling

An 8-byte packet on EP0 is treated as a SETUP; the handler splits on `bmRequestType` and
dispatches standard requests through a 16-entry table indexed by `bRequest & 0x0F`:
GET_STATUS, CLEAR_FEATURE (endpoint-halt), SET_FEATURE, **SET_ADDRESS** (writes FADDR),
**GET_DESCRIPTOR** (device/config/string/qualifier), GET/SET_CONFIGURATION,
GET/SET_INTERFACE, plus the MSC class requests (Get-Max-LUN → 0, Bulk-Only reset).
(Observed table; the Get-Max-LUN slot assignment Inferred.)

### 4.4 The host-side sequence that gets the device configured

The standard Linux/Windows order works, and the answers are fully known, so a script can
verify byte-for-byte:

1. Bus **Reset** (INTRUSB bit2 pulse) → firmware re-arms endpoints, FADDR = 0.
2. `GET_DESCRIPTOR(device)` → 18 bytes above.
3. `SET_ADDRESS(n)` → firmware writes FADDR = n after the status stage.
4. `GET_DESCRIPTOR(configuration, 0x20)` → the 32-byte blob above.
5. `SET_CONFIGURATION(1)` → device configured; bulk endpoints live.
6. (Optional, as real hosts do: string descriptors, device-qualifier, Get-Max-LUN.)

---

## 5. Bulk-Only Transport + SCSI (Observed)

### 5.1 BOT phase machine

One byte (`0x08122718`) tracks the phase; its **static .data initializer is 3** (a fact
the default contract in §1.2 relies on):

| value | phase |
|---|---|
| 0 | idle — waiting for a CBW on EP1 |
| 1 | command / data-out active (WRITE streaming in) |
| 2 | data-in active (READ streaming out on EP2) |
| 3 | status — send the 13-byte CSW |
| 5 / 6 | read-busy / write-busy (drives the activity animation) |
| 7 | vendor "exit" (production channel, §5.5) |

### 5.2 CBW / CSW

- A completed **31-byte** EP1-OUT transfer is parsed as a CBW; `dCBWSignature` must be
  `"USBC"` (0x43425355), else stall. `dCBWDataTransferLength` and `bCBWLUN` are honored.
- The **13-byte** CSW (`"USBS"` = 0x53425355, the CBW tag echoed, `dCSWDataResidue` =
  expected − transferred, status 0 = good / 1 = failed with pending sense) is sent on EP2,
  then phase → 0.

### 5.3 SCSI command set answered

| opcode | command | handling |
|---|---|---|
| 0x00 | TEST UNIT READY | medium-ready check; good or NOT-READY sense |
| 0x03 | REQUEST SENSE | up to 0x12 bytes fixed sense; clears pending sense |
| 0x12 | **INQUIRY** | `"MP4     "` / `"MP4 Player      "` / `"V1.0"`, removable |
| 0x1A | MODE SENSE(6) | 4- or 12-byte mode page (0x3F = all) |
| 0x1B | START STOP UNIT | eject/load flag |
| 0x1E | PREVENT/ALLOW MEDIUM REMOVAL | lock flag |
| 0x23 | READ FORMAT CAPACITIES | 12-byte capacity list |
| 0x25 | **READ CAPACITY(10)** | 8 bytes: last LBA + block size |
| 0x28 / 0xA8 | **READ(10) / READ(12)** | stream data-in from the medium |
| 0x2A / 0xAA | **WRITE(10) / WRITE(12)** | stream data-out to the medium |
| 0x2F | VERIFY(10) | ready-check only |
| 0x5A | MODE SENSE(10) | 8-byte zero header |
| other | — | unsupported-command sense |

### 5.4 The LUN → partition-B data path (the point of the whole feature)

READ/WRITE set up a stream at byte offset `LBA × block_size`, length
`count × block_size`; a pump loop then moves block-sized chunks between the 32 KiB USB
buffer and the **LUN's medium object — the same partition-B (user data) medium the pen
uses internally**, via its read/write vtable. There is **no MBR or partition-table
translation: LBA 0 is partition-B sector 0** (a "superfloppy" — the FAT filesystem starts
at LBA 0; see `nand-image-layout.md` for the on-media format). Consequently, in an
emulator with an authentic NAND back-end, a host READ(10) returns real partition-B bytes
and a host WRITE(10) flows through the firmware's own NFTL/flash-translation code and
**persists in the NAND image** — no emulator-side storage glue is needed at all. Every
access is gated on a medium-ready check. (Observed data flow.)

### 5.5 Vendor command channel (avoid in an emulated host)

Opcodes 0xCC–0xE2, each gated by an ASCII magic in the CDB (`"ANYKA"`, `"FM"`, `"NO"`,
`"AD"`), form a production/manufacturing back-channel (write serial, format, push config,
stream out a product log file). **Warning:** opcode 0xCC (`"ANYKA"`) sets phase 7, whose
exit path reflashes and then **parks the firmware in an infinite loop** by design. A
scripted host should never send these. (Observed.)

---

## 6. Emulator recipe: a scripted virtual host (opt-in)

Zero firmware hooks: the firmware runs its own complete USB device stack; the emulator
supplies only what the MUSB hardware plus a PC host would. Four pieces, in dependency
order. When the feature is **off**, none of this may run — the §1 defaults (GPIO8 = 0,
MUSB reads 0) are the whole story and the boot must be bit-identical to a non-USB build.

1. **MUSB register file** (§3.1): RAM-backed bytes/words at the listed offsets, with a
   3-endpoint bank behind INDEX. Only the §3.4 bits need semantics; everything else is
   scratch. INTRUSB/INTRTX/INTRRX are read-clear. All reset values 0.
2. **Connect handshake:** set GPIO8 = 1. When the firmware writes POWER = 0x21 (classify
   state entry), start latching INTRUSB **bit2 (Reset)** and, on subsequent reads,
   **bit3 (SOF)** — the classifier needs activity after the first settle tick but within
   the 0x28-tick window, so begin from the second sample. Result: "PC" classification →
   state 5, and the firmware opens the device and advertises.
3. **Enumeration script:** after the firmware brings up the PHY, pulse INTRUSB bit2 +
   IRQ line 6 once (→ the firmware runs its bus reset and re-arms the endpoints), then
   run §4.4. Each SETUP = write the 8 bytes to the EP0 RX shadow (`0x08006A40`), set
   RXCOUNT(EP0) = 8 and CSR0 RxPktRdy, latch INTRTX bit0, assert line 6. Consume the
   firmware's IN replies when it arms CSR0 TxPktRdy (staged length in +0x330); clear
   TxPktRdy and re-interrupt for the next stage. Verify the replies against §4.2.
4. **BOT host loop:** to deliver a CBW, stage 31 bytes in the bulk PIO window, set
   RXCOUNT(EP1) = 0x1F, RXCSR RxPktRdy, INTRRX bit1, line 6. Then behave exactly like a
   `usb-storage` host: INQUIRY → TEST UNIT READY → READ CAPACITY(10) → READ(10)/WRITE(10),
   honoring the §3.4 TXCSR/RXCSR handshake for PIO phases and **servicing the DMA-engine
   submits** (channels 2 = EP1-RX, 3 = EP2-TX, physical addresses) by copying between the
   DRAM buffer and the virtual endpoint. CSWs arrive as 13-byte EP2 transfers; check tag,
   residue, status.
5. **Unplug:** drop GPIO8 → the service loop exits cleanly (PHY off, buffers freed), the
   firmware re-mounts B: itself, and the statechart returns to standby. This is the
   normal, clean way to end a session.

**Timing tolerance (Observed):** nothing is hard-real-time — the classifier gives a
0x28-tick window, the service loop polls freely, and transfers are IRQ-paced at the
host's leisure. The strict parts are the read-clear discipline on the interrupt registers
and never asserting line 6 without a staged event.

---

## 7. Open points / gaps

- The L2 staging addresses (`0x08006400`, `0x08006600`, `0x08006A40`) and the DMA
  channel-code assignment (2/3) are pool-derived — re-verify against the exact firmware
  image before implementation (§3.3).
- Whether `usb_wait_host_sof` is ever reached in this firmware (no direct caller found;
  possibly pointer-called or vestigial) — it only matters as a §1.2 poller.
- The precise INTRUSB timing the real PHY produces during attach (Reset-then-SOF spacing)
  is Inferred from the classifier's acceptance window, not bus-captured; the recipe in
  §6.2 satisfies the firmware but has not been compared against a hardware trace.
- Suspend/Resume (INTRUSB bits 0/1) handling is untested territory: the firmware reads
  them as generic "bus event" flags; a scripted host should simply never set them.
