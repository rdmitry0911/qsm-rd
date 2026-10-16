#!/usr/bin/env python3
"""Exercise the native compositor adapter with real Wayland-style FD transfers."""
import array
import json
import tempfile
from unittest import mock
import os
from pathlib import Path
import runpy
import socket
import threading
import unittest

MODULE = runpy.run_path(str(Path(__file__).resolve().parents[2] / 'packaging/lxc/qsm-native-seat'))
forward = MODULE['forward_wayland']


class WaylandDescriptorRelayTests(unittest.TestCase):
    def test_shared_buffer_and_following_bytes_survive_relay(self):
        sender, ingress = socket.socketpair()
        egress, receiver = socket.socketpair()
        descriptor = os.memfd_create('qsm-native-seat-test')
        thread = threading.Thread(target=forward, args=(ingress, egress))
        thread.start()
        received = []
        try:
            os.write(descriptor, b'contents of a Wayland shared-memory buffer')
            sender.sendmsg([b'wl_shm_create_pool'], [
                (socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array('i', [descriptor]))])
            data, ancillary, flags, _ = receiver.recvmsg(4096, socket.CMSG_SPACE(4))
            self.assertEqual(data, b'wl_shm_create_pool')
            self.assertFalse(flags & socket.MSG_CTRUNC)
            for level, kind, value in ancillary:
                self.assertEqual((level, kind), (socket.SOL_SOCKET, socket.SCM_RIGHTS))
                fds = array.array('i')
                fds.frombytes(value)
                received.extend(fds)
            self.assertEqual(len(received), 1)
            self.assertEqual(os.pread(received[0], 100, 0), b'contents of a Wayland shared-memory buffer')
            payload = bytes(range(256)) * 2048
            writer = threading.Thread(target=sender.sendall, args=(payload,))
            writer.start()
            chunks = bytearray()
            while len(chunks) < len(payload):
                chunks.extend(receiver.recv(32768))
            writer.join(timeout=3)
            self.assertFalse(writer.is_alive())
            self.assertEqual(chunks, payload)
            self.assertEqual(os.pread(descriptor, 8, 0), b'contents')
        finally:
            for fd in received:
                os.close(fd)
            os.close(descriptor)
            sender.close()
            thread.join(timeout=3)
            for endpoint in (ingress, egress, receiver):
                endpoint.close()
        self.assertFalse(thread.is_alive())

    def test_closed_receiver_does_not_leak_received_descriptors(self):
        before = len(os.listdir('/proc/self/fd'))
        for _ in range(16):
            sender, ingress = socket.socketpair()
            egress, receiver = socket.socketpair()
            receiver.close()
            descriptor = os.memfd_create('qsm-native-seat-disconnect')
            sender.sendmsg([b'buffer'], [(socket.SOL_SOCKET, socket.SCM_RIGHTS,
                                        array.array('i', [descriptor]))])
            thread = threading.Thread(target=forward, args=(ingress, egress))
            thread.start()
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive())
            os.close(descriptor)
            for endpoint in (sender, ingress, egress):
                endpoint.close()
        self.assertEqual(len(os.listdir('/proc/self/fd')), before)


class NativeSessionCleanupTests(unittest.TestCase):
    def test_stale_pam_record_does_not_revoke_a_replacement_desktop(self):
        active = MODULE['active_users']
        uid = os.getuid()
        for replacement in (False, True):
            with self.subTest(replacement=replacement), tempfile.TemporaryDirectory() as directory:
                runtime = Path(directory)
                seat = runtime / '1'; seat.mkdir()
                old = seat / 'old.json'
                old.write_text(json.dumps({'uid': uid, 'service': 'qsm-sddm'}))
                if replacement:
                    (seat / 'new.json').write_text(old.read_text())
                def properties(identifier):
                    return {'State': 'closing' if identifier == 'old' else 'active',
                            'Seat': 'seatqsm1', 'User': str(uid), 'Service': 'qsm-sddm'}
                revoke, stop = mock.Mock(), mock.Mock()
                with mock.patch.dict(active.__globals__, {'RUNTIME': runtime,
                        'session_properties': properties, 'command': revoke, 'stop_desktop': stop}):
                    self.assertEqual(active(1), {uid} if replacement else set())
                self.assertFalse(old.exists())
                self.assertEqual(stop.call_count, 0 if replacement else 1)
                self.assertEqual(revoke.call_count, 0 if replacement else 1)


class NativeManifestTests(unittest.TestCase):
    def test_device_scope_and_duplicate_seats_are_rejected(self):
        configure = MODULE['configuration']
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'seats.json'
            with mock.patch.dict(configure.__globals__, {'CONFIG': path}), mock.patch.object(
                    os, 'fstat', wraps=os.fstat) as fstat:
                # Keep this test runnable unprivileged while exercising actual
                # files, modes, O_NOFOLLOW and manifest parsing.
                real_fstat = os.fstat.__wrapped__ if hasattr(os.fstat, '__wrapped__') else fstat._mock_wraps
                def owned(fd):
                    meta = real_fstat(fd)
                    values = list(meta)
                    values[4] = 0
                    return os.stat_result(values)
                fstat.side_effect = owned
                valid = {'vmid': 108, 'slots': [{'slot': 1, 'device': '/dev/qsm-native/108-seat1'}]}
                path.write_text(json.dumps(valid)); path.chmod(0o644)
                self.assertEqual(configure(), valid)
                invalid = [[], {'vmid': True, 'slots': valid['slots']},
                           {'vmid': 108, 'slots': [None]},
                           {'vmid': 108, 'slots': valid['slots'] * 2},
                           {'vmid': 108, 'slots': [{'slot': 1, 'device': '/dev/qsm-native/109-seat1'}]},
                           {'vmid': 108, 'slots': [{'slot': 1, 'device': '/dev/tty0'}]}]
                for manifest in invalid:
                    path.write_text(json.dumps(manifest))
                    with self.subTest(manifest=manifest), self.assertRaises(ValueError):
                        configure()
                path.write_text(json.dumps(valid)); path.chmod(0o666)
                with self.assertRaises(ValueError): configure()
                path.unlink(); path.symlink_to('/dev/null')
                with self.assertRaises(OSError): configure()


if __name__ == '__main__':
    unittest.main()
