/* smoke.c — harness self-test: signal PASS with a known detail value.
 * Proves the toolchain, the load/entry path, and the mailbox convention. */

#include "tt_test.h"

int main(void)
{
    TEST_PASS(0x12345678);
}
