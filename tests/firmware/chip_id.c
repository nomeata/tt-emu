/* chip_id.c — the boot-constants contract (docs/index.md checklist).
 *
 * Reads the identification and status constants the real firmware's boot
 * self-tests gate on, exactly as MMIO loads, and checks each documented
 * value.  On failure the detail is (check# << 28) | (observed & 0x0FFFFFFF).
 */

#include "tt_test.h"

#define FAILV(n, got) TEST_FAIL(((u32)(n) << 28) | ((got)&0x0FFFFFFFu))

int main(void)
{
    /* Chip-ID: constant "1090", writes ignored (system-control-and-clock §2). */
    u32 id = CHIP_ID;
    if (id != CHIP_ID_VALUE)
        FAILV(1, id);
    CHIP_ID = 0xDEADBEEF;
    if (CHIP_ID != CHIP_ID_VALUE)
        FAILV(2, CHIP_ID);

    /* Clock/PLL latch bits 12/13/14/21 self-clear; other bits persist (§3). */
    CLK_DIV = (1u << 12) | (1u << 13) | (1u << 14) | (1u << 21) | 0x4D;
    u32 clk = CLK_DIV;
    if (clk & ((1u << 12) | (1u << 13) | (1u << 14) | (1u << 21)))
        FAILV(3, clk);
    if ((clk & 0xFF) != 0x4D)
        FAILV(4, clk);

    /* Analog power-down: bit8 rate-apply strobe reads 0 (§4). */
    ANALOG_PD = 1u << 8;
    if (ANALOG_PD & (1u << 8))
        FAILV(5, ANALOG_PD);

    /* Battery ADC serves the healthy constant (battery-and-power §7). */
    BATTERY_ENABLE = 0x200;
    u32 adc = BATTERY_ADC;
    if (adc != BATTERY_ADC_HEALTHY)
        FAILV(6, adc);

    /* Audio clock bit19 reads 1 (boot checklist; audio-dac-dma §5). */
    if (!(AUDIO_CLOCK & (1u << 19)))
        FAILV(7, AUDIO_CLOCK);

    /* NFC sequencer ready (bit31); ECC complete+pass (nand doc §5/§6). */
    if (!(NFC_CTRL_STATUS & NFC_READY))
        FAILV(8, NFC_CTRL_STATUS);
    u32 ecc = ECC_CTRL;
    if (ecc != ECC_STATUS_PASS)
        FAILV(9, ecc);

    /* USB window: dead-bus reads 0, never 0xFFFFFFFF (usb-musb-device §1). */
    if (USB_BASE != 0)
        FAILV(10, USB_BASE);

    /* Interrupt pending is 0 when idle (interrupts-and-timers §2.3). */
    if (INT_PENDING != 0)
        FAILV(11, INT_PENDING);

    TEST_PASS(id);
}
