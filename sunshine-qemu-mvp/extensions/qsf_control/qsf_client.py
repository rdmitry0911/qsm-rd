#!/usr/bin/env python3
"""Client CLI for the local QSF clipboard, file, and resize side-channel."""

from __future__ import annotations

import argparse
import base64
import json
import socket
import sys
from pathlib import Path
from typing import Any


MAX_REPLY = 4 * 1024 * 1024
SUPPORTED_CODECS = frozenset({"H264", "HEVC", "AV1"})
PROFILE_APPLY_TIMEOUT_SECONDS = 40


def request(socket_path: Path, token_file: Path, payload: dict[str, Any]) -> dict[str, Any]:
    token = token_file.read_text(encoding="ascii").strip()
    payload["token"] = token
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        # A negotiated profile waits for a bounded guest compositor restart
        # and an actual scanout acknowledgement.
        client.settimeout(PROFILE_APPLY_TIMEOUT_SECONDS)
        client.connect(str(socket_path))
        client.sendall(json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\n")
        response = bytearray()
        while b"\n" not in response:
            chunk = client.recv(65536)
            if not chunk:
                break
            response.extend(chunk)
            if len(response) > MAX_REPLY:
                raise RuntimeError("QSF control reply is too large")
    decoded = json.loads(bytes(response).split(b"\n", 1)[0].decode("utf-8"))
    if not decoded.get("ok"):
        raise RuntimeError(decoded.get("error", "unknown QSF control error"))
    return decoded["result"]


def connection_optimize_payload(resolution: str, max_fps: int, decoder_codecs: str) -> dict[str, Any]:
    try:
        width_text, height_text = resolution.split("x", 1)
        if not width_text.isdecimal() or not height_text.isdecimal():
            raise ValueError
        width, height = int(width_text), int(height_text)
    except ValueError as error:
        raise RuntimeError("--resolution must be WIDTHxHEIGHT") from error
    if not 64 <= width <= 16384 or not 64 <= height <= 16384:
        raise RuntimeError("--resolution dimensions must be in 64..16384")
    if not 10 <= max_fps <= 240:
        raise RuntimeError("--max-fps must be in 10..240")
    codecs = decoder_codecs.split(",")
    if not 1 <= len(codecs) <= 3 or any(codec not in SUPPORTED_CODECS for codec in codecs) or \
            len(set(codecs)) != len(codecs):
        raise RuntimeError("--decoder-codecs must be unique H264,HEVC,AV1 names")
    return {"op": "connection_optimize", "client": {
        "requested_width": width,
        "requested_height": height,
        "max_fps": max_fps,
        "decoder_codecs": codecs,
    }}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", required=True, help="QSF control Unix socket")
    parser.add_argument("--token-file", required=True, help="per-session QSF token")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status")
    optimize = commands.add_parser("optimize-connection")
    optimize.add_argument("--resolution", required=True, help="client-selected WIDTHxHEIGHT")
    optimize.add_argument("--max-fps", type=int, default=60, help="client decoder/display FPS ceiling")
    optimize.add_argument("--decoder-codecs", default="H264",
                         help="comma-separated verified decoder codecs (default: H264)")
    commands.add_parser("clipboard-get")
    commands.add_parser("clipboard-set")
    upload = commands.add_parser("upload")
    upload.add_argument("source", type=Path)
    upload.add_argument("--name")
    download = commands.add_parser("download")
    download.add_argument("name")
    download.add_argument("destination", type=Path)
    resize = commands.add_parser("resize")
    resize.add_argument("width", type=int)
    resize.add_argument("height", type=int)
    arguments = parser.parse_args()

    try:
        if arguments.command == "status":
            result = request(Path(arguments.socket), Path(arguments.token_file), {"op": "status"})
            print(json.dumps(result, sort_keys=True))
        elif arguments.command == "optimize-connection":
            result = request(Path(arguments.socket), Path(arguments.token_file),
                             connection_optimize_payload(arguments.resolution, arguments.max_fps,
                                                         arguments.decoder_codecs))
            print(json.dumps(result, sort_keys=True))
        elif arguments.command == "clipboard-get":
            result = request(Path(arguments.socket), Path(arguments.token_file), {"op": "clipboard_get"})
            sys.stdout.buffer.write(base64.b64decode(result["text_b64"], validate=True))
        elif arguments.command == "clipboard-set":
            data = sys.stdin.buffer.read()
            result = request(
                Path(arguments.socket), Path(arguments.token_file),
                {"op": "clipboard_set", "text_b64": base64.b64encode(data).decode("ascii")},
            )
            print(json.dumps(result, sort_keys=True))
        elif arguments.command == "upload":
            data = arguments.source.read_bytes()
            result = request(
                Path(arguments.socket), Path(arguments.token_file),
                {
                    "op": "upload",
                    "name": arguments.name or arguments.source.name,
                    "data_b64": base64.b64encode(data).decode("ascii"),
                },
            )
            print(json.dumps(result, sort_keys=True))
        elif arguments.command == "download":
            result = request(
                Path(arguments.socket), Path(arguments.token_file),
                {"op": "download", "name": arguments.name},
            )
            arguments.destination.write_bytes(base64.b64decode(result["data_b64"], validate=True))
            print(json.dumps({"name": result["name"], "bytes": result["bytes"]}, sort_keys=True))
        else:
            result = request(
                Path(arguments.socket), Path(arguments.token_file),
                {"op": "resize", "width": arguments.width, "height": arguments.height},
            )
            print(json.dumps(result, sort_keys=True))
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as error:
        print(f"qsf-client: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
