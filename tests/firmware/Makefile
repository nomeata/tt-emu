# Makefile — cross-compile the tt-emu bare-metal test blobs.
#
# Dependency: an `arm-none-eabi-gcc` toolchain on PATH (any recent GNU Arm
# Embedded release works; no libc or startup files are used).  On NixOS:
# `nix-shell -p gcc-arm-embedded`; Debian/Ubuntu: `apt install gcc-arm-none-eabi`.
# The pytest wrapper (tests/test_firmware_blobs.py) builds with the same
# flags by invoking the compiler directly (so `make` itself is optional) and
# skips the blob tests with a clear message when the toolchain is absent.
# Keep TESTS/CFLAGS in sync with tests/test_firmware_blobs.py.
#
# Each <name>.c becomes build/<name>.bin: a raw flat image linked at
# 0x08000000 (blob.ld) with the start.S vector table first, run by
# tt_emu.test_harness.TestBench.

CROSS   ?= arm-none-eabi-
CC       = $(CROSS)gcc
OBJCOPY  = $(CROSS)objcopy

CFLAGS   = -mcpu=arm926ej-s -marm -ffreestanding -nostdlib -Os \
           -Wall -Wextra -Werror -g
LDFLAGS  = -T blob.ld -nostdlib -Wl,--build-id=none

TESTS    = smoke chip_id gpio timer_irq nand dac_dma poweroff
BINS     = $(TESTS:%=build/%.bin)

all: $(BINS)

build:
	mkdir -p build

build/%.elf: %.c start.S blob.ld tt_test.h | build
	$(CC) $(CFLAGS) $(LDFLAGS) -o $@ start.S $<

build/%.bin: build/%.elf
	$(OBJCOPY) -O binary $< $@

clean:
	rm -rf build

.PHONY: all clean
.PRECIOUS: build/%.elf
