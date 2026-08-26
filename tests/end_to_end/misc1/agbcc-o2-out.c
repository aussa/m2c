struct _struct_unk_0x8 {
    /* 0x0 */ s32 unk0;                             /* inferred */
    /* 0x4 */ char pad4[4];
};                                                  /* size = 0x8 */

? bar(s32, s32, s32);                               /* static */
s32 foo(s32, s32, s32, s32, s32);                   /* static */
extern s32 global;
extern ? global2;

s32 test(s32 arg0, s32 arg1) {
    s32 temp_r0;
    s32 temp_r6;
    s32 temp_r7;

    temp_r6 = ((struct _struct_unk_0x8 *) (global + 4))[arg0].unk0 + 1;
    temp_r7 = ((struct _struct_unk_0x8 *) (global + 8))[arg0].unk0;
    temp_r0 = foo(1, 2, temp_r6, arg1, arg0);
    if (temp_r0 != 0) {
        bar(temp_r7, temp_r0, temp_r6);
        *(arg0 + &global2) = 5;
        return temp_r0;
    }
    return 0;
}
