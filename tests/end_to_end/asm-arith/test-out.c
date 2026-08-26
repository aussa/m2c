extern ? sym;

void test(void) {
    *(s32 *) 0 = 1;
    *(s32 *) 0x10 = 0;
    *(s32 *) 0x20 = 0;
    *(s32 *) 0x40 = 0;
    *(s32 *) 0 = 0x50;
    sym.unk8 = 0;
}
