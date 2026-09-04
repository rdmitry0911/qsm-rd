#!/usr/bin/env python3
"""Stress and lifecycle acceptance suite for a packaged QSM Direct VM.

Run as root *on the PVE node*.  The browser peer is deliberately outside that
node, so every successful case includes an ordinary Chrome H.264 decoder,
real UDP/DTLS/SRTP, the WebRTC input data channels, and QEMU Display1.  It is
not an aiortc-to-aiortc substitute.

The suite is intentionally stateful: it holds an open Console while restarting
the VM and the terminal service, then proves that a fresh Console has current
pixels and live input channels.  That catches the stale-UEFI-frame regression
which a sequence of independent short connections cannot detect.
"""

from __future__ import annotations

import argparse
import json
import re
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


class StressFailure(RuntimeError):
    """One required direct-console contract did not hold."""


HERE = Path(__file__).resolve().parent
MEASURE = HERE / "measure-direct-browser-e2e.py"
MARKER = "QSM_LAB_DIRECT_CHROME_E2E "


def _command(arguments: argparse.Namespace, *, width: int, height: int,
             hold_seconds: float = 0.0, guest_transfer: bool = False,
             expect_disconnect: bool = False,
             viewport_resizes: tuple[tuple[int, int], ...] = (),
             screenshot: str = "") -> list[str]:
    command = [
        sys.executable, os.fspath(MEASURE),
        "--socket", arguments.socket,
        "--node", arguments.node,
        "--vmid", str(arguments.vmid),
        "--width", str(width), "--height", str(height),
        "--fps", str(arguments.fps),
        "--warmup-seconds", str(arguments.warmup_seconds),
        "--resize-settle-seconds", str(arguments.resize_settle_seconds),
        "--hold-seconds", str(hold_seconds),
        "--browser-host", arguments.browser_host,
        "--browser-user", arguments.browser_user,
        "--browser-key", arguments.browser_key,
        "--browser-script", arguments.browser_script,
        "--browser-executable", arguments.browser_executable,
    ]
    if arguments.browser_headful:
        command.append("--browser-headful")
    if arguments.browser_display:
        command.extend(["--browser-display", arguments.browser_display])
    if arguments.browser_ice_server:
        command.extend(["--browser-ice-server", arguments.browser_ice_server])
    if guest_transfer:
        command.extend(["--guest-transfer", "--guest-file-bytes", str(arguments.guest_file_bytes)])
    if expect_disconnect:
        command.append("--expect-disconnect")
    if screenshot:
        command.extend(["--screenshot", screenshot])
    if arguments.max_edge_luma is not None:
        command.extend(["--max-edge-luma", str(arguments.max_edge_luma)])
    for resize_width, resize_height in viewport_resizes:
        command.extend(["--viewport-resize", f"{resize_width}x{resize_height}"])
    return command


def _result(output: str, name: str) -> dict[str, Any]:
    for line in output.splitlines():
        if line.startswith(MARKER):
            try:
                value = json.loads(line[len(MARKER):])
            except json.JSONDecodeError as error:
                raise StressFailure(f"{name}: invalid E2E JSON") from error
            if isinstance(value, dict):
                return value
    raise StressFailure(f"{name}: Chrome E2E did not produce its completion marker")


def _check_live(result: dict[str, Any], name: str, width: int, height: int) -> None:
    video = result.get("video")
    pixels = result.get("pixels")
    if not isinstance(video, dict) or not isinstance(pixels, dict):
        raise StressFailure(f"{name}: incomplete browser evidence")
    if video.get("connectionState") != "connected" or video.get("pointerReady") is not True or \
            video.get("controlReady") is not True:
        raise StressFailure(f"{name}: browser data/media channels are not connected")
    if video.get("videoWidth") != width or video.get("videoHeight") != height:
        raise StressFailure(f"{name}: decoded geometry is not {width}x{height}")
    if int(result.get("deltaFramesDecoded", 0)) < 1:
        raise StressFailure(f"{name}: Chrome did not decode a progressing video stream")
    if int(pixels.get("nonBlack", 0)) < 8 or int(pixels.get("lumaMax", 0)) <= int(pixels.get("lumaMin", 0)):
        raise StressFailure(f"{name}: Chrome received an empty or black Display1 frame")
    exercise = result.get("inputExercise")
    if not isinstance(exercise, dict) or exercise.get("pointerSweep") != 3 or \
            exercise.get("mouseClick") is not True or exercise.get("keyboard") != "left-shift":
        raise StressFailure(f"{name}: browser did not exercise all direct input lanes")
    layout = video.get("layout")
    if not isinstance(layout, dict) or layout.get("viewportWidth") != width or \
            layout.get("viewportHeight") != height or layout.get("objectFit") != "contain" or \
            layout.get("fillsViewport") is not True or layout.get("contentFillsViewport") is not True:
        raise StressFailure(f"{name}: decoded frame does not fill its final browser viewport")


def _check_settled_viewport(state: object, name: str, width: int, height: int) -> None:
    """Assert that a held Console still shows the last requested Display1 mode."""
    if not isinstance(state, dict) or state.get("connectionState") != "connected":
        raise StressFailure(f"{name}: Console was not connected after the competing resize")
    if state.get("videoWidth") != width or state.get("videoHeight") != height:
        raise StressFailure(f"{name}: another Console restored a stale {state.get('videoWidth')}x{state.get('videoHeight')} mode")
    layout = state.get("layout")
    if not isinstance(layout, dict) or layout.get("contentFillsViewport") is not True:
        raise StressFailure(f"{name}: last-resized Console is letterboxed after a competing viewer")


EVENT_DEVICE = re.compile(r"^/dev/input/event[0-9]{1,4}$")


def _guest_capture(arguments: argparse.Namespace, device: str) -> subprocess.Popen[bytes] | None:
    """Read exactly one evdev record while the first real Chrome case runs.

    The optional lane belongs to the disposable guest image, not to the QSM
    product.  It makes a WebRTC input assertion physically observable beyond
    the browser data-channel state.  No guest command beyond a bounded,
    read-only event-device read is issued.
    """
    if not arguments.guest_input_host:
        return None
    if not EVENT_DEVICE.fullmatch(device):
        raise StressFailure("guest input device must be /dev/input/eventN")
    command = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
               "-o", "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR"]
    if arguments.guest_input_key:
        command.extend(["-i", arguments.guest_input_key])
    command.extend([
        f"{arguments.guest_input_user}@{arguments.guest_input_host}",
        "sudo", "-n", "timeout", "45", "dd", f"if={device}", "bs=24", "count=1", "status=none",
    ])
    return subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def _await_guest_capture(process: subprocess.Popen[bytes], label: str) -> int:
    try:
        stdout, stderr = process.communicate(timeout=50)
    except subprocess.TimeoutExpired as error:
        process.kill()
        process.communicate()
        raise StressFailure(f"guest {label}: evdev read timed out") from error
    if process.returncode != 0 or len(stdout) != 24 or stderr:
        raise StressFailure(f"guest {label}: direct input did not reach its evdev device")
    return len(stdout)


def _guest_ssh_command(arguments: argparse.Namespace, *remote: str) -> list[str]:
    """Build the bounded lab SSH command used for guest physical evidence."""
    command = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
               "-o", "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR"]
    if arguments.guest_display_key:
        command.extend(["-i", arguments.guest_display_key])
    command.extend([f"{arguments.guest_display_user}@{arguments.guest_display_host}", *remote])
    return command


def _guest_wayland_geometry(arguments: argparse.Namespace, expected: tuple[int, int], name: str) -> dict[str, Any] | None:
    """Prove the *guest compositor*, not merely QEMU's scanout, resized.

    Browser video dimensions can change while a desktop keeps drawing its old
    logical mode inside the new framebuffer.  Kubuntu's KScreen exposes the
    active Wayland output geometry; query the active local seat (including the
    SDDM Wayland greeter after a reboot) through the existing disposable-guest
    SSH account and require it to equal the settled browser viewport.
    """
    if not arguments.guest_display_host:
        return None
    sessions = subprocess.run(
        _guest_ssh_command(arguments, "sudo", "-n", "loginctl", "list-sessions", "--no-legend", "--no-pager"),
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", timeout=20, check=False,
    )
    if sessions.returncode != 0:
        raise StressFailure(f"{name}: could not enumerate guest graphical sessions")
    selected: tuple[str, str, str] | None = None
    for line in sessions.stdout.splitlines():
        session_id = line.split(maxsplit=1)[0] if line.split() else ""
        if not session_id.isdecimal():
            continue
        details = subprocess.run(
            _guest_ssh_command(arguments, "sudo", "-n", "loginctl", "show-session", session_id,
                               "-p", "User", "-p", "Name", "-p", "Remote", "-p", "Type", "-p", "State"),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", timeout=20, check=False,
        )
        if details.returncode != 0:
            continue
        properties = dict(line.split("=", 1) for line in details.stdout.splitlines() if "=" in line)
        uid, user = properties.get("User", ""), properties.get("Name", "")
        if (properties.get("Remote") == "no" and properties.get("Type") == "wayland" and
                properties.get("State") == "active" and uid.isdecimal() and
                re.fullmatch(r"[A-Za-z0-9._-]+", user or "") and
                user == arguments.guest_desktop_user):
            selected = session_id, uid, user
            break
    if selected is None:
        raise StressFailure(
            f"{name}: guest has no active local Wayland session for {arguments.guest_desktop_user!r}")
    session_id, uid, user = selected

    def read_geometry() -> tuple[int, int] | None:
        for socket_name in ("wayland-0", "wayland-1", "wayland-2", "wayland-3"):
            output = subprocess.run(
                _guest_ssh_command(
                    arguments, "sudo", "-n", "runuser", "-u", user, "--", "env",
                    f"XDG_RUNTIME_DIR=/run/user/{uid}", f"WAYLAND_DISPLAY={socket_name}",
                    f"DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{uid}/bus", "kscreen-doctor", "--json",
                ),
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", timeout=20, check=False,
            )
            if output.returncode != 0:
                continue
            try:
                kscreen = json.loads(output.stdout)
            except json.JSONDecodeError:
                continue
            outputs = kscreen.get("outputs") if isinstance(kscreen, dict) else None
            if not isinstance(outputs, list):
                continue
            for display in outputs:
                if not isinstance(display, dict) or display.get("enabled") is not True:
                    continue
                size = display.get("size")
                if isinstance(size, dict) and isinstance(size.get("width"), int) and isinstance(size.get("height"), int):
                    return size["width"], size["height"]
        return None

    # A successful decoder-size check alone catches only a transient frame.
    # Keep observing KWin for two seconds after it has selected the requested
    # mode: a second stale Console must not be able to restore its old window
    # dimensions immediately afterwards.
    deadline = time.monotonic() + 12.0
    stable_since: float | None = None
    latest: tuple[int, int] | None = None
    while time.monotonic() < deadline:
        latest = read_geometry()
        if latest == expected:
            if stable_since is None:
                stable_since = time.monotonic()
            elif time.monotonic() - stable_since >= 2.0:
                return {"session": session_id, "user": user, "geometry": latest}
        else:
            stable_since = None
        time.sleep(0.25)
    if latest is None:
        raise StressFailure(f"{name}: KScreen could not query the active guest Wayland output")
    raise StressFailure(f"{name}: guest Wayland output settled at {latest[0]}x{latest[1]}, expected {expected[0]}x{expected[1]}")


def _run_case(arguments: argparse.Namespace, name: str, width: int, height: int,
              *, guest_transfer: bool = False,
              viewport_resizes: tuple[tuple[int, int], ...] = ()) -> dict[str, Any]:
    screenshot = (f"/tmp/qsm-browser-e2e-vm{arguments.vmid}-{name}.png"
                  if arguments.visual_evidence else "")
    completed = subprocess.run(
        _command(arguments, width=width, height=height, guest_transfer=guest_transfer,
                 viewport_resizes=viewport_resizes, screenshot=screenshot),
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", timeout=90, check=False,
    )
    if completed.returncode != 0:
        reason = completed.stderr.strip().splitlines()[-1:] or ["browser E2E failed"]
        raise StressFailure(f"{name}: {reason[0][:400]}")
    result = _result(completed.stdout, name)
    _check_live(result, name, width, height)
    if guest_transfer and not isinstance(result.get("guestTransfer"), dict):
        raise StressFailure(f"{name}: guest clipboard/file transfer was not completed")
    if viewport_resizes:
        observed = result.get("viewportResizes")
        expected = [{"width": resize_width, "height": resize_height}
                    for resize_width, resize_height in viewport_resizes]
        if not isinstance(observed, list) or [
                {"width": value.get("width"), "height": value.get("height")}
                for value in observed if isinstance(value, dict)] != expected:
            raise StressFailure(f"{name}: browser viewport/Display1 resize sequence was incomplete")
    if screenshot:
        result["screenshot"] = screenshot
    return result


def _qemu_generation(vmid: int) -> str | None:
    path = Path("/run/qemu-server") / f"{vmid}.pid"
    try:
        pid = int(path.read_text(encoding="ascii").strip())
        stat_fields = (Path("/proc") / str(pid) / "stat").read_text(encoding="ascii").rsplit(") ", 1)[1].split()
        if stat_fields[0] == "Z" or not stat_fields[19].isdecimal():
            return None
    except (OSError, ValueError, IndexError):
        return None
    return f"{pid}:{stat_fields[19]}"


def _wait_generation_change(arguments: argparse.Namespace, previous: str | None) -> str:
    deadline = time.monotonic() + arguments.restart_timeout
    while time.monotonic() < deadline:
        status = subprocess.run(["qm", "status", str(arguments.vmid)], stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True, check=False).stdout.strip()
        generation = _qemu_generation(arguments.vmid)
        if status == "status: running" and generation is not None and generation != previous:
            return generation
        time.sleep(0.25)
    raise StressFailure("VM did not return with a new QEMU generation")


def _run_held_case(arguments: argparse.Namespace, name: str, *, restart_vm: bool) -> dict[str, Any]:
    width, height = arguments.window_size
    process = subprocess.Popen(
        _command(arguments, width=width, height=height, hold_seconds=arguments.hold_seconds,
                 expect_disconnect=True),
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8",
    )
    try:
        # First video + warm-up complete before the disruptive action.  The
        # wait is deliberately longer than the measurement's own warm-up.
        time.sleep(arguments.warmup_seconds + 3.0)
        if process.poll() is not None:
            stdout, stderr = process.communicate(timeout=1)
            raise StressFailure(f"{name}: held Console ended early: {(stderr or stdout)[-400:]}")
        if restart_vm:
            before = _qemu_generation(arguments.vmid)
            reboot = subprocess.run(["qm", "reboot", str(arguments.vmid)], stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True, timeout=30, check=False)
            if reboot.returncode != 0:
                raise StressFailure(f"{name}: qm reboot failed")
            _wait_generation_change(arguments, before)
        else:
            restart = subprocess.run(["systemctl", "restart", "qsm-pve-direct-terminal.service"],
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                     timeout=60, check=False)
            if restart.returncode != 0:
                raise StressFailure(f"{name}: terminal service restart failed")
        stdout, stderr = process.communicate(timeout=arguments.hold_seconds + 35)
    except BaseException:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        raise
    if process.returncode != 0:
        raise StressFailure(f"{name}: held Chrome peer failed: {stderr.strip()[-400:]}")
    result = _result(stdout, name)
    # The measure process actively waits for the browser-visible lifecycle
    # signal: a PeerConnection state transition or the closure of either
    # control channel. The shipped popup closes on that channel event, which
    # arrives before Chromium's unrelated ICE timeout.
    held = result.get("afterHold")
    if (not isinstance(held, dict) or
            (held.get("connectionState") == "connected" and
             held.get("controlReady") is True and held.get("pointerReady") is True)):
        raise StressFailure(f"{name}: old WebRTC Console was not retired")
    return result


def _concurrent_case(arguments: argparse.Namespace) -> None:
    width, height = arguments.window_size
    first = subprocess.Popen(_command(arguments, width=width, height=height, hold_seconds=4),
                             stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, encoding="utf-8")
    # Display1 has one guest scanout.  Two viewers must therefore subscribe
    # to the same geometry; an independently resized second viewer is tested
    # in the sequential fullscreen case below.
    second = subprocess.Popen(_command(arguments, width=width, height=height, hold_seconds=4),
                              stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, encoding="utf-8")
    for name, process, expected in (("concurrent-window", first, (width, height)),
                                    ("concurrent-window-second", second, (width, height))):
        try:
            stdout, stderr = process.communicate(timeout=90)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate()
            raise StressFailure(f"{name}: Chrome E2E timed out")
        if process.returncode != 0:
            raise StressFailure(f"{name}: {stderr.strip()[-400:]}")
        _check_live(_result(stdout, name), name, *expected)


def _competing_resolution_case(arguments: argparse.Namespace) -> dict[str, Any]:
    """The last actual popup resize wins; passive viewers must not fight it.

    A VM exposes one scanout, so separate Console windows cannot retain
    independent guest modes.  This specifically catches the regression where
    a first, fullscreen-sized popup received the new frame and treated that
    decoded-video event as if its own OS window had changed size.
    """
    first = subprocess.Popen(
        _command(arguments, width=arguments.fullscreen_size[0], height=arguments.fullscreen_size[1], hold_seconds=7),
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8",
    )
    try:
        # Let the first peer establish its own display mode before opening
        # the windowed Console that must become authoritative.
        time.sleep(2.0)
        second = subprocess.run(
            _command(arguments, width=arguments.window_size[0], height=arguments.window_size[1], hold_seconds=4),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", timeout=90, check=False,
        )
        if second.returncode != 0:
            raise StressFailure(f"competing-window: {second.stderr.strip()[-400:]}")
        result = _result(second.stdout, "competing-window")
        _check_live(result, "competing-window", *arguments.window_size)
        _check_settled_viewport(result.get("afterHold"), "competing-window", *arguments.window_size)
        guest_display = _guest_wayland_geometry(arguments, arguments.window_size, "competing-window")
        return {"guestDisplay": guest_display, "lastViewer": result.get("afterHold")}
    finally:
        try:
            first.communicate(timeout=90)
        except subprocess.TimeoutExpired:
            first.kill()
            first.communicate()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vmid", type=int, required=True)
    parser.add_argument("--node", default=os.uname().nodename)
    parser.add_argument("--socket", default="/run/qsm-pve-direct-terminal/pve-webrtc.sock")
    parser.add_argument("--browser-host", required=True)
    parser.add_argument("--browser-user", default="qsm-browser")
    parser.add_argument("--browser-key", default="/root/.ssh/qsm-browser-gate")
    parser.add_argument("--browser-script", required=True)
    parser.add_argument("--browser-executable", default="/usr/bin/google-chrome")
    parser.add_argument("--browser-ice-server", default="",
                        help="optional lab-only STUN/TURN URL passed only to the Chrome test peer")
    parser.add_argument("--browser-headful", action="store_true")
    parser.add_argument("--browser-display", default="",
                        help="X display for a visible browser qualification, for example :97")
    parser.add_argument("--visual-evidence", action="store_true",
                        help="save each headful Chrome Console frame on the browser peer for visual review")
    parser.add_argument("--max-edge-luma", type=int,
                        help="optional fixture assertion for each decoded-frame corner (0..255)")
    parser.add_argument("--fps", type=int, default=60)
    parser.add_argument("--warmup-seconds", type=float, default=2.0)
    parser.add_argument("--resize-settle-seconds", type=float, default=1.0,
                        help="minimum seconds between distinct Display1 modes (0..30)")
    parser.add_argument("--hold-seconds", type=float, default=30.0)
    parser.add_argument("--restart-timeout", type=float, default=90.0)
    parser.add_argument("--guest-file-bytes", type=int, default=65536)
    parser.add_argument("--guest-input-host", default="",
                        help="optional disposable-guest SSH host for physical evdev input proof")
    parser.add_argument("--guest-input-user", default="root")
    parser.add_argument("--guest-input-key", default="")
    parser.add_argument("--guest-tablet-device", default="/dev/input/event2")
    parser.add_argument("--guest-keyboard-device", default="/dev/input/event1")
    parser.add_argument("--guest-display-host", default="",
                        help="optional guest SSH host for KScreen proof of the settled Wayland geometry")
    parser.add_argument("--guest-display-user", default="root")
    parser.add_argument("--guest-display-key", default="")
    parser.add_argument("--guest-desktop-user", default="",
                        help="required graphical Wayland user to prove; do not use the display-manager greeter")
    parser.add_argument("--window-size", default="1280x798")
    parser.add_argument("--fullscreen-size", default="1920x1080")
    arguments = parser.parse_args()
    try:
        def geometry(value: str) -> tuple[int, int]:
            width, separator, height = value.partition("x")
            if separator != "x" or not width.isdecimal() or not height.isdecimal():
                raise StressFailure("geometry must be WIDTHxHEIGHT")
            result = int(width), int(height)
            if not (64 <= result[0] <= 16384 and 64 <= result[1] <= 16384 and
                    result[0] % 2 == 0 and result[1] % 2 == 0):
                raise StressFailure("geometry must be even and within 64..16384")
            return result
        arguments.window_size = geometry(arguments.window_size)
        arguments.fullscreen_size = geometry(arguments.fullscreen_size)
        if not 0 <= arguments.warmup_seconds <= 30 or not 0 <= arguments.resize_settle_seconds <= 30 or \
                not 10 <= arguments.hold_seconds <= 120:
            raise StressFailure("warm-up/resize-settle/hold bounds are invalid")
        if not 0 <= arguments.guest_file_bytes <= 2 * 1024 * 1024:
            raise StressFailure("guest file size is invalid")
        if arguments.max_edge_luma is not None and not 0 <= arguments.max_edge_luma <= 255:
            raise StressFailure("max edge luma is invalid")
        if arguments.visual_evidence and not arguments.browser_headful:
            raise StressFailure("visual evidence needs --browser-headful")
        if arguments.browser_headful and not re.fullmatch(r":[0-9]{1,4}", arguments.browser_display):
            raise StressFailure("headful browser qualification needs --browser-display :N")
        if bool(arguments.guest_input_host) and (not arguments.guest_input_user or
                                                 not EVENT_DEVICE.fullmatch(arguments.guest_tablet_device) or
                                                 not EVENT_DEVICE.fullmatch(arguments.guest_keyboard_device)):
            raise StressFailure("guest input proof needs user and valid tablet/keyboard event devices")
        if arguments.guest_display_host and (
                not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,31}", arguments.guest_desktop_user)):
            raise StressFailure(
                "guest display proof needs --guest-desktop-user for a real graphical desktop, not a greeter")
        if os.geteuid() != 0:
            raise StressFailure("run this PVE acceptance suite as root")
        if not MEASURE.is_file():
            raise StressFailure("measure-direct-browser-e2e.py is unavailable")
        if not Path(arguments.socket).is_socket():
            raise StressFailure("QSM Direct terminal socket is unavailable")
        if _qemu_generation(arguments.vmid) is None:
            raise StressFailure("target VM is not running")

        evidence: dict[str, Any] = {}
        display_evidence: dict[str, dict[str, Any]] = {}
        tablet_capture = _guest_capture(arguments, arguments.guest_tablet_device)
        keyboard_capture = _guest_capture(arguments, arguments.guest_keyboard_device)
        try:
            evidence["window"] = _run_case(
                arguments, "window", *arguments.window_size, guest_transfer=True,
                viewport_resizes=(arguments.fullscreen_size, arguments.window_size),
            )
        except BaseException:
            # A failed browser case must not leave a 45-second SSH reader
            # behind in a continuous lab run.  On success the readers finish
            # naturally after the pointer and key exercise and are asserted
            # below.
            if tablet_capture is not None and tablet_capture.poll() is None:
                tablet_capture.terminate()
            if keyboard_capture is not None and keyboard_capture.poll() is None:
                keyboard_capture.terminate()
            raise
        input_evidence: dict[str, int] | None = None
        if tablet_capture is not None and keyboard_capture is not None:
            input_evidence = {
                "tabletEventBytes": _await_guest_capture(tablet_capture, "tablet"),
                "keyboardEventBytes": _await_guest_capture(keyboard_capture, "keyboard"),
            }
        window_geometry = _guest_wayland_geometry(arguments, arguments.window_size, "window")
        if window_geometry is not None:
            display_evidence["window"] = window_geometry
        evidence["fullscreen"] = _run_case(
            arguments, "fullscreen", *arguments.fullscreen_size,
            viewport_resizes=(arguments.window_size, arguments.fullscreen_size),
        )
        fullscreen_geometry = _guest_wayland_geometry(arguments, arguments.fullscreen_size, "fullscreen")
        if fullscreen_geometry is not None:
            display_evidence["fullscreen"] = fullscreen_geometry
        _concurrent_case(arguments)
        evidence["competingWindow"] = _competing_resolution_case(arguments)
        evidence["vmRestart"] = _run_held_case(arguments, "vm-restart", restart_vm=True)
        evidence["afterVmRestart"] = _run_case(arguments, "after-vm-restart", *arguments.window_size)
        evidence["serviceRestart"] = _run_held_case(arguments, "service-restart", restart_vm=False)
        evidence["afterServiceRestart"] = _run_case(arguments, "after-service-restart", *arguments.fullscreen_size)
        summary = {
            "vmid": arguments.vmid,
            "cases": tuple(evidence),
            "window": arguments.window_size,
            "fullscreen": arguments.fullscreen_size,
            "guestTransfer": evidence["window"].get("guestTransfer"),
            "guestInput": input_evidence,
            "guestDisplay": display_evidence or None,
            "competingWindow": evidence["competingWindow"],
        }
    except (OSError, StressFailure, subprocess.SubprocessError, subprocess.TimeoutExpired) as error:
        print(f"QSM_DIRECT_STRESS_SUITE_FAILED: {error}", file=sys.stderr)
        return 1
    print("QSM_DIRECT_STRESS_SUITE_OK " + json.dumps(summary, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
