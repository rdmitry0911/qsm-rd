/*
 * Real-QEMU AudioOutListener fixture.
 *
 * This 512-byte BIOS disk starts a 1 kHz PC-speaker tone, emits a debugcon
 * marker, and continuously repaints VGA mode 13h.  It has no guest OS or
 * host sound-server dependency: QEMU's `pcspk-audiodev=dbus0` routes the
 * generated samples straight into the Display1 D-Bus audio backend.
 */

.code16
.section .text
.global _start

_start:
    cli
    xorw %ax, %ax
    movw %ax, %ds

    movw $0x0013, %ax              /* VGA mode 13h: 320x200x8. */
    int $0x10

    /* PIT channel 2, square wave, 1193 divisor ~= 1 kHz. */
    movb $0xb6, %al
    outb %al, $0x43
    movw $1193, %ax
    outb %al, $0x42
    movb %ah, %al
    outb %al, $0x42
    inb $0x61, %al
    orb $0x03, %al                 /* Enable counter 2 and speaker data. */
    outb %al, $0x61

    /* Stable guest-side evidence before the first QEMU audio callback. */
    movb $'A', %al
    outb %al, $0xe9
    movb $'U', %al
    outb %al, $0xe9
    movb $'D', %al
    outb %al, $0xe9
    movb $'I', %al
    outb %al, $0xe9
    movb $'O', %al
    outb %al, $0xe9
    movb $'_', %al
    outb %al, $0xe9
    movb $'T', %al
    outb %al, $0xe9
    movb $'O', %al
    outb %al, $0xe9
    movb $'N', %al
    outb %al, $0xe9
    movb $'E', %al
    outb %al, $0xe9
    movb $'_', %al
    outb %al, $0xe9
    movb $'O', %al
    outb %al, $0xe9
    movb $'N', %al
    outb %al, $0xe9
    movb $10, %al
    outb %al, $0xe9

    movw $0xa000, %ax
    movw %ax, %es
    movb $0x30, %al

.Lpaint:
    xorw %di, %di
    movw $0xfa00, %cx              /* 320 * 200 bytes. */
    rep stosb
    incb %al
    movw $0xffff, %cx
.Ldelay:
    loop .Ldelay
    jmp .Lpaint

.org 510
.word 0xaa55
