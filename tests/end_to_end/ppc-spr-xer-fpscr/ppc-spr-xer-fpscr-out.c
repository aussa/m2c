void test(u32 *arg0) {
    u32 temp_r5;

    temp_r5 = M2C_MFXER();
    *arg0 = temp_r5;
    M2C_MTFSF(0xFFU, M2C_MFFS());
    M2C_MTXER(temp_r5);
}
