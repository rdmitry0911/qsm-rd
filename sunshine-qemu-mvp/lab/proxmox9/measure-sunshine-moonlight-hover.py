#!/usr/bin/env python3
"""Measure a real q-sunshine/Moonlight input-to-pixel path in the PVE lab.

The test consumes exactly one PVE-issued launch descriptor, redeems it over
the broker's authenticated TLS endpoint, and supplies the resulting native
GameStream ticket only through an inherited pipe.  It deliberately does not
use PIN pairing or persist a client identity.  Moonlight is shown in its own
Xvfb display and the magenta hover target is detected in decoded pixels.

This complements measure-direct-browser-e2e.py: both targets are the same
VirGL guest and the same 1280x800 hover fixture, while the client transports
are their actual product paths (browser WebRTC versus Moonlight GameStream).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import select
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any


class MeasurementError(RuntimeError):
    """A bounded local laboratory failure."""


def command_output(arguments: list[str], *, input_bytes: bytes | None = None,
                   timeout: float = 10.0) -> bytes:
    try:
        return subprocess.run(arguments, input=input_bytes, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, check=True, timeout=timeout).stdout
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        # Commands in this harness carry window IDs and fixed coordinates only;
        # include their shape to distinguish a missing X surface from a failed
        # pixel capture without exposing descriptor or ticket material.
        detail = ""
        if isinstance(error, subprocess.CalledProcessError) and error.stderr:
            detail = " " + error.stderr.decode("utf-8", "replace").replace("\n", " ").strip()[:240]
        raise MeasurementError(f"laboratory command failed: {' '.join(arguments)}{detail}") from error


def parse_descriptor(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="ascii"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise MeasurementError("launch descriptor is unavailable") from error
    if not isinstance(payload, dict) or payload.get("kind") != "q-sunshine-pve-launch":
        raise MeasurementError("launch descriptor is invalid")
    endpoint = payload.get("endpoint")
    if not isinstance(endpoint, dict):
        raise MeasurementError("launch descriptor endpoint is invalid")
    for key in ("host", "server_name", "ca_pem"):
        if not isinstance(endpoint.get(key), str) or not endpoint[key]:
            raise MeasurementError("launch descriptor endpoint is invalid")
    if not isinstance(endpoint.get("port"), int) or not 1 <= endpoint["port"] <= 65535:
        raise MeasurementError("launch descriptor endpoint is invalid")
    if not isinstance(payload.get("claim"), str) or not payload["claim"].startswith("qsd1."):
        raise MeasurementError("launch descriptor claim is invalid")
    return payload


def recv_line(connection: ssl.SSLSocket, maximum: int = 8192) -> dict[str, Any]:
    result = bytearray()
    while not result.endswith(b"\n"):
        part = connection.recv(maximum + 1 - len(result))
        if not part:
            raise MeasurementError("terminal broker closed its response")
        result.extend(part)
        if len(result) > maximum:
            raise MeasurementError("terminal broker response is excessive")
    try:
        payload = json.loads(result[:-1].decode("ascii"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise MeasurementError("terminal broker response is invalid") from error
    if not isinstance(payload, dict):
        raise MeasurementError("terminal broker response is invalid")
    return payload


def redeem_descriptor(descriptor: dict[str, Any]) -> dict[str, Any]:
    endpoint = descriptor["endpoint"]
    context = ssl.create_default_context(cadata=endpoint["ca_pem"])
    context.check_hostname = True
    try:
        with socket.create_connection((endpoint["host"], endpoint["port"]), timeout=12.0) as raw:
            with context.wrap_socket(raw, server_hostname=endpoint["server_name"]) as connection:
                request = json.dumps({"version": 1, "op": "redeem_launch",
                                      "claim": descriptor["claim"]},
                                     separators=(",", ":")).encode("ascii") + b"\n"
                connection.sendall(request)
                response = recv_line(connection)
    except (OSError, ssl.SSLError) as error:
        raise MeasurementError("terminal broker TLS redemption failed") from error
    # The node terminal keeps a generic {ok,result} envelope for both native
    # descriptor redemption and its lease request. Do not accept an error or
    # arbitrary result shape as a usable route.
    if response.get("ok") is not True or not isinstance(response.get("result"), dict):
        raise MeasurementError("terminal broker rejected launch redemption")
    response = response["result"]
    routes = response.get("routes")
    if (response.get("version") != 1 or not isinstance(response.get("session_token"), str) or
            not isinstance(response.get("audience"), str) or not isinstance(response.get("ca_pem"), str) or
            not isinstance(response.get("server_name"), str) or not isinstance(routes, dict)):
        # Field names are public protocol shape, not credential material. They
        # make a version-skew failure diagnosable without printing a ticket,
        # descriptor claim, certificate, or route value.
        fields = ",".join(sorted(str(key) for key in response))
        raise MeasurementError(f"terminal broker returned an invalid launch route fields={fields}")
    for route_name in ("media", "lease"):
        route = routes.get(route_name)
        if (not isinstance(route, dict) or not isinstance(route.get("host"), str) or
                not isinstance(route.get("port"), int) or not route["host"] or
                not 1 <= route["port"] <= 65535):
            raise MeasurementError("terminal broker returned an invalid launch route")
    return response


def stream_windows() -> list[str]:
    """Return visible Moonlight stream surfaces without treating none as an error."""
    try:
        # xdotool uses a non-zero status for the normal "no matching window
        # yet" condition; that must remain a polling state, not a transport
        # failure.
        probe = subprocess.run(["xdotool", "search", "--onlyvisible", "--class",
                                "^com[.]moonlight_stream[.]Moonlight$"], stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, check=False, timeout=3.0)
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [line.strip() for line in probe.stdout.decode("ascii", "ignore").splitlines()
            if line.strip()]


def find_window(display: str, deadline: float) -> str:
    del display  # DISPLAY is intentionally inherited from the isolated Xvfb.
    while time.monotonic() < deadline:
        windows = stream_windows()
        if windows:
            return windows[-1]
        time.sleep(0.05)
    raise MeasurementError("Moonlight did not create a visible stream window")


def window_size(window: str) -> tuple[int, int]:
    output = command_output(["xdotool", "getwindowgeometry", "--shell", window]).decode("ascii")
    values: dict[str, int] = {}
    for line in output.splitlines():
        key, separator, value = line.partition("=")
        if separator and key in {"WIDTH", "HEIGHT"} and value.isdecimal():
            values[key] = int(value)
    if values.get("WIDTH", 0) < 64 or values.get("HEIGHT", 0) < 64:
        raise MeasurementError("Moonlight window has invalid geometry")
    return values["WIDTH"], values["HEIGHT"]


def popup_bounds(window: str) -> dict[str, int] | None:
    width, height = window_size(window)
    xwd = command_output(["xwd", "-silent", "-id", window], timeout=3.0)
    rgba = command_output(["convert", "xwd:-", "rgba:-"], input_bytes=xwd, timeout=4.0)
    if len(rgba) != width * height * 4:
        raise MeasurementError("Moonlight screenshot has unexpected size")
    minimum_x, minimum_y, maximum_x, maximum_y = width, height, -1, -1
    # The guest's dedicated popover is #ff00ff.  Sampling every fourth pixel
    # keeps screenshot polling below one display frame while tolerating H.264
    # 4:2:0 reconstruction around the boundary.
    for y in range(0, height, 4):
        row = y * width * 4
        for x in range(0, width, 4):
            offset = row + x * 4
            if rgba[offset] > 180 and rgba[offset + 1] < 100 and rgba[offset + 2] > 180:
                minimum_x = min(minimum_x, x)
                minimum_y = min(minimum_y, y)
                maximum_x = max(maximum_x, x)
                maximum_y = max(maximum_y, y)
    if maximum_x < 0:
        return None
    return {"minX": minimum_x, "minY": minimum_y, "maxX": maximum_x, "maxY": maximum_y}


def move_pointer(window: str, x: int, y: int) -> None:
    # Moonlight's SDL stream surface is reparented by Qt while negotiation
    # finishes.  xdotool's ``--window`` mode consequently races a transient
    # child XID even though the displayed top-level stream remains valid.
    # This test uses a private 1280x800 Xvfb with the stream at origin, so
    # absolute coordinates are the same causal guest coordinates and do not
    # depend on that implementation detail.
    del window
    # ``--sync`` waits for a physical cursor position transition, which Xvfb
    # intentionally does not guarantee.  xdotool still flushes the XTEST
    # event before returning, which is the causal send boundary we measure.
    command_output(["xdotool", "mousemove", str(x), str(y)], timeout=3.0)


def fixture_blue_visible(window: str) -> bool:
    """Detect the known blue hover icon in decoded fixture pixels.

    A generic non-black check accepts Moonlight's transient connection dialog,
    which made a disappearing splash surface look like a working stream.  The
    fixture's #2b6cff rectangle is a stable, codec-tolerant readiness probe.
    """
    width, height = window_size(window)
    xwd = command_output(["xwd", "-silent", "-id", window], timeout=3.0)
    rgba = command_output(["convert", "xwd:-", "rgba:-"], input_bytes=xwd, timeout=4.0)
    if len(rgba) != width * height * 4:
        raise MeasurementError("Moonlight screenshot has unexpected size")
    blue_pixels = 0
    for y in range(0, height, 4):
        row = y * width * 4
        for x in range(0, width, 4):
            offset = row + x * 4
            red, green, blue = rgba[offset], rgba[offset + 1], rgba[offset + 2]
            if red < 110 and 45 < green < 180 and blue > 160 and blue > green + 55:
                blue_pixels += 1
                if blue_pixels >= 20:
                    return True
    return False


def wait_for_decoded_fixture(process: subprocess.Popen[bytes], deadline: float) -> str:
    """Wait through Moonlight splash-window replacement for actual guest video."""
    last_problem = "no visible Moonlight stream surface"
    while time.monotonic() < deadline:
        diagnostic = moonlight_exit_diagnostic(process)
        if diagnostic:
            raise MeasurementError(diagnostic)
        for window in reversed(stream_windows()):
            try:
                if fixture_blue_visible(window):
                    # Require the same surface to live for two samples.  This
                    # excludes a painted connection/splash UI whose X window
                    # is immediately destroyed during stream negotiation.
                    time.sleep(0.15)
                    if window in stream_windows() and fixture_blue_visible(window):
                        return window
            except MeasurementError as error:
                last_problem = str(error)
        time.sleep(0.03)
    diagnostic = moonlight_exit_diagnostic(process)
    if diagnostic:
        raise MeasurementError(diagnostic)
    raise MeasurementError("Moonlight did not present decoded fixture: " + last_problem)


def measure_hover(window: str, runs: int) -> list[dict[str, Any]]:
    measurements: list[dict[str, Any]] = []
    for _ in range(runs):
        move_pointer(window, 40, 40)
        reset_deadline = time.monotonic() + 4.0
        while popup_bounds(window) is not None:
            if time.monotonic() >= reset_deadline:
                raise MeasurementError("Moonlight hover popup did not clear")
        started = time.monotonic_ns()
        move_pointer(window, 640, 370)
        deadline = time.monotonic() + 8.0
        samples = 0
        while True:
            bounds = popup_bounds(window)
            samples += 1
            if bounds is not None:
                measurements.append({"latencyMs": (time.monotonic_ns() - started) / 1_000_000.0,
                                     "captureSamples": samples, "popupBounds": bounds})
                break
            if time.monotonic() >= deadline:
                raise MeasurementError("Moonlight hover popup was not presented before timeout")
    return measurements


def require_commands() -> None:
    missing = [name for name in ("Xvfb", "xdotool", "xwd", "convert") if shutil.which(name) is None]
    if missing:
        raise MeasurementError("laboratory client is missing: " + ", ".join(missing))


def moonlight_exit_diagnostic(process: subprocess.Popen[bytes]) -> str:
    """Return a short redacted failure reason from a terminated child."""
    status = process.poll()
    if status is None:
        return ""
    try:
        output = process.stdout.read(4096) if process.stdout is not None else b""
    except OSError:
        output = b""
    text = output.decode("utf-8", "replace").replace("\r", " ").replace("\n", " ")
    # Defensive redaction: a transport ticket or private key must never enter
    # a diagnostic line even if a future upstream logging path regresses.
    text = re.sub(r"(?:qsd1|qsa1|qst1)\.[A-Za-z0-9_-]+", "[redacted]", text)
    text = re.sub(r"(?i)((?:ticket|token|password|private[_ -]?key))=[^ ]+", r"\1=[redacted]", text)
    return f"Moonlight exited code={status} {text.strip()[:480]}"


def moonlight_surface_failure_diagnostic(process: subprocess.Popen[bytes]) -> str:
    """Collect a bounded, redacted Moonlight log tail after a lost X surface."""
    # A stream error can first destroy its SDL window, then return to the
    # long-lived Moonlight UI process.  Give that transition a short bounded
    # time before sampling its already-written stdout; never block waiting for
    # a GUI process which is intentionally kept alive by --no-quit-after.
    time.sleep(0.35)
    status = process.poll()
    output = bytearray()
    if process.stdout is not None:
        descriptor = process.stdout.fileno()
        while len(output) < 4096:
            ready, _, _ = select.select([descriptor], [], [], 0.0)
            if not ready:
                break
            try:
                chunk = os.read(descriptor, 4096 - len(output))
            except OSError:
                break
            if not chunk:
                break
            output.extend(chunk)
    text = output.decode("utf-8", "replace").replace("\r", " ").replace("\n", " ")
    text = re.sub(r"(?:qsd1|qsa1|qst1)\.[A-Za-z0-9_-]+", "[redacted]", text)
    text = re.sub(r"(?i)((?:ticket|token|password|private[_ -]?key))=[^ ]+", r"\1=[redacted]", text)
    state = f"exited code={status}" if status is not None else "remained running"
    visible: list[str] = []
    try:
        probe = subprocess.run(["xdotool", "search", "--onlyvisible", "--name", "."],
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               check=False, timeout=3.0)
        for candidate in probe.stdout.decode("ascii", "ignore").splitlines()[:8]:
            candidate = candidate.strip()
            if not candidate:
                continue
            name = subprocess.run(["xdotool", "getwindowname", candidate], stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, check=False, timeout=1.0)
            visible.append(f"{candidate}:{name.stdout.decode('utf-8', 'replace').strip()[:80]}")
    except (OSError, subprocess.TimeoutExpired):
        pass
    # Startup noise is expected; the final lines describe the stream teardown.
    return (f"Moonlight {state}; visible={','.join(visible) or '[none]'}; "
            f"log={text.strip()[-4096:] or '[no stdout diagnostic]'}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--launch-file", type=Path, required=True)
    parser.add_argument("--moonlight", type=Path, required=True)
    parser.add_argument("--runs", type=int, default=10)
    parser.add_argument("--display", default=":98")
    arguments = parser.parse_args()
    if not arguments.launch_file.is_absolute() or not arguments.moonlight.is_absolute() or not arguments.moonlight.is_file():
        raise MeasurementError("launch file and Moonlight binary must be absolute regular files")
    if not 1 <= arguments.runs <= 30 or not arguments.display.startswith(":"):
        raise MeasurementError("invalid measurement settings")
    require_commands()
    # xdotool/xwd obtain their X server solely from DISPLAY. Set it before
    # the first probe so every client-side measurement observes the dedicated
    # Moonlight surface rather than a caller's desktop.
    os.environ["DISPLAY"] = arguments.display
    descriptor = parse_descriptor(arguments.launch_file)
    redeemed = redeem_descriptor(descriptor)
    temporary_directory = Path(tempfile.mkdtemp(prefix="qsm-sunshine-moonlight-", dir="/tmp"))
    os.chmod(temporary_directory, 0o700)
    certificate = temporary_directory / "terminal-ca.pem"
    certificate.write_text(redeemed["ca_pem"], encoding="ascii")
    os.chmod(certificate, 0o600)
    ticket_read, ticket_write = os.pipe2(os.O_CLOEXEC)
    xvfb: subprocess.Popen[bytes] | None = None
    moonlight: subprocess.Popen[bytes] | None = None
    try:
        xvfb = subprocess.Popen(["Xvfb", arguments.display, "-screen", "0", "1280x800x24", "-nolisten", "tcp"],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            if xvfb.poll() is not None:
                raise MeasurementError("Xvfb exited before Moonlight started")
            if os.path.exists(f"/tmp/.X11-unix/X{arguments.display[1:]}"):
                break
            time.sleep(0.05)
        else:
            raise MeasurementError("Xvfb did not become ready")
        routes = redeemed["routes"]
        environment = {
            "DISPLAY": arguments.display,
            "HOME": str(temporary_directory),
            "XDG_CONFIG_HOME": str(temporary_directory / "config"),
            "XDG_DATA_HOME": str(temporary_directory / "data"),
            "XDG_CACHE_HOME": str(temporary_directory / "cache"),
            "QSM_GAMESTREAM_AUTH_HOST": routes["lease"]["host"],
            "QSM_GAMESTREAM_AUTH_PORT": str(routes["lease"]["port"]),
            "QSM_GAMESTREAM_AUTH_SNI": redeemed["server_name"],
            "QSM_GAMESTREAM_AUTH_CA_FILE": str(certificate),
            "QSM_GAMESTREAM_AUDIENCE": redeemed["audience"],
            "QSM_GAMESTREAM_TICKET_FD": str(ticket_read),
            "QSM_GAMESTREAM_HOST": routes["media"]["host"],
            "QSM_GAMESTREAM_HTTPS_PORT": str(routes["media"]["port"] - 5),
        }
        for directory in (environment["XDG_CONFIG_HOME"], environment["XDG_DATA_HOME"], environment["XDG_CACHE_HOME"]):
            Path(directory).mkdir(mode=0o700)
        moonlight = subprocess.Popen(
            [str(arguments.moonlight), "stream", "--qsm-system-auth", "--display-mode", "windowed",
             "--resolution", "1280x800", "--fps", "60", "--bitrate", "16000", "--video-codec", "H.264",
             "--video-decoder", "software", "--no-quit-after", "--absolute-mouse", "--", routes["media"]["host"],
             "QEMU Console"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            env=environment, pass_fds=(ticket_read,), close_fds=True)
        os.write(ticket_write, redeemed["session_token"].encode("ascii"))
        os.close(ticket_write)
        ticket_write = -1
        # Moonlight can create and replace a splash X surface while it opens
        # GameStream.  Select the surviving surface only after the actual
        # known guest fixture has appeared in decoded pixels.
        try:
            window = wait_for_decoded_fixture(moonlight, time.monotonic() + 30.0)
        except MeasurementError as error:
            # A modern Moonlight can keep its Qt process alive after its SDL
            # stream window fails. Include the bounded, redacted child tail
            # in that otherwise indistinguishable timeout.
            raise MeasurementError(f"{error}; {moonlight_surface_failure_diagnostic(moonlight)}") from error
        diagnostic = moonlight_exit_diagnostic(moonlight)
        if diagnostic:
            raise MeasurementError(diagnostic)
        # Clear any residue from the connection-animation sequence before the
        # first causal mouse event.
        try:
            move_pointer(window, 40, 40)
        except MeasurementError as error:
            raise MeasurementError(f"{error}; {moonlight_surface_failure_diagnostic(moonlight)}") from error
        try:
            result = {"runs": measure_hover(window, arguments.runs)}
        except MeasurementError as error:
            raise MeasurementError(f"{error}; {moonlight_surface_failure_diagnostic(moonlight)}") from error
        values = [entry["latencyMs"] for entry in result["runs"]]
        result["meanLatencyMs"] = sum(values) / len(values)
        result["minLatencyMs"] = min(values)
        result["maxLatencyMs"] = max(values)
        print("QSM_LAB_SUNSHINE_MOONLIGHT_HOVER " + json.dumps(result, separators=(",", ":"), sort_keys=True))
        return 0
    finally:
        for descriptor in (ticket_read, ticket_write):
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
        for process in (moonlight, xvfb):
            if process is not None and process.poll() is None:
                process.terminate()
        for process in (moonlight, xvfb):
            if process is not None:
                try:
                    process.wait(timeout=5.0)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5.0)
        # This is the exact private mkdtemp directory created above; remove
        # the short-lived public CA copy and Moonlight's ephemeral profile
        # even when the client populated nested XDG directories.
        shutil.rmtree(temporary_directory, ignore_errors=True)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except MeasurementError as error:
        print(f"qsm sunshine/moonlight latency: {error}", file=sys.stderr)
        raise SystemExit(2)
