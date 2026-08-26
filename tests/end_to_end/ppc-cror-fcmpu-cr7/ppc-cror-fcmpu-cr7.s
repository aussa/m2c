.global test
test:
fcmpu cr7, f1, f2
fsubs f3, f3, f4
cror cr7un, cr7eq, cr7gt
bns cr7, .L1
stfs f1, 8(r3)
.L1:
fcmpu cr7, f5, f6
cror cr7un, cr7eq, cr7lt
mfcr r5
rlwinm r5, r5, 0, 31, 31
stw r5, 4(r3)
fcmpu cr7, f1, f2
cror cr7un, cr7eq, cr7gt
bns cr7, .L2
lfs f3, 4(r3)
cmpwi cr6, r4, 0
bne cr6, .L2
cror cr7un, cr7eq, cr7gt
bso cr7, .L3
b .L2
.L3:
stfs f1, 8(r3)
.L2:
blr
