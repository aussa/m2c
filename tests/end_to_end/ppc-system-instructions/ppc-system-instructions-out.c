void test(s32 arg4) {
    M2C_MTMSR(M2C_MFMSR());
    M2C_MTSPR(0x1AU, M2C_MFSPR(0x1AU));
    M2C_MTSPR(0x21AU, arg4);
}
