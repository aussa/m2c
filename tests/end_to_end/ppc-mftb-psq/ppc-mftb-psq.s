.global test
test:
psq_lx f2, r4, r5, 0, qr0
psq_stx f2, r6, r7, 0, qr0
mftb r3, 268
blr
