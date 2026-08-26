typedef struct {
    /* 0x0000 */ s8 unk0;                           /* inferred */
    /* 0x0001 */ char pad1[0xF];                    /* maybe part of unk0[0x10]? */
    /* 0x0010 */ s32 whatever[0x1000];
} SomeStruct;                                       /* size = 0x4010 */

? foo(s16);                                         /* extern */

void test(void) {
    SomeStruct *var_s0;
    s32 var_s1;

    var_s1 = 0;
    var_s0 = &glob;
    do {
        if (*(s16 *) 0 == 0) {
            foo(var_s0->unk2004);
        }
        var_s1 += 1;
        var_s0 += 0xC;
    } while (var_s1 < 5);
}
