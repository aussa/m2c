.global test
test:
    li r11, 5
    bl helper
    mr r3, r11
    blr

.global helper
helper:
    blr
