.global test
test:
    bl helper
    mr r3, r11
    blr

.global helper
helper:
    blr
