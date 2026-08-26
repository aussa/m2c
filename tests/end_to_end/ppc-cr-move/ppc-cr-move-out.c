void test(s32 arg_cr0_eq, s32 arg_cr0_gt, s32 arg_cr0_lt, s32 arg_cr0_so, u32 arg_sp10) {
    arg_sp10 = M2C_MFCR();
    M2C_MTCRF(8U, arg_sp10);
    M2C_MTCRF(0xFFU, M2C_MFCR());
    M2C_MCRF(7U, 0U);
    M2C_MCRF(4U, 0U);
}
