.global test
test:
    crclr cr1eq
    bne cr1, .Lset
    li r3, 0
    blr
.Lset:
    crset cr1eq
    bne cr1, .Ldone
    li r3, 1
    blr
.Ldone:
    li r3, 2
    blr
