/* dac_dma.c — audio DAC DMA submit/completion contract (audio-dac-dma.md).
 *
 * Performs one DAC playback submit exactly as the firmware does (§2/§7):
 * poll bit13 idle, program source/destination, START with the word count,
 * kick bit16 — then waits for the paced line-0 completion IRQ and ACKs it by
 * clearing the kick bit (§3).  The emulator recovers the CPU pointer of the
 * chunk from the firmware's PCM ring singleton (body 0x08008D30), so this
 * test also seeds a minimal ring pointing at its PCM buffer; the pytest side
 * additionally verifies the captured bytes equal the pattern below.
 *
 * Pattern contract with tests/test_firmware_blobs.py:
 *   pcm[i] = (i16)(i * 3 - 128), 256 samples = 512 bytes.
 */

#include "tt_test.h"

#define FAILV(n, got) TEST_FAIL(((u32)(n) << 28) | ((got)&0x0FFFFFFFu))

/* The firmware's PCM ring singleton body (audio-dac-dma.md §5). */
#define RING_BODY 0x08008D30u
#define RING_READ REG32(RING_BODY + 0x38)
#define RING_SIZE REG32(RING_BODY + 0x40)
#define RING_BASE REG32(RING_BODY + 0x44)

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

    /* Minimal PCM ring: one chunk at the buffer, read pointer at 0 (§5). */
    RING_BASE = (u32)pcm;
    RING_SIZE = sizeof pcm;
    RING_READ = 0;

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
