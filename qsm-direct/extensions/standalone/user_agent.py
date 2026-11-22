#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""User-owned Display1 discovery and media. There is no network listener here."""
import argparse
import asyncio
import os
from pathlib import Path
import signal
import socket
import tempfile

from .discovery import discover, display_owner
from . import rpc
from .server import ConsoleServer, ConsoleSession
from extensions.browser_bridge.qsm_browser_bridge import BridgeError


class AgentError(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message


class UserAgent:
    def __init__(self, args, runtime):
        self.args = args
        self.runtime = runtime
        self.transports = {}
        self.owners = {}

    async def dispatch(self, request):
        method = request.get("method")
        login = request.get("login")
        if not isinstance(login, str) or len(login) != 32:
            raise AgentError(400, "Invalid user session")
        if method == "logout":
            for guest_id, owner in list(self.owners.items()):
                if owner == login:
                    transport = self.transports.get(guest_id)
                    if transport is not None:
                        await transport.close(None)
                    self.owners.pop(guest_id, None)
            return None
        guests = await asyncio.to_thread(discover)
        if method == "guests":
            return {"guests": [guest.public() for guest in guests]}
        guest = next((guest for guest in guests if guest.id == request.get("guest")), None)
        if guest is None:
            raise AgentError(404, "Guest is not running under this account")
        if method == "status":
            return {"status": "running", "name": guest.name, "fps": self.args.fps}
        if method == "disconnect":
            transport = self.transports.get(guest.id)
            if (transport is not None and transport.session is not None and
                    self.owners.get(guest.id) == login and transport.session.id == request.get("session")):
                await transport.session.close()
                self.owners.pop(guest.id, None)
            return None
        if method != "offer" or not isinstance(request.get("offer"), dict):
            raise AgentError(400, "Invalid console request")
        transport = self.transports.get(guest.id)
        if transport is not None and transport.session is not None:
            raise AgentError(409, "This guest console is in use. Close its other browser tab first.")
        try:
            destination = await asyncio.to_thread(display_owner, guest)
        except Exception:
            raise AgentError(503, "The guest's QSM display is unavailable or has changed") from None
        config = argparse.Namespace(**vars(self.args), dbus_address=guest.address,
                                    dbus_destination=destination, name=guest.name)
        if transport is None:
            directory = self.runtime / guest.id
            directory.mkdir(mode=0o700)
            transport = ConsoleServer(config, directory, "", frozenset())
            self.transports[guest.id] = transport
        else:
            transport.args = config
            transport.closing = False
        # Only the inherited control socket can select this already validated
        # guest. Both this bridge and its encoder run with the guest owner's UID.
        session = ConsoleSession(transport)
        transport.session = session
        self.owners[guest.id] = login
        try:
            answer = await session.start(request["offer"])
            return {**answer, "session": session.id}
        except BridgeError as error:
            await session.close()
            raise AgentError(400, str(error)) from None
        except asyncio.CancelledError:
            await session.close()
            raise
        except Exception:
            await session.close()
            raise AgentError(502, "Could not attach to the guest's QSM display") from None

    async def close(self):
        await asyncio.gather(*(transport.close(None) for transport in self.transports.values()))


async def run(args):
    control = socket.socket(fileno=args.fd)
    control.setblocking(False)
    reader, writer = await asyncio.open_connection(sock=control)
    task = asyncio.current_task()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, task.cancel)
    with tempfile.TemporaryDirectory(prefix="qsm-user-") as directory:
        agent = UserAgent(args, Path(directory))
        try:
            await rpc.send(writer, {"uid": os.geteuid(), "gid": os.getegid()})
            while True:
                request = await rpc.receive(reader)
                try:
                    result = await agent.dispatch(request)
                    reply = {"data": result}
                except AgentError as error:
                    reply = {"status": error.status, "error": error.message}
                await rpc.send(writer, reply)
        except (asyncio.IncompleteReadError, ConnectionError, asyncio.CancelledError):
            pass
        finally:
            await agent.close()
            writer.close()
            await writer.wait_closed()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fd", type=int, required=True)
    parser.add_argument("--worker", type=Path, required=True)
    parser.add_argument("--fps", type=int, required=True)
    parser.add_argument("--bitrate", type=int, required=True)
    parser.add_argument("--encoder", required=True)
    parser.add_argument("--vaapi-device", type=Path)
    parser.add_argument("--capture-render-node", type=Path)
    asyncio.run(run(parser.parse_args()))
