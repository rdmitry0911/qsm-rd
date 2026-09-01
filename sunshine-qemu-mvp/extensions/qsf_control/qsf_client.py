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


def request(socket_path: Path, token_file: Path, payload: dict[str, Any]) -> dict[str, Any]:
    token = token_file.read_text(encoding="ascii").strip()
    payload["token"] = token
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(12)
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", required=True, help="QSF control Unix socket")
    parser.add_argument("--token-file", required=True, help="per-session QSF token")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status")
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
