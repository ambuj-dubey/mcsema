BITS 64
;TEST_FILE_META_BEGIN
;TEST_TYPE=TEST_F
;TEST_IGNOREFLAGS=
;TEST_FILE_META_END
    ; MOVZX32rr8
    mov edx, 0x12
    mov dh, 0x3
    ;TEST_BEGIN_RECORDING
    movzx edx, dh
    ;TEST_END_RECORDING
