# SPDX-License-Identifier: AGPL-3.0-or-later
# Boot sector: continuously rewrites the VGA text screen (changing characters/colours).
    .code16
    .globl _start
_start:
    cli
    xor %ax, %ax
    mov %ax, %ds
    mov $0xb800, %ax
    mov %ax, %es
    xor %bx, %bx            # frame counter
frame:
    xor %di, %di
    mov $2000, %cx
    mov %bx, %ax
cell:
    mov %al, %dl
    and $0x3f, %dl
    add $0x30, %dl          # printable char
    mov %dl, %es:(%di)
    mov %ah, %dl
    and $0x7f, %dl
    or  $0x01, %dl
    mov %dl, %es:1(%di)
    add $2, %di
    add $7, %ax
    loop cell
    inc %bx
    mov $0x0800, %si        # delay
d1: mov $0x0200, %dx
d2: dec %dx
    jnz d2
    dec %si
    jnz d1
    jmp frame
    .org 510
    .byte 0x55, 0xaa
