/* dac_dma.c — audio DAC DMA submit/completion contract (audio-dac-dma.md).
 *
 * Performs one DAC playback submit exactly as the firmware does (§2/§7):
 * poll bit13 idle, program source/destination, START with the word count,
 * kick bit16 — then waits for the paced line-0 completion IRQ and ACKs it by
 * clearing the kick bit (§3).  The DAC engine is a physical bus master: it
 * reads the buffer at the source register's 18-bit aperture offset (§2); the
 * pytest side verifies the captured bytes equal the pattern below.
 *
 * Pattern contract with tests/test_firmware_blobs.py:
 *   pcm[i] = (i16)(i * 3 - 128), 256 samples = 512 bytes.
 */

#include "tt_test.h"

#define FAILV(n, got) TEST_FAIL(((u32)(n) << 28) | ((got)&0x0FFFFFFFu))

static i16 pcm[256]; /* 512 bytes of S16LE samples */
static volatile u32 done;

void test_irq_handler(void)
{
    /* Line 0 must be pending on entry (interrupts-and-timers §2). */
    u32 pending = INT_PENDING;
    if (!(pending & IRQ_LINE_AUDIO))
        FAILV(1, pending);

    /* The ISR's kick-clear is the line-0 ACK (§3); pending must drop. */
    DMA_CTRL = DMA_CTRL & ~DMA_KICK;
    pending = INT_PENDING;
    if (pending & IRQ_LINE_AUDIO)
        FAILV(2, pending);

    done = 1;
}

int main(void)
{
    for (u32 i = 0; i < 256; i++)
        pcm[i] = (i16)(i * 3 - 128);

    done = 0;
    INT_ENABLE = IRQ_LINE_AUDIO;

    /* Submit protocol (§2): poll idle, src/dst, START, kick. */
    while (DMA_WORDCOUNT & DMA_START)
        ;
    DMA_SRC = DMA_SRC_WINDOW(pcm); /* (phys - 0x4000) & 0x3ffff (§2 caveat) */
    DMA_DST = DAC_PORT_DST;
    DMA_WORDCOUNT = (sizeof pcm / 4) | DMA_START;
    DMA_CTRL = DMA_CTRL | DMA_KICK;

    /* bit13 reads back clear while the transfer is in flight (§7 item 3). */
    if (DMA_WORDCOUNT & DMA_START)
        FAILV(3, DMA_WORDCOUNT);

    while (!done)
        ; /* completion is paced: len/(4*rate) seconds of model time (§4) */

    TEST_PASS(sizeof pcm);
}
