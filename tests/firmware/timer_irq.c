/* timer_irq.c — timer1 + IRQ-delivery contract (interrupts-and-timers.md).
 *
 * Programs timer1 with the firmware's 20 ms reload (240000), enables only
 * line 10, and waits for three IRQs.  The handler verifies the level-pending
 * word, the second-level bit17 cause, and that the bit28 ACK de-asserts both.
 * Exercises: the architectural IRQ entry to vector 0x08000018, banked
 * SPSR/LR, the `ldmfd ..., pc}^` return, timer latch/ack semantics, and the
 * timer's periodic re-arm.
 */

#include "tt_test.h"

#define FAILV(n, got) TEST_FAIL(((u32)(n) << 28) | ((got)&0x0FFFFFFFu))

static volatile u32 irq_count;

void test_irq_handler(void)
{
    /* Level pending: line 10 must be up on entry (§2.2/§2.3). */
    u32 pending = INT_PENDING;
    if (!(pending & IRQ_LINE_TIMER))
        FAILV(1, pending);

    /* Second-level cause: bit17 = timer1 fired (§2.2). */
    u32 stat = TIMER_STAT;
    if (!(stat & STAT_TIMER_FIRED))
        FAILV(2, stat);

    /* ACK: write ctrl with bit28; latch and pending must drop (§4). */
    TIMER1_CTRL = TIMER1_CTRL | TIMER1_ACK;
    stat = TIMER_STAT;
    if (stat & STAT_TIMER_FIRED)
        FAILV(3, stat);
    pending = INT_PENDING;
    if (pending & IRQ_LINE_TIMER)
        FAILV(4, pending);

    irq_count++;
}

int main(void)
{
    irq_count = 0;
    INT_ENABLE = IRQ_LINE_TIMER;
    TIMER1_CTRL = TIMER1_ENABLE | TIMER1_RELOAD_20MS;

    /* The ctrl register reads back reload+enable (ACK bit never stored). */
    u32 ctrl = TIMER1_CTRL;
    if (ctrl != (TIMER1_ENABLE | TIMER1_RELOAD_20MS))
        FAILV(5, ctrl);

    while (irq_count < 3)
        ; /* IRQs arrive between the emulator's execution chunks */

    TEST_PASS(irq_count);
}
