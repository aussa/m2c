f64 test(void *arg0) {
    f64 temp_f0;
    f64 temp_f12;
    f64 temp_f1;
    f64 temp_f1_2;
    f64 temp_f3;
    f64 temp_f8;

    temp_f0 = arg0->unk0;
    temp_f1_2 = arg0->unk8;
    temp_f3 = arg0->unk18;
    temp_f8 = M2C_PS_MUL(M2C_PS_SUB(M2C_PS_ADD(M2C_PS_RSQRTE(temp_f0), M2C_PS_MULS0(temp_f1_2, temp_f0)), arg0->unk10), temp_f3);
    temp_f12 = M2C_PS_SUM0(temp_f8, temp_f1_2, temp_f0);
    temp_f1 = M2C_PS_MR(temp_f8);
    arg0->unk0 = M2C_PS_DIV(temp_f8, temp_f3);
    arg0->unk8 = temp_f12;
    return temp_f1;
}
