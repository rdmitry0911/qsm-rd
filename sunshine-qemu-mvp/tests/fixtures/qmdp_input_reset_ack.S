/*
 * BIOS-only acknowledgement fixture for Sunshine input::reset().
 *
 * The E2E driver must send a physical A key-down through Moonlight, then
 * disconnect the client without sending its key-up.  Sunshine's disconnect
 * reset must deliver the PS/2 set-1 A break byte.  Once both bytes arrive,
 * this fixture writes INPUT_RESET_RELEASE_OK to QEMU debugcon (port 0xe9).
 *
 * The fixture cannot identify the sender of the break byte by itself; the
 * driver contract (no client key-up before disconnect) is what makes the
 * marker evidence of the release-all path.
 */

.code16
.section .text
.global _start

_start:
    cli
    xorw %ax, %ax
    movw %ax, %ds

    movw $0x0013, %ax              /* VGA mode 13h: 320x200 indexed color. */
    int $0x10

    movw $0xa000, %ax
    movw %ax, %es
    movb $0x04, %al                /* Initial red framebuffer. */
    xorw %di, %di
    movw $0xfa00, %cx              /* 320 * 200 bytes. */
    rep stosb

    /* IRQ1 stays disabled so raw port reads observe both make and break. */
.Lwait_make:
    inb $0x64, %al
    testb $0x01, %al
    jz .Lwait_make
    inb $0x60, %al
    cmpb $0x1e, %al                /* QEMU qnum 0x1e: physical A key. */
    jne .Lwait_make

.Lwait_break:
    inb $0x64, %al
    testb $0x01, %al
    jz .Lwait_break
    inb $0x60, %al
    cmpb $0x9e, %al                /* Set-1 break for A. */
    jne .Lwait_break

    movb $'I', %al
    outb %al, $0xe9
    movb $'N', %al
    outb %al, $0xe9
    movb $'P', %al
    outb %al, $0xe9
    movb $'U', %al
    outb %al, $0xe9
    movb $'T', %al
    outb %al, $0xe9
    movb $'_', %al
    outb %al, $0xe9
    movb $'R', %al
    outb %al, $0xe9
    movb $'E', %al
    outb %al, $0xe9
    movb $'S', %al
    outb %al, $0xe9
    movb $'E', %al
    outb %al, $0xe9
    movb $'T', %al
    outb %al, $0xe9
    movb $'_', %al
    outb %al, $0xe9
    movb $'R', %al
    outb %al, $0xe9
    movb $'E', %al
    outb %al, $0xe9
    movb $'L', %al
    outb %al, $0xe9
    movb $'E', %al
    outb %al, $0xe9
    movb $'A', %al
    outb %al, $0xe9
    movb $'S', %al
    outb %al, $0xe9
    movb $'E', %al
    outb %al, $0xe9
    movb $'_', %al
    outb %al, $0xe9
    movb $'O', %al
    outb %al, $0xe9
    movb $'K', %al
    outb %al, $0xe9
    movb $'\n', %al
    outb %al, $0xe9

    movw $0xa000, %ax
    movw %ax, %es
    movb $0x02, %al                /* Green after guest receives reset release. */
    xorw %di, %di
    movw $0xfa00, %cx
    rep stosb

.Lhalt:
    hlt
    jmp .Lhalt

.org 510
.word 0xaa55
