"""Reduce untrusted geometry to one bounded, unoccluded text priority area.

This is a hint, not authority to paint pixels. The capture worker and viewer
still enforce layout/capture fences and ordinary pixel fallback.
"""
from __future__ import annotations
import secrets
import struct
from .qsm_desktop_metadata import validate_metadata

HINT = struct.Struct('!B3xQIIIIII')


def intersection(a, b):
    x, y = max(a[0], b[0]), max(a[1], b[1])
    right, bottom = min(a[0] + a[2], b[0] + b[2]), min(a[1] + a[3], b[1] + b[3])
    return [x, y, right - x, bottom - y] if right > x and bottom > y else None


def contains(a, b):
    return a[0] <= b[0] and a[1] <= b[1] and a[0] + a[2] >= b[0] + b[2] and a[1] + a[3] >= b[1] + b[3]


class DesktopPriority:
    def __init__(self):
        self._identity = None
        self._layout = secrets.randbits(40) + 1

    def update(self, value):
        if value is None:
            self._identity = None
            return HINT.pack(0, 0, 0, 0, 0, 0, 0, 0)
        value = validate_metadata(value)
        desktop = value['desktop']['rect']
        if not value['available'] or value['locked'] or not desktop[2] or not desktop[3]:
            return self.update(None)
        identity = (value['session'], value['generation'], tuple(desktop), value['desktop']['scale'], value['focus'],
                    tuple((w['id'], w['pid'], tuple(w['rect']), tuple(w['client_rect']) if w['client_rect'] else None,
                           w['visible'], w['opacity']) for w in value['windows']))
        if identity != self._identity:
            self._layout += 1
            self._identity = identity
        layout = self._layout
        windows = value['windows']
        index = next((i for i, w in enumerate(windows) if w['id'] == value['focus']), None)
        caret = value['caret']
        area = None
        if index is not None and caret:
            focus = windows[index]
            if focus['visible'] and focus['opacity'] == 1 and focus['client_rect'] is not None:
                x, y, w, h = caret['rect']
                area = intersection([x - 256, y - 128, w + 768, h + 256], focus['client_rect'])
                area = intersection(area, desktop) if area else None
                for upper in windows[index + 1:]:
                    if area is None:
                        break
                    if not upper['visible'] or upper['opacity'] == 0 or not intersection(area, upper['rect']):
                        continue
                    if intersection(caret['rect'], upper['rect']):
                        area = None
                        break
                    # Keep a rectangle containing the complete caret box.
                    # Every retained candidate excludes this occluder; prior
                    # exclusions remain valid because candidates only shrink.
                    ax, ay, aw, ah = area
                    ux, uy, uw, uh = upper['rect']
                    candidates = [[ax, ay, ux - ax, ah], [ux + uw, ay, ax + aw - ux - uw, ah],
                                  [ax, ay, aw, uy - ay], [ax, uy + uh, aw, ay + ah - uy - uh]]
                    candidates = [r for r in candidates if r[2] > 0 and r[3] > 0 and contains(area, r) and contains(r, caret['rect'])]
                    area = max(candidates, key=lambda r: r[2] * r[3]) if candidates else None
        if area:
            x, y, w, h = area
            return HINT.pack(3, layout, desktop[2], desktop[3], x - desktop[0], y - desktop[1], w, h)
        return HINT.pack(1, layout, desktop[2], desktop[3], 0, 0, 0, 0)
