BITS 64
;TEST_FILE_META_BEGIN
;TEST_TYPE=TEST_F
;TEST_IGNOREFLAGS=FLAG_CF|FLAG_OF|FLAG_PF|FLAG_SF|FLAG_AF
;TEST_FILE_META_END
    ; BSR16rr
    mov ax, 0x0800
    ;TEST_BEGIN_RECORDING
    bsr bx, ax
    ;TEST_END_RECORDING
