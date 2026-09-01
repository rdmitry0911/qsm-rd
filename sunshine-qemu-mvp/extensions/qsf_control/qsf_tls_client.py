#!/usr/bin/env python3
"""Client CLI for the mutual-TLS QSF clipboard, file, and resize companion."""

from __future__ import annotations

import argparse
import base64
import json
import socket
import ssl
import sys
from pathlib import Path
from typing import Any


MAX_REPLY = 4 * 1024 * 1024
MAX_CLIPBOARD_BYTES = 1024 * 1024
MAX_FILE_BYTES = 2 * 1024 * 1024


def read_line(connection: socket.socket) -> bytes:
    response = bytearray()
    while b"\n" not in response:
        chunk = connection.recv(min(65536, MAX_REPLY + 1 - len(response)))
        if not chunk:
            break
        response.extend(chunk)
        if len(response) > MAX_REPLY:
            raise RuntimeError("QSF TLS reply is too large")
    if not response or b"\n" not in response:
        raise RuntimeError("QSF TLS peer closed without a complete reply")
    return bytes(response).split(b"\n", 1)[0]


def request(arguments: argparse.Namespace, payload: dict[str, Any]) -> dict[str, Any]:
    context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH,
                                         cafile=arguments.ca_file)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.load_cert_chain(arguments.cert_file, arguments.key_file)
    server_name = arguments.server_name or arguments.host
    with socket.create_connection((arguments.host, arguments.port), timeout=12) as raw:
        with context.wrap_socket(raw, server_hostname=server_name) as connection:
            connection.settimeout(12)
            connection.sendall(json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\n")
            response = read_line(connection)
    decoded = json.loads(response.decode("utf-8"))
    if not isinstance(decoded, dict) or not decoded.get("ok"):
        raise RuntimeError(decoded.get("error", "unknown QSF TLS error")
                           if isinstance(decoded, dict) else "malformed QSF TLS response")
    result = decoded.get("result")
    if not isinstance(result, dict):
        raise RuntimeError("malformed QSF TLS result")
    return result


def bounded_read(path: Path, maximum: int, label: str) -> bytes:
    data = path.read_bytes()
    if len(data) > maximum:
        raise RuntimeError(f"{label} exceeds {maximum} bytes")
    return data


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True, help="QSF TLS gateway host")
    parser.add_argument("--port", type=int, default=48122, help="QSF TLS gateway port")
    parser.add_argument("--ca-file", required=True, help="PEM CA for the gateway certificate")
    parser.add_argument("--cert-file", required=True, help="PEM client certificate")
    parser.add_argument("--key-file", required=True, help="PEM client private key")
    parser.add_argument("--server-name", help="expected TLS server name (default: --host)")
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
    if not 1 <= arguments.port <= 65535:
        parser.error("--port must be between 1 and 65535")

    try:
        if arguments.command == "status":
            result = request(arguments, {"op": "status"})
            print(json.dumps(result, sort_keys=True))
        elif arguments.command == "clipboard-get":
            result = request(arguments, {"op": "clipboard_get"})
            sys.stdout.buffer.write(base64.b64decode(result["text_b64"], validate=True))
        elif arguments.command == "clipboard-set":
            data = sys.stdin.buffer.read(MAX_CLIPBOARD_BYTES + 1)
            if len(data) > MAX_CLIPBOARD_BYTES:
                raise RuntimeError("clipboard exceeds 1048576 bytes")
            result = request(arguments, {
                "op": "clipboard_set",
                "text_b64": base64.b64encode(data).decode("ascii"),
            })
            print(json.dumps(result, sort_keys=True))
        elif arguments.command == "upload":
            data = bounded_read(arguments.source, MAX_FILE_BYTES, "upload")
            result = request(arguments, {
                "op": "upload",
                "name": arguments.name or arguments.source.name,
                "data_b64": base64.b64encode(data).decode("ascii"),
            })
            print(json.dumps(result, sort_keys=True))
        elif arguments.command == "download":
            result = request(arguments, {"op": "download", "name": arguments.name})
            data = base64.b64decode(result["data_b64"], validate=True)
            if len(data) > MAX_FILE_BYTES:
                raise RuntimeError("download exceeds 2097152 bytes")
            arguments.destination.write_bytes(data)
            print(json.dumps({"name": result["name"], "bytes": result["bytes"]}, sort_keys=True))
        else:
            result = request(arguments, {
                "op": "resize", "width": arguments.width, "height": arguments.height,
            })
            print(json.dumps(result, sort_keys=True))
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError, ssl.SSLError) as error:
        print(f"qsf-tls-client: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
