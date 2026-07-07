/* blob.ld — link a tt-emu test blob as a flat image at 0x08000000.
 *
 * The vector table (.vectors, from start.S) must come first so the emulator's
 * fixed IRQ vector 0x08000018 lands on the blob's IRQ slot.  The raw .bin
 * (objcopy -O binary) is loaded verbatim at 0x08000000 by the harness; .bss
 * is not in the .bin but the emulator's RAM is zero-initialised, which is the
 * same thing.
 */

ENTRY(_vectors)

MEMORY
{
    /* Keep blobs below 0x08006000: the NAND L2 SRAM buffers (buffer 4 = the
     * 512-byte window at 0x08006800) and the firmware globals the audio model
     * observes (PCM ring body 0x08008D30) live just above. */
    ram (rwx) : ORIGIN = 0x08000000, LENGTH = 0x6000
}

__stack_top = 0x08020000;   /* grows down; well below the mailbox and the
                               emulator's IRQ stack (0x083F0000) */

SECTIONS
{
    .text : {
        KEEP(*(.vectors))
        *(.text*)
        *(.rodata*)
    } > ram

    .data : {
        *(.data*)
    } > ram

    .bss (NOLOAD) : {
        *(.bss*)
        *(COMMON)
    } > ram
}
