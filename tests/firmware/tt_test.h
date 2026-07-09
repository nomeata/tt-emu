/* tt_test.h — bare-metal test-blob support for the tt-emu emulator.
 *
 * Register addresses are taken from the tt-emu hardware docs (docs/index.md
 * quick reference and the per-peripheral files); each define cites its doc.
 * The result-signalling convention (the "mailbox") is shared with the Python
 * harness in src/tt_emu/test_harness.py — keep the two in sync.
 *
 * Test blobs are freestanding ARM (arm926ej-s) programs linked at 0x08000000
 * (see blob.ld / start.S): the vector table sits at the load address so the
 * emulator's fixed IRQ vector 0x08000018 lands in the blob, `_start` sets up
 * a stack and calls `main`, and IRQs arrive on `test_irq_handler` (weak
 * default provided by start.S; define your own to handle them).
 */

#ifndef TT_TEST_H
#define TT_TEST_H

typedef unsigned char u8;
typedef unsigned short u16;
typedef unsigned int u32;
typedef signed short i16;

/* A 32-bit MMIO / memory register. */
#define REG32(addr) (*(volatile u32 *)(addr))
#define REG8(addr) (*(volatile u8 *)(addr))

/* --- Result mailbox (harness convention, src/tt_emu/test_harness.py) ------------
 *
 * The blob reports by writing DETAIL first, then STATUS; the harness polls
 * STATUS between execution chunks and stops the machine when it becomes
 * PASS or FAIL. After signalling, the blob parks in an endless loop.
 * The mailbox lives at the top of main RAM, far above any blob content.
 */
#define TT_MAILBOX 0x083FF000u
#define TT_STATUS REG32(TT_MAILBOX + 0x0)
#define TT_DETAIL REG32(TT_MAILBOX + 0x4)

#define TT_STATUS_PASS 0x600DCAFEu
#define TT_STATUS_FAIL 0xBAD0DEADu

#define TEST_PASS(v)                                                           \
    do {                                                                       \
        TT_DETAIL = (u32)(v);                                                  \
        TT_STATUS = TT_STATUS_PASS;                                            \
        for (;;)                                                               \
            ;                                                                  \
    } while (0)

#define TEST_FAIL(v)                                                           \
    do {                                                                       \
        TT_DETAIL = (u32)(v);                                                  \
        TT_STATUS = TT_STATUS_FAIL;                                            \
        for (;;)                                                               \
            ;                                                                  \
    } while (0)

/* Assert helper: fail with the checked value as the detail. */
#define TEST_CHECK(cond, v)                                                    \
    do {                                                                       \
        if (!(cond))                                                           \
            TEST_FAIL(v);                                                      \
    } while (0)

/* --- SoC core block 0x04000000 (system-control-and-clock.md, index.md) --------- */

#define CHIP_ID REG32(0x04000000)      /* constant 0x30393031 ("1090") */
#define CHIP_ID_VALUE 0x30393031u
#define CLK_DIV REG32(0x04000004)      /* bits 12/13/14/21 self-clearing */
#define ANALOG_PD REG32(0x04000010)    /* bit8 (rate-apply strobe) reads 0 */

/* --- Interrupt controller + timer1 (interrupts-and-timers.md) ------------------ */

#define TIMER1_CTRL REG32(0x04000018)  /* reload[25:0], bit27 enable, bit28 ACK */
#define INT_ENABLE REG32(0x04000034)
#define TIMER_STAT REG32(0x0400004C)   /* bit17 timer fired, bit20 GPIO cause */
#define INT_PENDING REG32(0x040000CC)  /* level; 0 when idle */

#define TIMER1_ENABLE (1u << 27)
#define TIMER1_ACK (1u << 28)
#define TIMER1_RELOAD_20MS 240000u     /* one 20 ms tick */
#define STAT_TIMER_FIRED (1u << 17)
#define IRQ_LINE_AUDIO (1u << 0)
#define IRQ_LINE_TIMER (1u << 10)

/* --- Battery ADC (battery-and-power.md §7) -------------------------------------- */

#define BATTERY_ENABLE REG32(0x04000064) /* firmware writes 0x200 */
#define BATTERY_ADC REG32(0x04000070)    /* healthy constant 0x000C0000 */
#define BATTERY_ADC_HEALTHY 0x000C0000u

/* --- GPIO bank 0 (gpio-buttons-led.md §1/§2) ------------------------------------ */

#define GPIO_DIR0 REG32(0x0400007C)   /* bit = 1 -> input; boot seed 0x3100 */
#define GPIO_OUT0 REG32(0x04000080)   /* output latch, reads back */
#define GPIO_IN0 REG32(0x040000BC)    /* idle retail word 0x00003201 */
#define GPIO_INT_EN0 REG32(0x040000E0)
#define GPIO_INT_POL0 REG32(0x040000F0)

#define GPIO_IN_IDLE 0x00003201u
#define GPIO_PIN_POWER_HOLD (1u << 15) /* 1->0 = pen powers itself off */
#define GPIO_PIN_AMP (1u << 16)        /* out latch mirrored into GPIO_IN */

/* --- Audio DMA / L2 buffer 0x04010000 (audio-dac-dma.md §2,
 *     nand-and-nfc-controller.md §7) --------------------------------------------- */

#define DMA_CTRL REG32(0x04010000)      /* bit16 kick/GO; ISR clear = line-0 ACK */
#define DMA_SRC REG32(0x04010004)       /* memory source: phys & 0x3ffff (aperture offset) */
#define DMA_DST REG32(0x04010008)       /* DAC port destination 0x08086200 */
#define DMA_WORDCOUNT REG32(0x0401000C) /* (len/4)|bit13 START; bit13 reads clear */
#define L2_BUF_STATUS REG32(0x04010010) /* bits[19:16] = buffer-4 fill (64-B chunks) */

#define DMA_KICK (1u << 16)
#define DMA_START (1u << 13)
#define DAC_PORT_DST 0x08086200u
#define DMA_SRC_WINDOW(cpu_addr) (((u32)(cpu_addr)) & 0x3FFFFu)

/* --- Audio clock (system-control-and-clock.md; boot checklist) ------------------ */

#define AUDIO_CLOCK REG32(0x04036004) /* bit19 must read 1 */

/* --- NAND flash controller 0x0404A000 (nand-and-nfc-controller.md §5/§8) -------- */

#define NFC_CMDLIST ((volatile u32 *)0x0404A100) /* micro-op FIFO ..+0x14F */
#define NFC_DATA_RD0 REG32(0x0404A150)
#define NFC_DATA_RD1 REG32(0x0404A154)
#define NFC_CTRL_STATUS REG32(0x0404A158) /* write bit30 = GO; read bit31 = ready */

#define NFC_GO (1u << 30)
#define NFC_READY (1u << 31)

/* Micro-op words (§5.2): low bits select the op, bit0 = LAST in list. */
#define NFC_OP_CMD(cmd) (((u32)(cmd) << 11) | 0x64u)   /* command cycle (CLE) */
#define NFC_OP_ADDR(byte) (((u32)(byte) << 11) | 0x62u) /* address cycle (ALE) */
#define NFC_OP_DATA_RD(n) ((((u32)(n)-1) << 11) | 0x118u) /* NAND->ECC->L2, n bytes */
#define NFC_OP_LAST 0x1u

/* NAND command bytes (§8). */
#define NAND_CMD_READ_SETUP 0x00
#define NAND_CMD_READ_CONFIRM 0x30
#define NAND_CMD_STATUS 0x70
#define NAND_CMD_READ_ID 0x90
#define NAND_CMD_RESET 0xFF

#define NAND_READ_ID_VALUE 0x9551D3ECu /* Samsung K9GAG08U0M (§8.5) */
#define NAND_STATUS_VALUE 0xC0u        /* not-protected + ready (§8.4) */

/* --- ECC engine 0x0405B000 (§6) -------------------------------------------------- */

#define ECC_CTRL REG32(0x0405B000)      /* reads 0x7000040 (complete + pass) */
#define ECC_STATUS_PASS 0x07000040u
#define ECC_CONFIG_PAYLOAD(n) ((u32)(n) << 7) /* payload length in bits[18:7] */

/* The 512-byte circular L2 SRAM window, buffer 4 (§7) — plain RAM. */
#define NAND_SRAM_WINDOW ((volatile u8 *)0x08006800)

/* --- USB (usb-musb-device.md §1: dead-bus defaults) ------------------------------ */

#define USB_BASE REG32(0x04070000) /* reads 0, never 0xFFFFFFFF */

#endif /* TT_TEST_H */
