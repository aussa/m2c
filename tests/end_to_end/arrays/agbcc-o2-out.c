? memcpy(? *, ? *, s32);                            /* extern */
extern ? globalArray;

s32 test(s32 arg0, s32 arg1, s32 arg2) {
    ? sp0;

    memcpy(&sp0, "hello", 6);
    return (*(s32 *) ((arg0 * 4) + arg1) * *(&sp0 + arg0)) + *((arg0 * 2) + &globalArray) + ((s32 *) (arg2 + 4))[arg0];
}
