"""Validate advisory desktop state from an authenticated, untrusted guest.

The schema contains geometry and opaque identities, never window captions or
application text. All consumers must keep pixel/video fallback available.
"""
from __future__ import annotations

import json
import math
import os
import re
import stat
import time
from typing import Any

MAX_METADATA_BYTES = 48 * 1024
MAX_WINDOWS = 128
MAX_DAMAGE_WINDOWS = 64
MAX_COORDINATE = 65536
MAX_SEQUENCE = (1 << 53) - 1
_IDENTITY = re.compile(r"[A-Za-z0-9_.:{}-]{1,80}\Z")
_CAPABILITIES = {"windows", "focus", "window-damage", "caret"}


class DesktopMetadataError(ValueError):
    pass


def _integer(value: Any, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        raise DesktopMetadataError("invalid desktop integer")
    return value


def _identity(value: Any) -> str:
    if not isinstance(value, str) or not _IDENTITY.fullmatch(value):
        raise DesktopMetadataError("invalid desktop identity")
    return value


def _rect(value: Any) -> list[int]:
    if not isinstance(value, list) or len(value) != 4:
        raise DesktopMetadataError("invalid desktop rectangle")
    x, y = (_integer(v, -MAX_COORDINATE, MAX_COORDINATE) for v in value[:2])
    w, h = (_integer(v, 0, MAX_COORDINATE) for v in value[2:])
    if abs(x + w) > MAX_COORDINATE or abs(y + h) > MAX_COORDINATE:
        raise DesktopMetadataError("desktop rectangle exceeds bounds")
    return [x, y, w, h]


def validate_metadata(value: Any) -> dict[str, Any]:
    fields = {"version", "session", "sequence", "generation", "available", "locked",
              "capabilities", "desktop", "windows", "focus", "damage", "caret"}
    if (not isinstance(value, dict) or set(value) != fields or
            type(value["version"]) is not int or value["version"] not in (1, 2)):
        raise DesktopMetadataError("unsupported desktop metadata")
    session = _identity(value["session"])
    sequence = _integer(value["sequence"], 1, MAX_SEQUENCE)
    generation = _integer(value["generation"], 1, MAX_SEQUENCE)
    if type(value["available"]) is not bool or type(value["locked"]) is not bool:
        raise DesktopMetadataError("invalid desktop availability")
    caps = value["capabilities"]
    if (not isinstance(caps, list) or len(caps) > len(_CAPABILITIES) or
            any(not isinstance(c, str) or c not in _CAPABILITIES for c in caps) or len(set(caps)) != len(caps)):
        raise DesktopMetadataError("invalid desktop capabilities")
    desktop = value["desktop"]
    if not isinstance(desktop, dict) or set(desktop) != {"rect", "scale"}:
        raise DesktopMetadataError("invalid desktop output")
    scale = desktop["scale"]
    if type(scale) not in (int, float) or not math.isfinite(scale) or not .25 <= scale <= 8:
        raise DesktopMetadataError("invalid desktop scale")
    output = {"rect": _rect(desktop["rect"]), "scale": scale}
    windows = value["windows"]
    if not isinstance(windows, list) or len(windows) > MAX_WINDOWS:
        raise DesktopMetadataError("too many desktop windows")
    parsed, ids = [], set()
    for w in windows:
        if not isinstance(w, dict) or set(w) != {"id", "pid", "rect", "client_rect", "visible", "opacity"}:
            raise DesktopMetadataError("invalid desktop window")
        key = _identity(w["id"])
        if key in ids or type(w["visible"]) is not bool:
            raise DesktopMetadataError("invalid desktop window identity")
        ids.add(key)
        opacity = w["opacity"]
        if type(opacity) not in (int, float) or not math.isfinite(opacity) or not 0 <= opacity <= 1:
            raise DesktopMetadataError("invalid desktop opacity")
        frame = _rect(w['rect'])
        client = None if w['client_rect'] is None else _rect(w['client_rect'])
        if client is not None:
            x, y, width, height = frame
            cx, cy, cw, ch = client
            if cx < x or cy < y or cx + cw > x + width or cy + ch > y + height:
                raise DesktopMetadataError('client outside window frame')
        parsed.append({"id": key, "pid": _integer(w["pid"], 0, (1 << 31) - 1),
                       "rect": frame, "client_rect": client, "visible": w["visible"], "opacity": opacity})
    focus = value["focus"]
    if focus is not None and (not isinstance(focus, str) or focus not in ids):
        raise DesktopMetadataError("unknown desktop focus")
    damage = value["damage"]
    if (not isinstance(damage, list) or len(damage) > MAX_DAMAGE_WINDOWS or
            any(not isinstance(k, str) or k not in ids for k in damage) or len(set(damage)) != len(damage)):
        raise DesktopMetadataError("invalid desktop damage")
    caret = value["caret"]
    if caret is not None:
        caret_fields = {"window", "rect", "clip"} if value['version'] == 2 else {"window", "rect"}
        if not isinstance(caret, dict) or set(caret) != caret_fields or caret["window"] != focus or focus is None:
            raise DesktopMetadataError("invalid desktop caret")
        caret = {"window": focus, "rect": _rect(caret["rect"]),
                 **({'clip': None if caret['clip'] is None else _rect(caret['clip'])} if value['version'] == 2 else {})}
        win = next(w for w in parsed if w["id"] == focus)
        x, y, width, height = win['client_rect'] or win['rect']
        cx, cy, cw, ch = caret["rect"]
        if not win["visible"] or cx < x or cy < y or cx + cw > x + width or cy + ch > y + height:
            raise DesktopMetadataError("caret outside focused window")
        if value['version'] == 2 and caret['clip'] is not None:
            ix, iy, iw, ih = caret['clip']
            if (not iw or not ih or ix < x or iy < y or ix+iw > x+width or iy+ih > y+height or
                    cx < ix or cy < iy or cx+cw > ix+iw or cy+ch > iy+ih):
                raise DesktopMetadataError('caret outside input clip')
    if not value["available"] or value["locked"]:
        if parsed or focus is not None or damage or caret is not None:
            raise DesktopMetadataError("inactive desktop exposes live state")
    if caret is not None and "caret" not in caps:
        raise DesktopMetadataError("unadvertised caret metadata")
    return {"version": value['version'], "session": session, "sequence": sequence, "generation": generation,
            "available": value["available"], "locked": value["locked"], "capabilities": sorted(caps),
            "desktop": output, "windows": parsed, "focus": focus, "damage": damage, "caret": caret}


def parse_metadata(raw: bytes) -> dict[str, Any]:
    if len(raw) > MAX_METADATA_BYTES:
        raise DesktopMetadataError("desktop metadata too large")
    def unique(pairs):
        result = {}
        for k, v in pairs:
            if k in result:
                raise DesktopMetadataError("duplicate desktop field")
            result[k] = v
        return result
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=unique)
        return validate_metadata(value)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
        raise DesktopMetadataError("invalid desktop JSON") from error


def read_metadata_file(path, maximum_age: float = 3.0) -> bytes | None:
    """Read an atomic, private session file; stale writers provide no hints."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as source:
            info = os.fstat(source.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or
                    not 0 < info.st_size <= MAX_METADATA_BYTES or not -1 <= time.time() - info.st_mtime <= maximum_age):
                return None
            raw = source.read(MAX_METADATA_BYTES + 1)
        parse_metadata(raw)
        return raw
    except (OSError, DesktopMetadataError):
        return None
