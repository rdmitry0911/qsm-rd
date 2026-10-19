"""Configure the standalone QSM listener and its browser address together."""
from __future__ import annotations

import argparse
import errno
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from .qsm_direct_endpoint import CONFIG_PATH, SignalEndpoint, bind_socket, read_endpoint


def free_endpoint(listen: str = '0.0.0.0', first_port: int = 18007) -> SignalEndpoint:
    for port in range(first_port, min(first_port + 100, 65536)):
        endpoint = SignalEndpoint(listen, port)
        try:
            with bind_socket(endpoint):
                return endpoint
        except OSError as error:
            if error.errno != errno.EADDRINUSE:
                raise
    raise ValueError('no free QSM signalling port in the configured search range')


def write_config(path: Path, content: bytes) -> None:
    fd, name = tempfile.mkstemp(prefix='.signal-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as target:
            os.fchmod(target.fileno(), 0o600)
            target.write(content)
            target.flush()
            os.fsync(target.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def apply_endpoint(endpoint: SignalEndpoint, *, path: Path = CONFIG_PATH,
                   restart: bool = True) -> None:
    previous = read_endpoint(path)
    old = path.read_bytes() if path.exists() else None
    systemd = Path('/run/systemd/system').is_dir()
    active = systemd and subprocess.run(
        ['systemctl', 'is-active', '--quiet', 'qsm-pve-direct-signal.service'],
        check=False).returncode == 0
    if not (active and (endpoint.listen, endpoint.port) == (previous.listen, previous.port)):
        with bind_socket(endpoint):
            pass
    data = (json.dumps(endpoint.as_dict(), indent=2) + '\n').encode('ascii')
    write_config(path, data)
    try:
        subprocess.run(['/usr/sbin/qsm-pve-direct-ui', 'reconcile'], check=True)
        if restart and systemd:
            subprocess.run(['systemctl', 'restart', 'qsm-pve-direct-signal.service'], check=True)
    except BaseException:
        if old is None:
            path.unlink(missing_ok=True)
        else:
            write_config(path, old)
        subprocess.run(['/usr/sbin/qsm-pve-direct-ui', 'reconcile'], check=False)
        if restart and systemd:
            subprocess.run(['systemctl', 'restart' if active else 'stop',
                            'qsm-pve-direct-signal.service'], check=False)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--listen', help='local bind IP, for example 0.0.0.0, :: or 192.0.2.10')
    parser.add_argument('--port', type=int, help='local HTTPS port (1..65535)')
    parser.add_argument('--public-url', help='browser HTTPS origin, or auto to use the PVE page hostname')
    parser.add_argument('--show', action='store_true', help='show the saved endpoint')
    parser.add_argument('--initialize', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--no-restart', action='store_true', help='update config/UI without starting or restarting signalling')
    args = parser.parse_args(argv)
    if args.show and any((args.listen is not None, args.port is not None,
                          args.public_url is not None, args.initialize)):
        parser.error('--show cannot be combined with configuration changes')
    if not args.show and os.geteuid() != 0:
        parser.error('run as root')
    try:
        if args.show:
            endpoint = read_endpoint()
        else:
            CONFIG_PATH.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with (CONFIG_PATH.parent / '.endpoint.lock').open('a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                if args.initialize and CONFIG_PATH.exists():
                    endpoint = read_endpoint()
                    subprocess.run(['/usr/sbin/qsm-pve-direct-ui', 'reconcile'], check=True)
                else:
                    if args.initialize:
                        if any((args.listen is not None, args.port is not None, args.public_url is not None)):
                            parser.error('--initialize cannot be combined with endpoint overrides')
                        endpoint = free_endpoint()
                    else:
                        if args.listen is None and args.port is None and args.public_url is None:
                            parser.error('specify --listen, --port or --public-url, or use --show')
                        saved = read_endpoint()
                        endpoint = SignalEndpoint(
                            args.listen if args.listen is not None else saved.listen,
                            args.port if args.port is not None else saved.port,
                            saved.public_url if args.public_url is None else
                            ('' if args.public_url == 'auto' else args.public_url))
                    apply_endpoint(endpoint, restart=not (args.initialize or args.no_restart))
        print(json.dumps(endpoint.as_dict(), indent=2))
        return 0
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        message = ('could not apply endpoint; previous configuration restored'
                   if isinstance(error, subprocess.CalledProcessError) else str(error))
        print('qsm-pve-direct-endpoint: ' + message, file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
