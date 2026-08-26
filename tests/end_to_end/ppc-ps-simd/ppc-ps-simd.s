.global test
test:
lfd f0, 0(r3)
lfd f1, 8(r3)
lfd f2, 0x10(r3)
lfd f3, 0x18(r3)
ps_rsqrte f4, f0
ps_muls0 f5, f1, f0
ps_add f6, f4, f5
ps_sub f7, f6, f2
ps_mul f8, f7, f3
ps_madd f9, f8, f1, f0
ps_madds0 f10, f8, f2, f0
ps_madds1 f11, f8, f3, f0
ps_sum0 f12, f8, f1, f0
ps_merge00 f13, f8, f1
ps_neg f0, f8
ps_mr f1, f8
ps_div f2, f8, f3
stfd f2, 0(r3)
stfd f12, 8(r3)
blr
