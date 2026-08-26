.global test
test:
cmpwi cr4, r5, 0
beq cr4, .L1
stw r5, 0(r3)
.L1:
cmpwi cr7, r6, 5
bne cr7, .L2
stw r6, 4(r3)
.L2:
cmpw cr3, r7, r8
blt cr3, .L3
stw r7, 8(r3)
.L3:
cmpwi cr4, r9, 0
ble cr4, .L4
stw r9, 0xc(r3)
.L4:
cmpwi cr7, r10, 0
bgt cr7, .L5
stw r10, 0x10(r3)
.L5:
blr
