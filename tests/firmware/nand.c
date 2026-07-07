/* nand.c — NAND controller contract (nand-and-nfc-controller.md §5/§8).
 *
 * Drives the NFC command-list sequencer exactly as the firmware does:
 *   1. READ-ID (cmd 0x90): DATA_RD0 = 0x9551D3EC, DATA_RD1 = 0 (§8.5);
 *   2. STATUS (cmd 0x70): DATA_RD0 = 0xC0 (§8.4);
 *   3. a 512-byte data read of block 2 / AU 0 (row 0x0200, col 0) through the
 *      ECC engine into the L2 SRAM window, checked byte-for-byte against the
 *      pattern the pytest side pre-programmed into the NandImage (§8.1);
 *   4. an 8-byte spare-tag read of the same row (small-payload ECC config),
 *      checked against the pytest-set tag (§4.1/§9).
 *
 * Pattern contract with tests/test_firmware_blobs.py:
 *   data byte i  = (i * 7 + 3) & 0xFF   at flat offset 2 * 0x20000
 *   tag          = "TTEMUTAG"           at row 0x0200
 */

#include "tt_test.h"

#define FAILV(n, got) TEST_FAIL(((u32)(n) << 28) | ((got)&0x0FFFFFFFu))

#define TEST_ROW 0x0200u /* block 2, AU 0 */

static void nfc_go(void)
{
    NFC_CTRL_STATUS = NFC_GO;
    while (!(NFC_CTRL_STATUS & NFC_READY))
        ;
}

static const u8 tag_expect[8] = {'T', 'T', 'E', 'M', 'U', 'T', 'A', 'G'};

int main(void)
{
    /* --- 1. READ-ID (§8.5) ----------------------------------------------- */
    NFC_CMDLIST[0] = NFC_OP_CMD(NAND_CMD_READ_ID) | NFC_OP_LAST;
    nfc_go();
    if (NFC_DATA_RD0 != NAND_READ_ID_VALUE)
        FAILV(1, NFC_DATA_RD0);
    if (NFC_DATA_RD1 != 0)
        FAILV(2, NFC_DATA_RD1);

    /* --- 2. STATUS (§8.4) -------------------------------------------------- */
    NFC_CMDLIST[0] = NFC_OP_CMD(NAND_CMD_STATUS) | NFC_OP_LAST;
    nfc_go();
    if (NFC_DATA_RD0 != NAND_STATUS_VALUE)
        FAILV(3, NFC_DATA_RD0);

    /* --- 3. 512-byte data read of row 0x0200, col 0 (§8.1) ----------------- */
    ECC_CTRL = ECC_CONFIG_PAYLOAD(512); /* payload length in bits[18:7] (§6) */
    NFC_CMDLIST[0] = NFC_OP_CMD(NAND_CMD_READ_SETUP);
    NFC_CMDLIST[1] = NFC_OP_ADDR(0); /* column, 2 cycles LE */
    NFC_CMDLIST[2] = NFC_OP_ADDR(0);
    NFC_CMDLIST[3] = NFC_OP_ADDR(TEST_ROW & 0xFF); /* row, LSB first */
    NFC_CMDLIST[4] = NFC_OP_ADDR((TEST_ROW >> 8) & 0xFF);
    NFC_CMDLIST[5] = NFC_OP_ADDR((TEST_ROW >> 16) & 0xFF);
    NFC_CMDLIST[6] = NFC_OP_CMD(NAND_CMD_READ_CONFIRM);
    NFC_CMDLIST[7] = NFC_OP_DATA_RD(512) | NFC_OP_LAST;
    nfc_go();

    /* Wait for the L2 buffer-4 deposit (fill level != 0, §7). */
    while (((L2_BUF_STATUS >> 16) & 0xF) == 0)
        ;
    for (u32 i = 0; i < 512; i++) {
        u8 want = (u8)(i * 7 + 3);
        u8 got = NAND_SRAM_WINDOW[i];
        if (got != want)
            FAILV(4, (i << 16) | ((u32)got << 8) | want);
    }

    /* --- 4. 8-byte spare-tag read of the same row (§4.1) ------------------- */
    ECC_CTRL = ECC_CONFIG_PAYLOAD(8); /* < 512 -> the row's tag */
    NFC_CMDLIST[0] = NFC_OP_CMD(NAND_CMD_READ_SETUP);
    NFC_CMDLIST[1] = NFC_OP_ADDR(0);
    NFC_CMDLIST[2] = NFC_OP_ADDR(0x08); /* col past the data records */
    NFC_CMDLIST[3] = NFC_OP_ADDR(TEST_ROW & 0xFF);
    NFC_CMDLIST[4] = NFC_OP_ADDR((TEST_ROW >> 8) & 0xFF);
    NFC_CMDLIST[5] = NFC_OP_ADDR((TEST_ROW >> 16) & 0xFF);
    NFC_CMDLIST[6] = NFC_OP_CMD(NAND_CMD_READ_CONFIRM);
    NFC_CMDLIST[7] = NFC_OP_DATA_RD(8) | NFC_OP_LAST;
    nfc_go();
    while (((L2_BUF_STATUS >> 16) & 0xF) == 0)
        ;
    for (u32 i = 0; i < 8; i++) {
        u8 got = NAND_SRAM_WINDOW[i];
        if (got != tag_expect[i])
            FAILV(5, (i << 16) | ((u32)got << 8) | tag_expect[i]);
    }

    TEST_PASS(NAND_READ_ID_VALUE);
}
