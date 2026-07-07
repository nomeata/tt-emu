/* start.S — minimal bare-metal startup for tt-emu test blobs.
 *
 * The blob is linked at 0x08000000 (blob.ld) so this vector table occupies
 * the exact addresses the emulator uses: the machine's IRQ delivery jumps to
 * the fixed vector 0x08000018 (machine.py IRQ_VECTOR), which is the `b irq`
 * slot below.  No libc, no .data/.bss init (the emulator zero-fills RAM and
 * the raw .bin carries .data in place).
 */

    .arm
    .section .vectors, "ax"
    .global _vectors
_vectors:
    b   _start              /* 0x00 reset */
    b   .                   /* 0x04 undefined instruction */
    b   .                   /* 0x08 svc */
    b   .                   /* 0x0c prefetch abort */
    b   .                   /* 0x10 data abort */
    b   .                   /* 0x14 (reserved) */
    b   _irq_trampoline     /* 0x18 IRQ <- the emulator's IRQ_VECTOR */
    b   .                   /* 0x1c FIQ */

    .text
    .global _start
_start:
    ldr sp, =__stack_top
    bl  main
    /* main() returned without TEST_PASS/TEST_FAIL: park; the harness
     * reports "no result" when the instruction budget runs out. */
1:  b   1b

/* IRQ entry: save clobbers, call the C handler, return to the interrupted
 * instruction (the machine set LR = interrupted PC + 4). */
_irq_trampoline:
    sub lr, lr, #4
    stmfd sp!, {r0-r3, r12, lr}
    bl  test_irq_handler
    ldmfd sp!, {r0-r3, r12, pc}^

/* Weak default IRQ handler: ignore (tests that expect IRQs override it). */
    .weak test_irq_handler
test_irq_handler:
    bx  lr
