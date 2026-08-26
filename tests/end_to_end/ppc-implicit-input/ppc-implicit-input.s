.global test
test:
    bne cr1, .Ltaken
    add r3, r11, r12
    blr
.Ltaken:
    li r3, 0
    blr
