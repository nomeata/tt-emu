"""Unit tests for the OID sensor and audio DAC/DMA models.

The OID tests emulate the firmware side of the two-wire protocol
(``oid-sensor.md`` §3.2) against the sensor model and check that the decoded
word is exactly what the real capture paths would compute. The audio tests
exercise the submit protocol, the ring-source recovery, the teardown flush, and
the paced line-0 completion of ``audio-dac-dma.md`` §2/§4/§6/§7.
"""

from __future__ import annotations

import struct
import wave
from pathlib import Path

import pytest

from tt_emu.audio_capture import AudioCapture
from tt_emu.machine import Machine, MachineConfig
from tt_emu.peripherals.audio import (
    AudioDma,
    DAC_PORT_DST,
    DMA_CTRL,
    DMA_DST,
    DMA_KICK,
    DMA_SRC,
    DMA_START,
    DMA_WORDCOUNT,
    RING_BASE,
    RING_BODY,
    RING_READ,
    RING_SIZE,
    SWALLOW_FLAG_ADDR,
    rate_from_divider,
)
from tt_emu.peripherals.gpio import GPIO_DIR0, GPIO_IN0, GPIO_OUT0, GpioBlock
from tt_emu.peripherals.intc import IntcTimer, LINE_AUDIO
from tt_emu.peripherals.nand import EccEngine, NfcController
from tt_emu.nand_image import NandImage
from tt_emu.peripherals.oid import BIT_COUNT_ADDR, OidSensor, frame32
from tt_emu.peripherals.syscon import SysCon

# --- OID frame encoding (oid-sensor.md §2) ------------------------------------------


def test_frame32_worked_example() -> None:
    # §2 worked example: N = 42 -> 0x800055F0.
    assert frame32(42) == 0x800055F0


@pytest.mark.parametrize("oid", [1, 42, 4716, 0xFFFF, 0x3FFFF])
def test_frame32_structure(oid: int) -> None:
    f = frame32(oid)
    assert (f >> 30) == 0b10  # type bits "10" = decoded OID data frame
    assert (f >> 27) & 0b111 == 0  # reserved
    assert (f >> 9) & 0x3FFFF == oid  # index field
    assert (f >> 8) & 1 == 1  # valid bit
    check = f & 0xFF
    assert ((check + (check >> 4) + 1) & 0xF) == 0  # §2 check-byte rule
    # 23-bit capture (frame bits 31..9) yields the code word 0x400000 | oid.
    assert (f >> 9) == 0x400000 | oid


# --- firmware-side wire protocol simulation (§3.2) ------------------------------------


class _FwSide:
    """Bit-bangs the firmware's shift-in sequence against the GPIO block."""

    def __init__(self, machine: Machine, gpio: GpioBlock) -> None:
        self.machine = machine
        self.gpio = gpio

    def data_in(self) -> int:
        return (self.gpio.read_reg(GPIO_IN0) >> 9) & 1

    def set_clock(self, level: int) -> None:
        out = self.gpio.read_reg(GPIO_OUT0)
        self.gpio.write_reg(GPIO_OUT0, (out & ~(1 << 2)) | (level << 2))

    def set_data_out(self, level: int) -> None:
        out = self.gpio.read_reg(GPIO_OUT0)
        self.gpio.write_reg(GPIO_OUT0, (out & ~(1 << 9)) | (level << 9))

    def set_data_dir(self, is_input: int) -> None:
        d = self.gpio.read_reg(GPIO_DIR0)
        self.gpio.write_reg(GPIO_DIR0, (d & ~(1 << 9)) | (is_input << 9))

    def shift_in(self, bit_count: int) -> int | None:
        """The §3.2 capture sequence; returns the raw word or None on abort."""
        self.machine.write_u8(BIT_COUNT_ADDR, bit_count)
        if self.data_in() != 0:
            return None  # attention must be LOW
        self.set_clock(1)
        self.set_data_dir(1)
        if self.data_in() != 1:
            return None  # sensor must ACK by releasing HIGH
        self.set_data_dir(0)  # host ACK: drive low, pulse the clock
        self.set_data_out(0)
        self.set_clock(0)
        self.set_clock(1)
        self.set_data_dir(1)  # release; bits follow
        raw = 0
        for _ in range(bit_count):
            self.set_clock(1)
            raw <<= 1
            self.set_clock(0)
            raw |= self.data_in()
        return raw

    def send_command(self, byte: int) -> None:
        """A §3.3 host->sensor command: GPIO9 as output, 8 falling-edge bits."""
        self.set_data_dir(0)
        self.set_clock(1)
        for i in range(8):
            self.set_data_out((byte >> (7 - i)) & 1)
            self.set_clock(0)
            self.set_clock(1)
        self.set_clock(0)
        self.set_data_dir(1)


@pytest.fixture()
def oid_rig() -> tuple[Machine, GpioBlock, OidSensor, _FwSide]:
    machine = Machine(MachineConfig())
    gpio = GpioBlock()
    machine.add_peripheral(gpio)
    sensor = OidSensor(gpio)
    machine.add_peripheral(sensor)
    return machine, gpio, sensor, _FwSide(machine, gpio)


def _settle(sensor: OidSensor) -> None:
    sensor.tick(0)
    sensor.tick(0)


def test_oid_idle_no_attention_and_abort(oid_rig) -> None:
    _machine, _gpio, sensor, fw = oid_rig
    assert fw.data_in() == 1  # bus idle: data latched high (§3.1)
    assert fw.shift_in(23) is None  # capture gate closed without a tap
    assert not sensor.pending


def test_oid_23bit_gameplay_capture(oid_rig) -> None:
    _machine, _gpio, sensor, fw = oid_rig
    sensor.tap(4716)
    assert fw.data_in() == 0  # attention asserted
    raw = fw.shift_in(23)
    assert raw == 0x400000 | 4716  # the code word (§4.1 step 2)
    assert (raw & 0x600000) == 0x400000  # passes the type check (§4.1 step 3)
    _settle(sensor)
    assert fw.data_in() == 1  # tap-and-lift: pen looks lifted (§6 item 5)
    assert sensor.taps_served == 1
    assert not sensor.pending


def test_oid_32bit_status_poll_capture(oid_rig) -> None:
    _machine, _gpio, sensor, fw = oid_rig
    sensor.tap(42)
    raw = fw.shift_in(32)
    assert raw == frame32(42) == 0x800055F0
    assert raw & 0x100  # valid bit (§4.2 step 2)
    b = raw & 0xFF
    assert ((b + (b >> 4) + 1) & 0xF) == 0  # check byte passes
    assert (raw >> 9) == 0x400000 | 42
    _settle(sensor)
    assert sensor.taps_served == 1


def test_oid_command_does_not_desync_pending_frame(oid_rig) -> None:
    _machine, _gpio, sensor, fw = oid_rig
    sensor.tap(1234)
    fw.send_command(0xA6)  # per-tap acknowledge byte (§3.3)
    assert sensor.pending
    assert fw.data_in() == 0  # frame re-armed: attention still asserted (§6)
    raw = fw.shift_in(23)
    assert raw == 0x400000 | 1234
    _settle(sensor)
    assert sensor.taps_served == 1


def test_oid_trigger_pulse_recovery(oid_rig) -> None:
    # §3.4/§6: a long clock-high pulse hits an armed frame; on the pulse's end
    # the sensor must re-assert attention so the following shift-in finds it.
    _machine, _gpio, sensor, fw = oid_rig
    sensor.tap(77)
    fw.set_clock(1)  # trigger pulse begins: frame advances to ACK
    assert fw.data_in() == 1
    fw.set_clock(0)  # pulse ends
    assert fw.data_in() == 0  # attention recovered
    raw = fw.shift_in(32)
    assert raw == frame32(77)


def test_oid_hold_reserves_until_lift(oid_rig) -> None:
    # §6 "Repeat / anti-repeat": press-and-hold re-serves the frame every
    # capture (this is what survives the standby 32-bit status polls, §4.2).
    _machine, _gpio, sensor, fw = oid_rig
    sensor.hold(42)
    assert fw.shift_in(32) == frame32(42)  # a status poll eats one frame …
    _settle(sensor)
    assert fw.data_in() == 0  # … but the code is still pressed (re-armed)
    assert fw.shift_in(23) == 0x400000 | 42  # the gameplay poll gets the next
    _settle(sensor)
    assert sensor.taps_served == 2
    sensor.lift()
    assert fw.data_in() == 1  # pen lifted: attention released
    assert not sensor.pending
    assert fw.shift_in(23) is None


def test_oid_status_poll_answer_opt_in(oid_rig) -> None:
    # Opt-in sleep handshake (§4.2): with answer_status_polls a bare trigger
    # pulse in IDLE offers the status frame; a 32-bit poll then reads it.
    machine, gpio, _sensor, fw = oid_rig
    from tt_emu.peripherals.oid import OidSensor, STATUS_FRAME

    sensor = OidSensor(gpio, answer_status_polls=True)
    machine.add_peripheral(sensor)
    fw.set_clock(1)  # trigger pulse arms the status frame
    fw.set_clock(0)
    assert fw.data_in() == 0  # attention asserted for the status frame
    assert fw.shift_in(32) == STATUS_FRAME
    _settle(sensor)
    assert sensor.status_frames_served == 1
    assert not sensor.pending  # status frames don't count as pending taps


def test_oid_idle_ignores_trigger_by_default(oid_rig) -> None:
    # Default (answer_status_polls off): trigger pulses in IDLE are ignored
    # (§3.4), so no spurious status frame / sleep handshake.
    _machine, _gpio, sensor, fw = oid_rig
    fw.set_clock(1)
    fw.set_clock(0)
    assert fw.data_in() == 1  # still idle, no attention
    assert not sensor.pending


def test_oid_queued_taps_serve_in_order(oid_rig) -> None:
    _machine, _gpio, sensor, fw = oid_rig
    sensor.tap(10)
    sensor.tap(20)
    assert fw.shift_in(23) == 0x400000 | 10
    _settle(sensor)
    assert fw.data_in() == 0  # second tap armed
    assert fw.shift_in(23) == 0x400000 | 20
    _settle(sensor)
    assert not sensor.pending


# --- audio DMA (audio-dac-dma.md) -----------------------------------------------------

RING_PTR = 0x0810_0000  # test PCM ring buffer location in main RAM
IPT = 20_000  # instructions per 20 ms tick


@pytest.fixture()
def audio_rig() -> tuple[Machine, AudioDma, IntcTimer, SysCon]:
    machine = Machine(MachineConfig(instructions_per_tick=IPT))
    gpio = GpioBlock()
    intc = IntcTimer(gpio)
    syscon = SysCon()
    audio = AudioDma(NfcController(NandImage(), EccEngine()), intc, syscon, gpio)
    machine.add_peripheral(gpio)
    machine.add_peripheral(intc)
    machine.add_peripheral(syscon)
    machine.add_peripheral(audio)
    machine.intc = intc
    # Build the PCM ring the firmware would have (§5): base/size/read pointer.
    machine.write_u32(RING_BODY + RING_BASE, RING_PTR)
    machine.write_u32(RING_BODY + RING_SIZE, 0x3000)
    machine.write_u32(RING_BODY + RING_READ, 0)
    # DAC rate divider: the Observed 22050 Hz code (§5 data point).
    syscon.write_reg(0x08, (0x46 << 13) | (1 << 24))
    return machine, audio, intc, syscon


def _submit(audio: AudioDma, src: int, length: int) -> None:
    """The firmware's §2 submit protocol at register level."""
    assert audio.read(DMA_WORDCOUNT, 4) & DMA_START == 0  # slot free (§2)
    audio.write(DMA_SRC, 4, src & 0x3FFFF)
    audio.write(DMA_DST, 4, DAC_PORT_DST)
    audio.write(DMA_WORDCOUNT, 4, (length // 4) | DMA_START)
    audio.write(DMA_CTRL, 4, audio.read(DMA_CTRL, 4) | DMA_KICK)


def test_audio_capture_and_paced_completion(audio_rig) -> None:
    machine, audio, intc, _syscon = audio_rig
    pattern = bytes(range(256)) * 4  # 0x400 bytes
    machine.write_bytes(RING_PTR, pattern)
    _submit(audio, RING_PTR, 0x400)

    # Captured exactly the submitted bytes, tagged 22050 Hz.
    assert audio.capture.total_bytes == 0x400
    assert audio.capture.chunks[0].data == pattern
    assert audio.capture.chunks[0].rate == 22050
    # START/BUSY reads back clear (§7 item 3).
    assert audio.read(DMA_WORDCOUNT, 4) & DMA_START == 0

    # Pacing (§4): 0x400 bytes = 256 frames / 22050 Hz = 11.61 ms = 0.58 ticks.
    expected = 0x400 * IPT * 50 // (4 * 22050)  # = 11609 instructions
    audio.tick(expected - 200)
    assert not intc.pending() & (1 << LINE_AUDIO)  # not before the paced time
    audio.tick(expected + 200)
    assert intc.pending() & (1 << LINE_AUDIO)  # completion delivered
    assert audio.completions == 1

    # The ISR's kick-clear is the ACK (§3): line 0 must drop.
    audio.write(DMA_CTRL, 4, audio.read(DMA_CTRL, 4) & ~DMA_KICK)
    assert not intc.pending() & (1 << LINE_AUDIO)


def test_audio_source_recovery_post_advance(audio_rig) -> None:
    # The dequeue may advance the read pointer before the submit; the source
    # register's low 18 bits disambiguate (§7 item 2).
    machine, audio, _intc, _syscon = audio_rig
    pattern = b"\x11\x22\x33\x44" * 0x100
    machine.write_bytes(RING_PTR, pattern)
    machine.write_u32(RING_BODY + RING_READ, 0x400)  # already advanced past chunk 0
    _submit(audio, RING_PTR, 0x400)  # chunk really started at offset 0
    assert audio.capture.chunks[0].data == pattern


def test_audio_ring_wraparound_read(audio_rig) -> None:
    machine, audio, _intc, _syscon = audio_rig
    machine.write_bytes(RING_PTR + 0x3000 - 0x200, b"\xAA" * 0x200)
    machine.write_bytes(RING_PTR, b"\xBB" * 0x200)
    machine.write_u32(RING_BODY + RING_READ, 0x3000 - 0x200)
    _submit(audio, RING_PTR + 0x3000 - 0x200, 0x400)
    assert audio.capture.chunks[0].data == b"\xAA" * 0x200 + b"\xBB" * 0x200


def test_audio_teardown_flush_not_captured(audio_rig) -> None:
    # §6: swallow-flag submits (the silence flush) complete but don't record.
    machine, audio, intc, _syscon = audio_rig
    machine.write_u8(SWALLOW_FLAG_ADDR, 1)
    _submit(audio, RING_PTR, 0x800)
    assert audio.capture.total_bytes == 0
    assert audio.flush_submits == 1
    audio.tick(10**9)  # completion must still be delivered (the firmware spins)
    assert intc.pending() & (1 << LINE_AUDIO)


def test_audio_spurious_status_reads_zero(audio_rig) -> None:
    _machine, audio, _intc, _syscon = audio_rig
    assert audio.read(0x1C, 4) == 0  # §2: else the ISR treats IRQs as spurious


@pytest.mark.parametrize(
    "divider,rate",
    [(0x46, 22050), (0, 22050), (0x61, 16000), (0x30, 32000), (0x22, 44100), (0xC3, 8000)],
)
def test_rate_from_divider(divider: int, rate: int) -> None:
    assert rate_from_divider(divider) == rate


# --- WAV writer -------------------------------------------------------------------------


def test_wav_writer_and_stats(tmp_path: Path) -> None:
    cap = AudioCapture()
    quiet = struct.pack("<4h", 0, 0, 0, 0)
    loud = struct.pack("<4h", 1000, -2000, 30000, -5)
    cap.append(0, 8000, quiet)  # bring-up-rate chunk: must not set the WAV rate
    cap.append(1, 22050, loud)
    stats = cap.write_wav(tmp_path / "out.wav")
    assert stats.rate == 22050
    assert stats.peak == 30000
    assert stats.nonzero_pct == pytest.approx(50.0)
    assert stats.total_bytes == 16
    assert stats.duration_s == pytest.approx(4 / 22050)
    with wave.open(str(tmp_path / "out.wav"), "rb") as w:
        assert w.getnchannels() == 2
        assert w.getsampwidth() == 2
        assert w.getframerate() == 22050
        assert w.readframes(w.getnframes()) == quiet + loud
