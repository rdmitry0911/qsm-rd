/*
 * A self-contained BIOS boot sector for the real-QEMU Display1 test.
 *
 * It switches the standard VGA adapter to mode 13h (320x200x8) and keeps
 * repainting the framebuffer.  There is no guest OS, disk image, network, or
 * firmware dependency beyond SeaBIOS, so this is a useful CPU/TCG fixture for
 * CI hosts that cannot use KVM.
 */

.code16
.section .text
.global _start

_start:
    cli
    xorw %ax, %ax
    movw %ax, %ds

    movw $0x0013, %ax              /* VGA mode 13h: 320x200, indexed color. */
    int $0x10

    movw $0xa000, %ax
    movw %ax, %es
    movb $0x10, %al

.Lpaint:
    xorw %di, %di
    movw $0xfa00, %cx              /* 320 * 200 bytes. */
    rep stosb
    incb %al

    /* Keep producing visible VGA dirties after the listener attaches. */
    movw $0xffff, %cx
.Ldelay:
    loop .Ldelay
    jmp .Lpaint

.org 510
.word 0xaa55
