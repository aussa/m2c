u32 test(s32 arg1, s32 arg2, s32 arg3, s32 arg4) {
    *(s64 *) (arg3 + arg4) = *(s64 *) (arg1 + arg2);
    return M2C_MFTB(0x10CU);
}
