void test(s32 arg0) {
    if (arg0 == 0) {
        *(s32 *) 0 = 0;
    } else {
        *(s32 *) 0 = 1;
    }
    *(s32 *) 0 = 2;
}
