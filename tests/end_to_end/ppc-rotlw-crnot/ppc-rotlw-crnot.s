.global test
test:
rotlw r5, r5, r6
stw r5, 0(r3)
fcmpu cr7, f1, f2
crnot cr7un, cr7eq
bns cr7, .L1
stfs f1, 8(r3)
.L1:
blr
