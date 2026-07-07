/* poweroff.c — the GPIO15 power-hold contract (battery-and-power.md §5,
 * gpio-buttons-led.md §4): a 1->0 transition of output bit 15 is the pen's
 * clean power-off and must terminate the run.
 *
 * This blob never signals PASS: it raises the power-hold latch, drops it,
 * and parks.  The pytest side asserts the machine stopped with the
 * "pen powered off" reason (and not by exhausting its budget).
 */

#include "tt_test.h"

int main(void)
{
    TT_DETAIL = 0x0FF0FF; /* progress marker (no status word) */

    GPIO_OUT0 = GPIO_PIN_POWER_HOLD; /* 0 -> 1: latched on */
    if (GPIO_OUT0 != GPIO_PIN_POWER_HOLD)
        TEST_FAIL(GPIO_OUT0);

    GPIO_OUT0 = 0; /* 1 -> 0: the pen releases its own supply */

    for (;;)
        ; /* must never spin long: the machine stops on the write above */
}
