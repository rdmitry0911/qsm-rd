// SPDX-License-Identifier: GPL-3.0-or-later
//
// macOS clipboard backend for the QSM desktop agent (qsf_guest_agent.c).
//
// On Linux the agent keeps a per-user state file that an external Wayland
// bridge (wl-clipboard, or KDE Klipper over D-Bus) mirrors to and from the
// real selection, and it waits for that bridge to acknowledge each write.
// macOS has one synchronous, system-wide clipboard — NSPasteboard — so the
// agent drives it directly: no state file, no bridge, no filesystem watch.
// The functions here are the only macOS-specific code; everything else in the
// agent, including the QSF1 wire protocol, is shared portable C.

#import <AppKit/AppKit.h>

#include <stdint.h>
#include <stdlib.h>
#include <string.h>

// A monotonic counter that NSPasteboard bumps on every change, by any app.
// The agent polls this to detect "the user copied something in the guest".
long qsm_macos_clipboard_change_count(void) {
    @autoreleasepool {
        return (long)[[NSPasteboard generalPasteboard] changeCount];
    }
}

// Read the pasteboard's plain text as UTF-8 bytes. An empty or non-text
// pasteboard is reported as empty text (data set to a 1-byte allocation,
// size 0), matching the agent's "-" empty-clipboard convention. Returns 0 on
// success (caller frees *data), -1 only on an allocation or encoding failure
// or when the text is larger than the agent's clipboard limit.
int qsm_macos_clipboard_read(uint8_t **data, size_t *size, size_t maximum) {
    if (data == NULL || size == NULL) {
        return -1;
    }
    @autoreleasepool {
        NSString *text = [[NSPasteboard generalPasteboard] stringForType:NSPasteboardTypeString];
        if (text == nil) {
            text = @"";
        }
        NSData *utf8 = [text dataUsingEncoding:NSUTF8StringEncoding];
        if (utf8 == nil) {
            return -1;
        }
        size_t length = (size_t)[utf8 length];
        if (length > maximum) {
            return -1;
        }
        uint8_t *buffer = malloc(length == 0U ? 1U : length);
        if (buffer == NULL) {
            return -1;
        }
        if (length > 0U) {
            memcpy(buffer, [utf8 bytes], length);
        }
        *data = buffer;
        *size = length;
        return 0;
    }
}

// Replace the pasteboard's contents with UTF-8 text. Returns 0 on success.
int qsm_macos_clipboard_write(const uint8_t *data, size_t size) {
    @autoreleasepool {
        NSString *text = [[NSString alloc] initWithBytes:(const void *)data
                                                  length:size
                                                encoding:NSUTF8StringEncoding];
        if (text == nil) {
            return -1;
        }
        NSPasteboard *board = [NSPasteboard generalPasteboard];
        [board clearContents];
        return [board setString:text forType:NSPasteboardTypeString] ? 0 : -1;
    }
}
