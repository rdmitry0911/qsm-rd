"""Listener selection, trusted browser configuration and update rollback."""
import errno
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'integration/proxmox/pve9/direct_ui'))
from extensions.signal.qsm_direct_endpoint import SignalEndpoint, bind_socket, read_endpoint
from extensions.signal.qsm_direct_endpoint_cli import apply_endpoint, free_endpoint
from qsm_pve_direct_ui import render_patched_template, ANCHOR


class EndpointTests(unittest.TestCase):
    def test_default_and_configured_ipv6_and_public_origin(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'signal.json'
            self.assertEqual(read_endpoint(path).port, 8007)
            path.write_text(json.dumps({'listen': '::', 'port': 18019,
                                        'public_url': 'https://[2001:db8::1]:28019/'}))
            path.chmod(0o600)
            endpoint = read_endpoint(path, require_root_owner=False)
            self.assertEqual(endpoint.listen, '::')
            self.assertEqual(endpoint.browser_config(),
                             {'port': 18019, 'public_url': 'https://[2001:db8::1]:28019'})

    def test_invalid_and_untrusted_configuration_is_rejected(self):
        for kwargs in ({'port': True}, {'port': 0}, {'port': 65536}, {'port': '18007'},
                       {'listen': '0.0.0.0; command'}, {'public_url': 'http://host:18007'},
                       {'public_url': 'https://user:secret@host:18007'},
                       {'public_url': 'https://host:18007/path'},
                       {'public_url': 'https://host:18007/?x=1'},
                       {'public_url': 'https://host:18007/#x'},
                       {'public_url': 'https://host:bad'}, {'public_url': 'https://host:0'}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                SignalEndpoint(**kwargs)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'signal.json'
            for value in ('{"port":18007,"port":18009}', '{"unknown":true}', '[]'):
                path.write_text(value)
                path.chmod(0o600)
                with self.assertRaises(ValueError):
                    read_endpoint(path, require_root_owner=False)
            path.write_text('{}')
            path.chmod(0o666)
            with self.assertRaises(ValueError):
                read_endpoint(path, require_root_owner=False)
            path.chmod(0o600)
            alias = Path(directory) / 'alias'
            alias.symlink_to(path)
            with self.assertRaises(OSError):
                read_endpoint(alias, require_root_owner=False)

    def test_free_port_search_keeps_occupied_listener_alive(self):
        with socket.socket() as occupied:
            occupied.bind(('127.0.0.1', 0))
            occupied.listen()
            port = occupied.getsockname()[1]
            if port > 65430:
                self.skipTest('ephemeral port too close to end of search range')
            selected = free_endpoint('127.0.0.1', port)
            self.assertGreater(selected.port, port)
            with bind_socket(selected):
                pass
            with socket.create_connection(('127.0.0.1', port), timeout=1):
                pass

    def test_busy_endpoint_does_not_change_config_or_call_services(self):
        with tempfile.TemporaryDirectory() as directory, socket.socket() as occupied:
            occupied.bind(('127.0.0.1', 0))
            occupied.listen()
            path = Path(directory) / 'signal.json'
            previous = b'{"listen":"127.0.0.1","port":18019}\n'
            path.write_bytes(previous)
            with patch('extensions.signal.qsm_direct_endpoint_cli.read_endpoint',
                       return_value=SignalEndpoint('127.0.0.1', 18019)), \
                    patch('extensions.signal.qsm_direct_endpoint_cli.subprocess.run') as run:
                with self.assertRaises(OSError) as raised:
                    apply_endpoint(SignalEndpoint('127.0.0.1', occupied.getsockname()[1]), path=path)
                self.assertEqual(raised.exception.errno, errno.EADDRINUSE)
                self.assertEqual(path.read_bytes(), previous)
                self.assertFalse(any('/usr/sbin/qsm-pve-direct-ui' in str(call) for call in run.call_args_list))

    def test_failed_ui_update_restores_previous_config(self):
        with tempfile.TemporaryDirectory() as directory, socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]
            probe.close()
            path = Path(directory) / 'signal.json'
            previous = b'{"listen":"127.0.0.1","port":18019}\n'
            path.write_bytes(previous)
            def run(command, **kwargs):
                if kwargs.get('check'):
                    raise subprocess.CalledProcessError(1, command)
                return subprocess.CompletedProcess(command, 1)
            with patch('extensions.signal.qsm_direct_endpoint_cli.read_endpoint',
                       return_value=SignalEndpoint('127.0.0.1', 18019)), \
                    patch('extensions.signal.qsm_direct_endpoint_cli.subprocess.run', side_effect=run):
                with self.assertRaises(subprocess.CalledProcessError):
                    apply_endpoint(SignalEndpoint('127.0.0.1', port), path=path)
            self.assertEqual(path.read_bytes(), previous)
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)

    def test_template_publishes_only_browser_metadata_before_client(self):
        endpoint = SignalEndpoint('0.0.0.0', 18019, 'https://pve.example.org:28019')
        rendered = render_patched_template(ANCHOR, 'a' * 64, endpoint.browser_config())
        self.assertLess(rendered.index(b'window.QSM_DIRECT_ENDPOINT'),
                        rendered.index(b'src="/pve2/js/qsm-pve-direct-console.js'))
        self.assertIn(b'"port":18019', rendered)
        self.assertIn(b'https://pve.example.org:28019', rendered)
        self.assertNotIn(b'0.0.0.0', rendered)


if __name__ == '__main__':
    unittest.main(verbosity=2)
