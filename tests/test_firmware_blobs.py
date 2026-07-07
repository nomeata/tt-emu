"""Cross-compiled firmware-blob tests: exercise the emulator's peripheral
contracts with tiny bare-metal ARM programs (``tests/firmware/``) run through
:mod:`tt_emu.test_harness`.

Requires ``arm-none-eabi-gcc`` on PATH; the whole module is skipped with a
clear message otherwise, so the suite stays green on hosts without the
cross-toolchain.  The build here invokes the compiler directly (no ``make``
needed) with the same flags as ``tests/firmware/Makefile`` — keep the two in
sync.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from tt_emu.machine import MachineConfig
from tt_emu.nand_image import BLOCK_SIZE, NandImage
from tt_emu.test_harness import BlobResult, TestBench

FIRMWARE_DIR = Path(__file__).parent / "firmware"
BUILD_DIR = FIRMWARE_DIR / "build"

#: Blob source names (Makefile TESTS) and the Makefile's build flags.
BLOB_NAMES = ("smoke", "chip_id", "gpio", "timer_irq", "nand", "dac_dma", "poweroff")
CFLAGS = [
    "-mcpu=arm926ej-s", "-marm", "-ffreestanding", "-nostdlib", "-Os",
    "-Wall", "-Wextra", "-Werror", "-g",
    "-T", "blob.ld", "-Wl,--build-id=none",
]

pytestmark = pytest.mark.skipif(
    shutil.which("arm-none-eabi-gcc") is None,
    reason="arm-none-eabi-gcc not on PATH — install a GNU Arm Embedded "
    "toolchain to run the cross-compiled firmware-blob tests",
)

# --- Contract constants shared with the .c sources --------------------------------

#: nand.c: data byte i = (i*7+3)&0xFF at flat offset 2*BLOCK_SIZE; tag at row 0x0200.
NAND_TEST_OFFSET = 2 * BLOCK_SIZE
NAND_TEST_ROW = 0x0200
NAND_TEST_DATA = bytes((i * 7 + 3) & 0xFF for i in range(512))
NAND_TEST_TAG = b"TTEMUTAG"

#: dac_dma.c: pcm[i] = (i16)(i*3 - 128), 256 samples.
DAC_TEST_PCM = b"".join(
    ((i * 3 - 128) & 0xFFFF).to_bytes(2, "little") for i in range(256)
)


def _build_blob(name: str) -> bytes:
    """Compile one blob to a raw image (same recipe as tests/firmware/Makefile)."""
    elf = BUILD_DIR / f"{name}.elf"
    binary = BUILD_DIR / f"{name}.bin"
    for cmd in (
        ["arm-none-eabi-gcc", *CFLAGS, "-o", str(elf), "start.S", f"{name}.c"],
        ["arm-none-eabi-objcopy", "-O", "binary", str(elf), str(binary)],
    ):
        proc = subprocess.run(cmd, cwd=FIRMWARE_DIR, capture_output=True, text=True)
        if proc.returncode != 0:
            pytest.fail(
                f"firmware blob build failed: {' '.join(cmd)}\n"
                f"{proc.stdout}\n{proc.stderr}",
                pytrace=False,
            )
    return binary.read_bytes()


@pytest.fixture(scope="session")
def blobs() -> dict[str, bytes]:
    """Cross-compile the blobs once per session and return name -> raw image."""
    BUILD_DIR.mkdir(exist_ok=True)
    return {name: _build_blob(name) for name in BLOB_NAMES}


def _run(
    blobs: dict[str, bytes], name: str, bench: TestBench | None = None
) -> tuple[BlobResult, TestBench]:
    assert name in blobs, f"blob {name!r} not built (Makefile TESTS out of date?)"
    bench = bench or TestBench(config=MachineConfig())
    result = bench.run_blob(blobs[name])
    return result, bench


# --- the harness itself -------------------------------------------------------------


def test_smoke(blobs: dict[str, bytes]) -> None:
    result, _ = _run(blobs, "smoke")
    assert result.passed, result.describe()
    assert result.detail == 0x12345678
    assert result.stop_reason == "blob signalled a result"


# --- boot constants (chip-ID, clock latches, battery, ECC/NFC, USB) -----------------


def test_chip_id_and_boot_constants(blobs: dict[str, bytes]) -> None:
    result, _ = _run(blobs, "chip_id")
    assert result.passed, result.describe()
    assert result.detail == 0x30393031  # the chip-ID the blob read


# --- GPIO register contract ----------------------------------------------------------


def test_gpio(blobs: dict[str, bytes]) -> None:
    result, _ = _run(blobs, "gpio")
    assert result.passed, result.describe()
    assert result.detail == 0x3201  # final idle GPIO_IN read-back


def test_poweroff_stops_machine(blobs: dict[str, bytes]) -> None:
    """GPIO15 1->0 is the pen's clean power-off and must stop the run."""
    result, _ = _run(blobs, "poweroff")
    assert result.status is None, result.describe()
    assert "powered off" in result.stop_reason, result.describe()
    assert result.detail == 0x0FF0FF  # the blob got as far as its marker


# --- timer1 + IRQ delivery -------------------------------------------------------------


def test_timer_irq_delivery(blobs: dict[str, bytes]) -> None:
    result, bench = _run(blobs, "timer_irq")
    assert result.passed, result.describe()
    assert result.detail == 3  # three handled timer IRQs
    assert bench.intc.timer_irqs >= 3
    assert bench.machine.irqs_delivered >= 3


# --- NAND controller: READ-ID, STATUS, page read, tag read ------------------------------


def test_nand_controller(blobs: dict[str, bytes]) -> None:
    image = NandImage()
    image.program(NAND_TEST_OFFSET, NAND_TEST_DATA)
    image.set_tag(NAND_TEST_ROW, NAND_TEST_TAG)
    bench = TestBench(nand_image=image)
    result, bench = _run(blobs, "nand", bench)
    assert result.passed, result.describe()
    assert result.detail == 0x9551D3EC  # the READ-ID answer
    assert bench.nand.reads >= 1


# --- audio DAC DMA: submit, capture, paced completion IRQ -------------------------------


def test_dac_dma(blobs: dict[str, bytes]) -> None:
    result, bench = _run(blobs, "dac_dma")
    assert result.passed, result.describe()
    assert result.detail == 512  # bytes submitted
    assert bench.audio.dac_submits == 1
    assert bench.audio.completions == 1
    assert bench.audio.unresolved_submits == 0
    # The captured chunk is the blob's PCM pattern, at the default 22050 Hz.
    assert bench.audio.capture.pcm() == DAC_TEST_PCM
    assert bench.audio.capture.wav_rate == 22050
