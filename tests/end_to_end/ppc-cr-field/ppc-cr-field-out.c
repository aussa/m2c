void test(void *arg0, s32 arg2, s32 arg3, s32 arg4, s32 arg5, s32 arg6, s32 arg7) {
    if (arg2 != 0) {
        arg0->unk0 = arg2;
    }
    if (arg3 == 5) {
        arg0->unk4 = arg3;
    }
    if (arg4 >= arg5) {
        arg0->unk8 = arg4;
    }
    if (arg6 > 0) {
        arg0->unkC = arg6;
    }
    if (arg7 <= 0) {
        arg0->unk10 = arg7;
    }
}
