"""Security boundaries of the opt-in single-port transport."""
import base64
import hashlib
import hmac
import importlib.util
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
