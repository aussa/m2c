void test(void *arg0, s32 arg2, s32 arg3, f32 farg0, f32 farg1) {
    arg0->unk0 = (s32) (((arg2 << (arg3 & 0x1F)) & 0xFFFFFFFF) | ((arg2 >> (0x20 - (arg3 & 0x1F))) & 0xFFFFFFFF));
    if (farg0 != farg1) {
        arg0->unk8 = farg0;
    }
}
