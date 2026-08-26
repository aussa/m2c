.global test
test:
mfcr r12
stw r12, 0x10(r1)
lwz r12, 0x10(r1)
mtcrf 8, r12
mfcr r0
mtcrf 0xFF, r0
mcrf cr7, cr0
mcrf cr4, cr0
blr
