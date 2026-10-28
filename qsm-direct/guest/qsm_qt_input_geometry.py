#!/usr/bin/python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Optional Qt input geometry, authenticated against the focused KWin PID.

Only input enablement, window dimensions and cursor bounds cross this local
D-Bus interface. Application text, titles and input events are not requested.
"""
from __future__ import annotations
import json
import os
import re
import time

INTERFACE = 'org.qsm.InputGeometry1'
OBJECT = '/org/qsm/InputGeometry'
DBUS = 'org.freedesktop.DBus'
DBUS_PATH = '/org/freedesktop/DBus'
MAX_BYTES = 1024
LEASE_SECONDS = 2.5


def parse_geometry(raw):
    if not isinstance(raw, str) or len(raw.encode('utf-8')) > MAX_BYTES:
        raise ValueError('oversized Qt geometry')
    def unique(pairs):
        result = {}
        for name, value in pairs:
            if name in result:
                raise ValueError('duplicate Qt geometry field')
            result[name] = value
        return result
    value = json.loads(raw, object_pairs_hook=unique)
    if not isinstance(value, dict) or set(value) != {'version', 'sequence', 'window', 'enabled', 'size', 'rect', 'clip'}:
        raise ValueError('invalid Qt geometry fields')
    if (type(value['version']) is not int or value['version'] != 1 or
            type(value['sequence']) is not int or not 1 <= value['sequence'] < 2**53 or
            type(value['enabled']) is not bool or not isinstance(value['window'], str) or
            (value['window'] != '' and re.fullmatch('[0-9a-f]{32}', value['window']) is None)):
        raise ValueError('invalid Qt geometry identity')
    for name, count in (('size', 2), ('rect', 4), ('clip', 4)):
        items = value[name]
        if not isinstance(items, list) or len(items) != count or any(type(i) is not int or not 0 <= i <= 65536 for i in items):
            raise ValueError('invalid Qt geometry bounds')
    x, y, w, h = value['rect']
    width, height = value['size']
    ix, iy, iw, ih = value['clip']
    if value['enabled']:
        if not value['window'] or not width or not height or not w or not h or x+w > width or y+h > height:
            raise ValueError('Qt caret outside its client')
        if not iw or not ih or ix+iw > width or iy+ih > height or x < ix or y < iy or x+w > ix+iw or y+h > iy+ih:
            raise ValueError('Qt caret outside its input clip')
    elif value['rect'] != [0, 0, 0, 0] or value['clip'] != [0, 0, 0, 0]:
        raise ValueError('disabled Qt input has a caret')
    return value


def valid_publisher(credentials, pid, uid):
    return (isinstance(credentials, dict) and type(credentials.get('ProcessID')) is int and
            type(credentials.get('UnixUserID')) is int and credentials['ProcessID'] == pid and
            credentials['UnixUserID'] == uid)


class QtInputGeometry:
    """A bounded, optional source; it never replaces compositor authority."""
    def __init__(self, bus, state, schedule, *, clock=time.monotonic):
        from gi.repository import Gio, GLib
        self.Gio, self.GLib = Gio, GLib
        self.bus, self.state, self.schedule, self.clock = bus, state, schedule, clock
        self.focus = None
        self.service = self.owner = ''
        self.binding = None
        self.last_payload = None
        self.sequence = -1
        self.last_seen = float('-inf')
        self.owns_caret = False
        self.owner_watch = self.changed_watch = 0
        self.pending = 0
        self.cancel = None
        self.inflight = self.needs_sync = self.closed = False
        self.epoch = 0
        self.expiry = GLib.timeout_add(250, self.expire)
        self.state.on_layout_changed = self.layout_changed
        self.layout_changed()

    def focused(self):
        state = self.state
        if state.screen_locked or state.layout['locked'] or not state.layout['available']:
            return None
        window = next((w for w in state.layout['windows'] if w['id'] == state.layout['focus'] and w['visible']), None)
        if window is None or window['client_rect'] is None:
            return None
        return state.generation, window['pid'], window['id'], tuple(window['client_rect'])

    @property
    def active(self):
        return (self.owns_caret and self.binding is not None and self.focus == self.focused() and
                self.clock() - self.last_seen < LEASE_SECONDS)

    def prefer(self):
        if self.active:
            return True
        self.clear_caret()
        return False

    def clear_caret(self, notify=True):
        if self.owns_caret:
            self.state.caret = None
            self.owns_caret = False
            if notify and not self.closed:
                self.schedule(True)

    def unsubscribe(self, name):
        number = getattr(self, name)
        if number:
            self.bus.signal_unsubscribe(number)
            setattr(self, name, 0)

    def invalidate(self):
        self.epoch += 1
        if self.cancel is not None:
            self.cancel.cancel()
            self.cancel = None
        self.inflight = False
        self.binding = self.last_payload = None
        self.sequence = -1
        self.clear_caret()

    def layout_changed(self):
        if self.closed:
            return
        focus = self.focused()
        if focus == self.focus:
            return
        old_pid = self.focus[1] if self.focus else None
        self.invalidate()
        self.focus = focus
        pid = focus[1] if focus else None
        if pid != old_pid:
            self.unsubscribe('owner_watch')
            self.unsubscribe('changed_watch')
            self.owner = ''
            self.service = 'org.qsm.InputGeometry.p'+str(pid) if pid else ''
            if self.service:
                self.owner_watch = self.bus.signal_subscribe(DBUS, DBUS, 'NameOwnerChanged', DBUS_PATH,
                    self.service, self.Gio.DBusSignalFlags.NONE, self.owner_changed)
        self.queue_sync()

    def owner_changed(self, bus, sender, path, interface, name, parameters):
        service, _, _ = parameters.unpack()
        if self.closed or service != self.service:
            return
        self.invalidate()
        self.unsubscribe('changed_watch')
        self.owner = ''
        self.queue_sync()

    def queue_sync(self):
        if self.closed or self.focus is None:
            return
        self.needs_sync = True
        if not self.pending:
            self.pending = self.GLib.timeout_add(30, self.begin)

    def begin(self):
        self.pending = 0
        if self.closed or self.focus is None or self.inflight:
            return False
        self.needs_sync = False
        self.inflight = True
        self.cancel = self.Gio.Cancellable()
        context = self.epoch, self.focus
        if self.owner:
            self.snapshot(context)
        else:
            self.call(DBUS, DBUS_PATH, DBUS, 'GetNameOwner', '(s)', (self.service,), '(s)', context,
                      lambda reply: self.got_owner(context, reply))
        return False

    def call(self, destination, path, interface, method, signature, arguments, output, context, continuation):
        def done(connection, result, user_data=None):
            try:
                value = connection.call_finish(result).unpack()
            except self.GLib.Error:
                value = None
            if not self.closed and context == (self.epoch, self.focus):
                continuation(value)
        parameters = self.GLib.Variant(signature, arguments) if signature else None
        self.bus.call(destination, path, interface, method, parameters, self.GLib.VariantType.new(output),
            self.Gio.DBusCallFlags.NO_AUTO_START, 1000, self.cancel, done, None)

    def got_owner(self, context, reply):
        if not reply or not reply[0].startswith(':'):
            self.finish()
            return
        owner = reply[0]
        self.call(DBUS, DBUS_PATH, DBUS, 'GetConnectionCredentials', '(s)', (owner,), '(a{sv})', context,
                  lambda value: self.got_credentials(context, owner, value))

    def got_credentials(self, context, owner, reply):
        if not reply or not valid_publisher(reply[0], context[1][1], os.getuid()):
            self.finish()
            return
        self.owner = owner
        self.unsubscribe('changed_watch')
        self.changed_watch = self.bus.signal_subscribe(owner, INTERFACE, 'Changed', OBJECT, None,
            self.Gio.DBusSignalFlags.NONE, self.changed)
        self.snapshot(context)

    def snapshot(self, context):
        self.call(self.owner, OBJECT, INTERFACE, 'Snapshot', None, None, '(s)', context,
                  lambda reply: self.snapshot_ready(context, reply))

    def snapshot_ready(self, context, reply):
        if reply:
            try:
                self.accept(parse_geometry(reply[0]), confirmed=True)
            except (ValueError, TypeError, RecursionError):
                self.binding = None
                self.clear_caret()
        self.finish()

    def finish(self):
        self.inflight = False
        self.cancel = None
        if self.needs_sync:
            self.queue_sync()

    def changed(self, bus, sender, path, interface, name, parameters):
        if self.closed or sender != self.owner or self.focus is None or self.focus != self.focused():
            return
        try:
            value = parse_geometry(parameters.unpack()[0])
        except (ValueError, TypeError, RecursionError):
            return
        if not self.binding or self.binding != (self.focus, value['window']):
            self.clear_caret()
            self.queue_sync()
            return
        self.accept(value)

    def accept(self, value, confirmed=False):
        if self.focus is None or self.focus != self.focused():
            return
        if value['sequence'] < self.sequence or (value['sequence'] == self.sequence and value != self.last_payload):
            return
        if value['enabled'] and tuple(value['size']) != self.focus[3][2:]:
            self.binding = None
            self.clear_caret()
            return
        changed = value != self.last_payload or confirmed
        self.sequence, self.last_payload = value['sequence'], value
        self.binding = self.focus, value['window']
        self.last_seen = self.clock()
        if not value['enabled']:
            self.clear_caret()
            return
        if changed or not self.owns_caret:
            try:
                self.state.caret_available = True
                self.state.set_caret(self.focus[1], value['rect'], window_relative=True, clip=value['clip'])
            except ValueError:
                self.clear_caret()
                return
            self.owns_caret = self.state.caret is not None
            self.schedule(True)

    def expire(self):
        if self.closed:
            return False
        if not self.active:
            self.clear_caret()
        return True

    def close(self):
        self.closed = True
        self.state.on_layout_changed = None
        self.invalidate()
        self.unsubscribe('owner_watch')
        self.unsubscribe('changed_watch')
        for number in (self.pending, self.expiry):
            if number:
                self.GLib.source_remove(number)
        self.pending = self.expiry = 0
