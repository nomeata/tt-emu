/* gpio.c — GPIO register contract (gpio-buttons-led.md §1/§2/§6).
 *
 * Checks: the idle GPIO_IN composite word, output-latch read-back, the
 * amp-enable latch (bit16) mirrored into GPIO_IN, direction-register
 * read-back over its boot seed, GPIO_IN read-only-ness, and the int-enable/
 * polarity banks reading 0 until written.
 *
 * Care: GPIO15 (power-hold) must never make a 1->0 transition here — that is
 * the pen's clean power-off and stops the machine (tested separately by
 * poweroff.c).  This test leaves bit15 at 0 throughout.
 */

#include "tt_test.h"

#define FAILV(n, got) TEST_FAIL(((u32)(n) << 28) | ((got)&0x0FFFFFFFu))

int main(void)
{
    /* Idle retail input word (§2). */
    u32 in = GPIO_IN0;
    if (in != GPIO_IN_IDLE)
        FAILV(1, in);

    /* Int-enable / polarity read 0 until written (§6). */
    if (GPIO_INT_EN0 != 0)
        FAILV(2, GPIO_INT_EN0);
    if (GPIO_INT_POL0 != 0)
        FAILV(3, GPIO_INT_POL0);

    /* Output latch reads back what was written (§1.1); avoid bit15. */
    GPIO_OUT0 = 0x00000004; /* OID clock pin, harmless */
    if (GPIO_OUT0 != 0x00000004)
        FAILV(4, GPIO_OUT0);

    /* Amp enable (bit16) mirrors into GPIO_IN (§1.1). */
    GPIO_OUT0 = GPIO_PIN_AMP;
    if (GPIO_OUT0 != GPIO_PIN_AMP)
        FAILV(5, GPIO_OUT0);
    in = GPIO_IN0;
    if (in != (GPIO_IN_IDLE | GPIO_PIN_AMP))
        FAILV(6, in);
    GPIO_OUT0 = 0;
    in = GPIO_IN0;
    if (in != GPIO_IN_IDLE)
        FAILV(7, in);

    /* Direction register: boot seed 0x3100, RAM-backed read-back (§1). */
    u32 dir = GPIO_DIR0;
    if (dir != 0x3100)
        FAILV(8, dir);
    GPIO_DIR0 = 0x3104;
    if (GPIO_DIR0 != 0x3104)
        FAILV(9, GPIO_DIR0);

    /* GPIO_IN is read-only: a write must not change what it reads (§1). */
    GPIO_IN0 = 0xFFFFFFFF;
    in = GPIO_IN0;
    if (in != GPIO_IN_IDLE)
        FAILV(10, in);

    TEST_PASS(in);
}
