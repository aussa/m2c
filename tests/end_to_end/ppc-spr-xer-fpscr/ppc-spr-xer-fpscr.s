.global test
test:
mfxer r5
stw r5, 0(r3)
mffs f2
mtfsf 255, f2
mtxer r5
dcbt r0, r5
dcbf r0, r6
icbi r0, r3
blr
