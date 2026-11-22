/* SPDX-License-Identifier: AGPL-3.0-or-later
 * BIOS guest for the standalone browser E2E test. A turns the left half
 * green; a PS/2 mouse packet turns the right half red. The test checks the
 * rendered browser picture, so delivery must reach the actual guest.
 */
.code16
.section .text
.global _start
_start:
    cli
    xorw %ax, %ax
    movw %ax, %ss
    movw $0x7c00, %sp
    movw %ax, %ds
    movw $0x0013, %ax
    int $0x10
    cli
    movw $0xa000, %ax
    movw %ax, %es
    xorw %di, %di
    movw $64000, %cx
    movb $1, %al
    rep stosb

    /* Poll the i8042 ourselves; disable IRQ1/IRQ12, keep translation on. */
    call .Lwait_write
    movb $0x20, %al
    outb %al, $0x64
    call .Lread
    andb $0xfc, %al
    orb $0x40, %al
    movb %al, %bl
    call .Lwait_write
    movb $0x60, %al
    outb %al, $0x64
    call .Lwait_write
    movb %bl, %al
    outb %al, $0x60
    call .Lwait_write
    movb $0xa8, %al
    outb %al, $0x64
    call .Lwait_write
    movb $0xd4, %al
    outb %al, $0x64
    call .Lwait_write
    movb $0xf4, %al
    outb %al, $0x60

.Lpoll:
    inb $0x64, %al
    testb $1, %al
    jz .Lpoll
    movb %al, %dl
    inb $0x60, %al
    testb $0x20, %dl
    jnz .Lmouse
    cmpb $0x1e, %al
    jne .Lpoll
    xorw %di, %di
    movb $10, %al
    jmp .Lpaint
.Lmouse:
    cmpb $0xfa, %al
    je .Lpoll
    movw $160, %di
    movb $12, %al
.Lpaint:
    movw $200, %bx
.Lline:
    movw $160, %cx
    rep stosb
    addw $160, %di
    decw %bx
    jnz .Lline
    jmp .Lpoll
.Lwait_write:
    inb $0x64, %al
    testb $2, %al
    jnz .Lwait_write
    ret
.Lread:
    inb $0x64, %al
    testb $1, %al
    jz .Lread
    inb $0x60, %al
    ret
.org 510
.word 0xaa55
