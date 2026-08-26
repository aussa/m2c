void *foo();                                        /* extern */

void test(void) {
    void *sp10C;
    void *temp_t1;

    temp_t1 = foo();
    if (*(u8 *) 0 == 0) {
        sp10C = temp_t1;
    }
    temp_t1->unk3 = 0;
    temp_t1->unk4 = 0;
}
