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


class RouteTests(unittest.TestCase):
    def test_route_description_is_validated(self):
        self.assertEqual(tool.parse_route({'id': 'qdev-host', 'port': 8017}), ('qdev-host', 'qdev-host', 8017))
        self.assertEqual(tool.parse_route({'id': 'tool', 'prefix': '/other/', 'port': 9000}), ('tool', 'other', 9000))
        for bad in ({'id': 'Bad', 'port': 8017}, {'id': 'x', 'prefix': '/a/b/', 'port': 8017},
                    {'id': 'x', 'prefix': '/../', 'port': 8017}, {'id': 'api2', 'port': 8017},
                    {'id': 'x', 'prefix': '/qsm-direct/', 'port': 8017}, {'id': 'x', 'port': 18007},
                    {'id': 'x', 'port': 8006}, {'id': 'x', 'port': 80}, {'id': 'x', 'port': '8017'},
                    {'id': 'x', 'port': True}, {'id': 'x'}, {'id': 'x', 'port': 8017, 'port_from': 'file'}):
            with self.subTest(route=bad), self.assertRaises(ValueError):
                tool.parse_route(bad)

    def test_port_comes_from_the_services_own_configuration(self):
        route = {'id': 'qdev-host', 'port': 8017, 'port_from': {'file': '/etc/qdev-host/ui.json', 'key': 'port'}}
        self.assertEqual(tool.parse_route(route, lambda path: {'port': 9443})[2], 9443)
        self.assertEqual(tool.parse_route(route, lambda path: {'users': []})[2], 8017)
        def missing(path):
            raise FileNotFoundError(path)
        self.assertEqual(tool.parse_route(route, missing)[2], 8017)
        with self.assertRaises(ValueError):
            tool.parse_route(route, lambda path: {'port': 8006})

    def test_routes_skip_bad_files_and_let_the_administrator_override(self):
        with tempfile.TemporaryDirectory() as directory:
            shipped, local = Path(directory) / 'shipped', Path(directory) / 'local'
            shipped.mkdir(); local.mkdir()
            (shipped / 'qdev-host.json').write_text('{"id": "qdev-host", "port": 8017}')
            (shipped / 'broken.json').write_text('{')
            (shipped / 'clash.json').write_text('{"id": "zz", "prefix": "/qdev-host/", "port": 9001}')
            (shipped / 'notes.txt').write_text('ignored')
            (local / 'qdev-host.json').write_text('{"id": "qdev-host", "port": 9443}')
            warnings = []
            reader = lambda path: json.loads(Path(path).read_text())
            found = tool.routes((shipped, local, Path(directory) / 'absent'), reader, warnings.append)
            self.assertEqual(found, [('qdev-host', 'qdev-host', 9443)])
            self.assertEqual(len(warnings), 2)
            self.assertTrue(any('broken.json' in w for w in warnings))
            self.assertTrue(any('already used by qdev-host' in w for w in warnings))

    def test_haproxy_routes_go_before_pve_and_strip_their_prefix(self):
        plain = tool.haproxy_config(18007, '192.0.2.5')
        self.assertNotIn('route_', plain)
        config = tool.haproxy_config(18007, '192.0.2.5', [('qdev-host', 'qdev-host', 8017)])
        self.assertLess(config.index('use_backend route_0 if route_0'), config.index('default_backend pve'))
        self.assertGreater(config.index('use_backend route_0'), config.index('use_backend signal if qsm'))
        self.assertIn('acl route_0 path_beg /qdev-host/\n', config)
        self.assertIn('http-request replace-path ^/qdev-host/(.*)$ /\\1\n', config)
        self.assertIn(f'server local 127.0.0.1:8017 ssl verify required ca-file {tool.RUN}/ca.pem', config)
        # Nothing else changes: the routes add their two ACL lines and their backend.
        acl = config[config.index('    acl route_0'):config.index('    default_backend pve')]
        self.assertEqual(config.replace(acl, ''), plain + config[config.index('\nbackend route_0') + 1:])

    def test_route_edit_keeps_what_the_administrator_added(self):
        # A node of the project carried extra bind lines for its other addresses;
        # a route change must not drop them (2026-11-08, 10.30.0.240).
        routes = [('qdev-host', 'qdev-host', 8017)]
        plain = tool.haproxy_config(18007, '192.0.2.5')
        self.assertEqual(tool.with_routes(plain, routes), tool.haproxy_config(18007, '192.0.2.5', routes))
        self.assertEqual(tool.with_routes(plain, []), plain)
        custom = plain.replace('    bind [::]:8006 v6only\n',
                               '    bind [::]:8006 v6only\n    # other node addresses\n    bind 198.51.100.7:8006\n')
        added = tool.with_routes(custom, routes)
        self.assertIn('    bind 198.51.100.7:8006\n', added)
        self.assertIn('backend route_0\n', added)
        self.assertEqual(tool.with_routes(added, routes), added)
        self.assertEqual(tool.with_routes(added, []), custom)
        two = tool.with_routes(added, [('a-tool', 'a-tool', 9000), ('qdev-host', 'qdev-host', 8017)])
        self.assertEqual(two.count('\nbackend route_'), 2)
        self.assertEqual(tool.with_routes(two, []), custom)
        with self.assertRaises(ValueError):
            tool.with_routes(plain.replace('    default_backend pve\n', ''), routes)

    def test_write_haproxy_checks_the_candidate_and_skips_identical_content(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            calls = []
            with patch.object(tool, 'STATE', state), patch.object(tool, 'run', lambda *args, **kwargs: calls.append(args)):
                self.assertTrue(tool.write_haproxy(b'one'))
                self.assertEqual(calls, [('haproxy', '-c', '-f', str(state / 'haproxy.cfg.new'))])
                self.assertEqual((state / 'haproxy.cfg').read_bytes(), b'one')
                self.assertFalse(tool.write_haproxy(b'one'))
                self.assertEqual(len(calls), 1)
            def refuse(*args, **kwargs):
                raise tool.subprocess.CalledProcessError(1, args)
            with patch.object(tool, 'STATE', state), patch.object(tool, 'run', refuse):
                with self.assertRaises(tool.subprocess.CalledProcessError):
                    tool.write_haproxy(b'two')
            self.assertEqual((state / 'haproxy.cfg').read_bytes(), b'one')
            self.assertFalse((state / 'haproxy.cfg.new').exists())

    def test_refresh_of_a_disabled_gateway_changes_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            etc, state = Path(directory) / 'etc', Path(directory) / 'state'
            etc.mkdir(); state.mkdir()
            with patch.object(tool, 'ETC', etc), patch.object(tool, 'STATE', state), \
                    patch.object(tool, 'run', side_effect=AssertionError('no command')), \
                    patch.object(tool, 'certificates', side_effect=AssertionError('no certificates')):
                tool.refresh()
            self.assertEqual(list(state.iterdir()), [])


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
