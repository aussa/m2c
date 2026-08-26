void test(void *arg0) {
    f32 temp_f0;
    f32 temp_f1;

    temp_f1 = arg0->unk0;
    temp_f0 = arg0->unk4;
    if (temp_f1 >= temp_f0) {
        arg0->unk8 = temp_f1;
    }
    if (temp_f1 <= temp_f0) {
        arg0->unkC = temp_f0;
    }
    if (!(temp_f1 >= temp_f0)) {
        arg0->unk10 = temp_f1;
    }
}
