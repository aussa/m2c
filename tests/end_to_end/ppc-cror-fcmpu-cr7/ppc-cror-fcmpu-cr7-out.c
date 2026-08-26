void test(void *arg0, s32 arg1, f32 farg0, f32 farg1, f32 farg2, f32 farg3, f32 farg4, f32 farg5) {
    if (farg0 >= farg1) {
        arg0->unk8 = farg0;
    }
    arg0->unk4 = (s32) (farg4 <= farg5);
    if ((farg0 >= farg1) && (arg1 == 0)) {
        if (((farg0 == farg1) || (farg0 > farg1)) == 0) {
            return;
        }
        arg0->unk8 = farg0;
    }
}
