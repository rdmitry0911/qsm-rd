"""Security boundaries of the opt-in single-port transport."""
import base64
import hashlib
import hmac
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from extensions.signal.qsm_gateway import read_gateway, ice_credentials, IcePeerLease
from extensions.signal.qsm_direct_signal import SignalError, handle_request, SignalConfig
spec = importlib.util.spec_from_file_location('gateway_tool', ROOT / 'extensions/gateway/qsm_direct_gateway.py')
tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tool)


class GatewayTests(unittest.TestCase):
    def test_credentials_are_expiring_opaque_and_coturn_compatible(self):
        value = ice_credentials({'secret': 'a' * 64}, now=1234)
        self.assertEqual(value['username'].split(':')[0], '1834')
        expected = base64.b64encode(hmac.new(b'a' * 64, value['username'].encode(), hashlib.sha1).digest()).decode()
        self.assertEqual(value['credential'], expected)
        self.assertEqual(value['ttl'], 600)
        self.assertNotEqual(value['username'], ice_credentials({'secret': 'a' * 64}, now=1234)['username'])

    def test_optional_udp_keeps_tcp_and_uses_the_same_scoped_authentication(self):
        config = tool.udp_turn_config('192.0.2.5', 'a' * 64, 8006)
        self.assertIn('listening-ip=192.0.2.5', config)
        self.assertIn('listening-port=8006', config)
        self.assertIn('no-tcp\n', config); self.assertNotIn('no-udp\n', config)
        self.assertNotIn('tcp-proxy-port=', config)
        self.assertIn('allowed-peer-ip=192.0.2.5', config)
        self.assertIn('no-cli', config)
        rules = tool.firewall(1234)
        self.assertIn('udp sport @udp_ports ct state established', rules)
        self.assertLess(rules.index('udp sport @udp_ports'), rules.index('udp counter drop'))
        self.assertNotIn('hook input', rules)
        old = ice_credentials({'secret': 'a' * 64}, now=1234)
        new = ice_credentials({'secret': 'a' * 64, 'udp_port': 8006}, now=1234)
        self.assertNotIn('udp_port', old); self.assertEqual(new['udp_port'], 8006)
        for port in (True, 0, 443, 65536, '8006'):
            with self.assertRaises(ValueError): tool.udp_turn_config('192.0.2.5', 'a' * 64, port)

    def test_udp_configuration_publishes_only_after_service_start_and_keeps_tcp_running(self):
        with tempfile.TemporaryDirectory() as directory:
            etc, state = Path(directory) / 'etc', Path(directory) / 'state'
            etc.mkdir(); state.mkdir(); (state / 'before.json').write_text('{}')
            initial = {'secret': 'a' * 64, 'relay_ipv4': '192.0.2.5'}
            (etc / 'gateway.json').write_text(json.dumps(initial))
            calls = []
            def run(*args, **kwargs):
                calls.append(args)
                if args == ('systemctl', 'enable', '--now', tool.UDP_UNIT):
                    self.assertNotIn('udp_port', json.loads((etc / 'gateway.json').read_text()),
                                     'do not advertise an unstarted listener')
            with patch.object(tool, 'ETC', etc), patch.object(tool, 'STATE', state), \
                    patch.object(tool, 'ensure_udp_set'), patch.object(tool, 'run', side_effect=run), \
                    patch.object(tool.socket, 'socket'), patch.object(tool.os, 'chown'), \
                    patch.object(tool.pwd, 'getpwnam', return_value=SimpleNamespace(pw_gid=1234)):
                tool.configure_udp(8006)
                self.assertEqual(json.loads((etc / 'gateway.json').read_text())['udp_port'], 8006)
                self.assertIn('no-tcp', (state / 'turn-udp.conf').read_text())
                tool.configure_udp(None)
            self.assertEqual(json.loads((etc / 'gateway.json').read_text()), initial)
            self.assertFalse((state / 'turn-udp.conf').exists())
            self.assertTrue(all('pveproxy' not in args and 'flush' not in args for args in calls))
            self.assertTrue(all(tool.UDP_UNIT in args for args in calls if args[0] == 'systemctl'))

    def test_failed_udp_start_does_not_advertise_a_broken_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            etc, state = Path(directory) / 'etc', Path(directory) / 'state'
            etc.mkdir(); state.mkdir(); (state / 'before.json').write_text('{}')
            initial = {'secret': 'a' * 64, 'relay_ipv4': '192.0.2.5'}
            (etc / 'gateway.json').write_text(json.dumps(initial))
            def run(*args, **kwargs):
                if args == ('systemctl', 'enable', '--now', tool.UDP_UNIT):
                    raise RuntimeError('owned test startup failure')
            with patch.object(tool, 'ETC', etc), patch.object(tool, 'STATE', state), \
                    patch.object(tool, 'ensure_udp_set'), patch.object(tool, 'run', side_effect=run), \
                    patch.object(tool.socket, 'socket'), patch.object(tool.os, 'chown'), \
                    patch.object(tool.pwd, 'getpwnam', return_value=SimpleNamespace(pw_gid=1234)):
                with self.assertRaises(RuntimeError): tool.configure_udp(8006)
            self.assertEqual(json.loads((etc / 'gateway.json').read_text()), initial)
            self.assertFalse((state / 'turn-udp.conf').exists())

    def test_bootstrap_requires_vm_console_even_when_no_gateway_config_exists(self):
        auth = SimpleNamespace(authorize=lambda *args: (_ for _ in ()).throw(SignalError(401)))
        config = SignalConfig(auth, None, None, 'node')
        with patch('extensions.signal.qsm_direct_signal.read_gateway') as read:
            with self.assertRaises(SignalError) as raised:
                handle_request(config, 'GET', '/nodes/node/lxc/105/qsm-direct-ice', {'user': 'a@pve', 'ticket': 'bad'})
            self.assertEqual(raised.exception.status, 401)
            read.assert_not_called()
        calls = []
        config.authority = SimpleNamespace(authorize=lambda *args: calls.append(args))
        with patch('extensions.signal.qsm_direct_signal.read_gateway', return_value={'secret': 'a' * 64}):
            result = handle_request(config, 'GET', '/nodes/node/qemu/103/qsm-direct-ice', {'user': 'a@pve', 'ticket': 'ticket'})
        self.assertEqual(calls, [('a@pve', 'ticket', 103, 'VM.Console')])
        self.assertNotIn('secret', result['data'])

    def test_existing_proxy_source_acl_is_never_silently_bypassed(self):
        for name in ('ALLOW_FROM', 'DENY_FROM', 'POLICY', 'LISTEN_IP', 'PROXY_REAL_IP_HEADER', 'PROXY_REAL_IP_ALLOW_FROM'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                tool.proxy_config(f'export {name}="test"\n'.encode())
        self.assertIn(b'LISTEN_IP="127.0.0.1"', tool.proxy_config(b'# ALLOW_FROM="test"\n'))

    def test_firewall_only_restricts_dedicated_turn_uid_output(self):
        rules = tool.firewall(1234)
        self.assertIn('meta skuid 1234', rules)
        self.assertIn('udp dport @peers counter accept', rules)
        self.assertIn('udp counter drop', rules)
        self.assertIn('timeout 15s', rules)
        self.assertNotIn('flush', rules)
        self.assertNotIn('hook input', rules)
        self.assertNotIn('hook forward', rules)

    @unittest.skipUnless(os.geteuid() == 0, 'root-owned config fixture')
    def test_gateway_config_rejects_public_permissions_and_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'gateway.json'
            path.write_text('{"secret":"' + 'a' * 64 + '","relay_ipv4":"192.0.2.5"}')
            path.chmod(0o600)
            self.assertEqual(read_gateway(path)['relay_ipv4'], '192.0.2.5')
            path.chmod(0o640)
            with self.assertRaises(ValueError):
                read_gateway(path)
            alias = path.with_name('alias')
            alias.symlink_to(path)
            with self.assertRaises(OSError):
                read_gateway(alias)


class LeaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_owned_numeric_socket_tuple_is_registered_refreshed_and_removed(self):
        writes = []
        process = SimpleNamespace(returncode=0, communicate=AsyncMock(side_effect=lambda value: writes.append(value)))
        with patch('extensions.signal.qsm_gateway.asyncio.create_subprocess_exec', new=AsyncMock(return_value=process)):
            lease = IcePeerLease('192.0.2.5', [45000, 45000])
            await lease._change()
            await lease._change()
            await lease.stop()
        self.assertEqual(writes[0].count(b'add element'), 1)
        self.assertIn(b'192.0.2.5 . 45000 timeout 15s', writes[0])
        self.assertTrue(writes[1].startswith(b'delete element'))
        self.assertIn(b'add element', writes[1])
        self.assertNotIn(b'add element', writes[2])

    async def test_missing_firewall_never_grants_a_lease(self):
        process = SimpleNamespace(returncode=1, communicate=AsyncMock())
        with patch('extensions.signal.qsm_gateway.asyncio.create_subprocess_exec', new=AsyncMock(return_value=process)):
            lease = IcePeerLease('192.0.2.5', [45000])
            with self.assertRaises(RuntimeError):
                await lease.start(AsyncMock())
            self.assertFalse(lease.registered)
            self.assertIsNone(lease.task)

if __name__ == '__main__':
    unittest.main()
