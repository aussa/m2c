.global test
test:
    mfmsr r3
    mtmsr r3
    mfsrr0 r4
    mtsrr0 r4
    mfdsisr r5
    mfsr r6, 0
    mtdbatu 1, r7
    mfdbatl r8, 2
    blr
