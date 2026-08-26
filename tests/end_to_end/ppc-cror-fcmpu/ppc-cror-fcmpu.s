.global test
test:
lfs f1, 0(r3)
lfs f0, 4(r3)
fcmpu cr0, f1, f0
cror un, eq, gt
bns .L1
stfs f1, 8(r3)
.L1:
fcmpu cr0, f1, f0
cror un, eq, lt
bns .L2
stfs f0, 0xc(r3)
.L2:
fcmpu cr0, f1, f0
cror un, eq, gt
bso .L3
stfs f1, 0x10(r3)
.L3:
blr
