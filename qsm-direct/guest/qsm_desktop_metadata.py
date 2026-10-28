#!/usr/bin/python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Per-session KWin metadata bridge. Geometry only, on the existing guest link.

KWin calls a same-user D-Bus object; this process atomically publishes bounded
state for the VM virtio agent or LXC session agent. Clipboard stays independent.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import stat
import sys
import tempfile
import time
import uuid

try:
    from qsm_desktop_metadata_schema import validate_metadata, DesktopMetadataError, MAX_METADATA_BYTES
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'extensions'))
    from direct_guest.qsm_desktop_metadata import validate_metadata, DesktopMetadataError, MAX_METADATA_BYTES

SERVICE = 'org.qsm.DesktopMetadata'
INTERFACE = SERVICE + '1'
OBJECT = '/org/qsm/DesktopMetadata'
PLUGIN = 'qsm-desktop-metadata'


def accessible_text_bounds(text, component, coordinate):
    """Read geometry only and bound priority to the focused text component."""
    clip = None
    if component is not None:
        box = component.get_extents(coordinate)
        if box.width > 0 and box.height > 0:
            clip = [box.x, box.y, box.width, box.height]
    offset = text.get_caret_offset()
    # Some providers expose no end-of-text box. A neighbouring character
    # remains a useful priority hint; neither is a locally rendered caret.
    for position in dict.fromkeys([offset, max(0, offset - 1)] if offset >= 0 else []):
        box = text.get_character_extents(position, coordinate)
        if box.height <= 0 or box.width < 0:
            continue
        rectangle = [box.x, box.y, max(1, box.width), box.height]
        if clip is not None:
            x, y = max(rectangle[0], clip[0]), max(rectangle[1], clip[1])
            right = min(rectangle[0] + rectangle[2], clip[0] + clip[2])
            bottom = min(rectangle[1] + rectangle[3], clip[1] + clip[3])
            if right <= x or bottom <= y:
                continue
            rectangle = [x, y, right - x, bottom - y]
        # If component bounds are unavailable, only the known character
        # box can authorize leading edits, never its surrounding window.
        return rectangle, clip if clip is not None else rectangle.copy()
    # Empty controls and providers lacking character geometry can still
    # identify their complete focused input. Keep its clip explicit too.
    return (clip.copy(), clip) if clip is not None else None


def register_metadata_object(bus, interface, callback):
    register = bus.register_object
    # Old GI maps this API to the original closure entry point, whose
    # transferred invocation reference is otherwise leaked on every damage
    # notification. PyGObject 3.50 wraps it; GLib 2.84 has a corrected
    # closures2 entry point. Test the actual callable rather than distro
    # versions, so backported overrides cannot cause a second unref.
    # https://gitlab.gnome.org/GNOME/pygobject/-/merge_requests/219
    if getattr(register, 'get_symbol', lambda: None)() == 'g_dbus_connection_register_object_with_closures':
        method = callback
        def owned_callback(*args):
            try:
                method(*args)
            finally:
                args[-1]._unref()
        callback = owned_callback
    return register(OBJECT, interface, callback, None, None)


class DesktopState:
    """Pure state model shared by production callbacks and protocol tests."""
    def __init__(self):
        self.session = uuid.uuid4().hex
        self.sequence = 0
        self.generation = 1
        self.layout = {'desktop': {'rect': [0, 0, 0, 0], 'scale': 1}, 'windows': [],
                       'focus': None, 'available': False, 'locked': False}
        self.damage = set()
        self.caret = None
        self.caret_available = False
        self.screen_locked = False
        self.on_layout_changed = None

    def snapshot(self, version=1):
        self.sequence += 1
        locked = self.screen_locked or self.layout['locked']
        active = self.layout['available'] and not locked
        caps = ['windows', 'focus', 'window-damage']
        if self.caret_available:
            caps.append('caret')
        caret = None
        if self.caret is not None and active and (version == 2 or self.caret.get('clip') is None):
            caret = {k: self.caret[k] for k in ('window', 'rect')}
            if version == 2:
                caret['clip'] = self.caret.get('clip')
        return validate_metadata({'version': version, 'session': self.session, 'sequence': self.sequence,
            'generation': self.generation, 'available': self.layout['available'], 'locked': locked,
            'capabilities': caps, 'desktop': self.layout['desktop'],
            'windows': self.layout['windows'] if active else [], 'focus': self.layout['focus'] if active else None,
            'damage': sorted(self.damage)[:64] if active else [], 'caret': caret})

    def set_layout(self, value):
        if not isinstance(value, dict) or set(value) != {'desktop', 'windows', 'focus', 'available', 'locked'}:
            raise DesktopMetadataError('invalid KWin layout')
        candidate = {'version': 1, 'session': self.session, 'sequence': 1, 'generation': 1,
            'capabilities': ['windows', 'focus', 'window-damage'], **value, 'damage': [], 'caret': None}
        parsed = validate_metadata(candidate)
        new = {k: parsed[k] for k in self.layout}
        if new == self.layout:
            return False
        self.layout = new
        self.generation += 1
        self.damage.clear()
        self.caret = None
        if self.on_layout_changed is not None:
            self.on_layout_changed()
        return True

    def invalidate(self):
        self.set_layout({'desktop': self.layout['desktop'], 'windows': [], 'focus': None,
                         'available': False, 'locked': False})

    def set_locked(self, locked):
        if self.screen_locked != locked:
            self.screen_locked = locked
            self.generation += 1
            self.damage.clear()
            self.caret = None
            if self.on_layout_changed is not None:
                self.on_layout_changed()

    def set_caret(self, pid, rectangle, *, window_relative=False, clip=None):
        focus = next((w for w in self.layout['windows'] if w['id'] == self.layout['focus'] and w['visible']), None)
        self.caret = None
        if focus is None or focus['pid'] != pid or self.layout['locked'] or self.screen_locked or not self.layout['available']:
            return
        if window_relative:
            if focus['client_rect'] is None:
                raise DesktopMetadataError('client origin unavailable')
            x, y, width, height = focus['client_rect']
            cx, cy, cw, ch = rectangle
            if cx < 0 or cy < 0 or cx + cw > width or cy + ch > height:
                raise DesktopMetadataError('caret outside client coordinates')
            rectangle = [x + cx, y + cy, cw, ch]
            if clip is not None:
                ix, iy, iw, ih = clip
                clip = [x+ix, y+iy, iw, ih]
        candidate = self.snapshot(version=2)
        candidate['capabilities'] = sorted(set(candidate['capabilities']) | {'caret'})
        candidate['caret'] = {'window': focus['id'], 'rect': rectangle, 'clip': clip}
        self.caret = validate_metadata(candidate)['caret']


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', type=Path, required=True)
    parser.add_argument('--script', type=Path, default=Path(__file__).with_name('qsm_kwin_metadata.js'))
    args = parser.parse_args(argv)
    import gi
    gi.require_version('Gio', '2.0')
    from gi.repository import Gio, GLib
    from qsm_qt_input_geometry import QtInputGeometry

    class Bridge:
        def __init__(self):
            self.state = DesktopState()
            self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            self.loop = GLib.MainLoop()
            self.pending = 0
            self.owner = ''
            self.script_path = None
            self.atspi = None
            self.atspi_listener = None
            self.last_pulse = 0
            self.next_retry = 0
            self.pulse_pending = 0
            self.needs_layout = True
            owned = self.bus.call_sync('org.freedesktop.DBus', '/org/freedesktop/DBus',
                'org.freedesktop.DBus', 'RequestName', GLib.Variant('(su)', (SERVICE, 4)),
                GLib.VariantType.new('(u)'), Gio.DBusCallFlags.NONE, 2000, None).unpack()[0]
            if owned != 1:
                raise RuntimeError('the session already has a metadata bridge')
            self.directory = args.state_dir
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            info = self.directory.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise RuntimeError('metadata state directory must be private to this user')
            xml = '<node><interface name="'+INTERFACE+'">' + \
                '<method name="Publish"><arg type="s" direction="in"/></method>' + \
                '<method name="Damage"><arg type="s" direction="in"/></method>' + \
                '<method name="Pulse"><arg type="b" direction="out"/></method>' + \
                '<method name="GetSnapshot"><arg type="s" direction="out"/></method>' + \
                '</interface></node>'
            iface = Gio.DBusNodeInfo.new_for_xml(xml).interfaces[0]
            register_metadata_object(self.bus, iface, self.method)
            self.bus.signal_subscribe('org.freedesktop.DBus', 'org.freedesktop.DBus', 'NameOwnerChanged',
                '/org/freedesktop/DBus', 'org.kde.KWin', Gio.DBusSignalFlags.NONE, self.owner_changed)
            self.bus.signal_subscribe('org.freedesktop.ScreenSaver', 'org.freedesktop.ScreenSaver',
                'ActiveChanged', None, None, Gio.DBusSignalFlags.NONE, self.lock_changed)
            self.bus.signal_subscribe('org.freedesktop.DBus', 'org.freedesktop.DBus', 'NameOwnerChanged',
                '/org/freedesktop/DBus', 'org.freedesktop.ScreenSaver', Gio.DBusSignalFlags.NONE,
                self.lock_owner_changed)
            try:
                self.owner = self.bus.call_sync('org.freedesktop.DBus', '/org/freedesktop/DBus',
                    'org.freedesktop.DBus', 'GetNameOwner', GLib.Variant('(s)', ('org.kde.KWin',)),
                    GLib.VariantType.new('(s)'), Gio.DBusCallFlags.NONE, 2000, None).unpack()[0]
            except GLib.Error:
                pass
            GLib.idle_add(self.install)
            GLib.timeout_add_seconds(1, self.heartbeat)
            self.setup_accessibility()
            self.qt_geometry = QtInputGeometry(self.bus, self.state, self.schedule)
            self.read_lock()
            self.write()

        def write(self):
            self.pending = 0
            # Retain a strict v1 file for already-running/older agents. A v2
            # subscription selects the additional input-component bounds.
            value = self.state.snapshot(version=2)
            for version, name in ((2, 'desktop-metadata-v2.json'), (1, 'desktop-metadata.json')):
                if version == 1:
                    value = dict(value, version=1)
                    if value['caret'] is not None:
                        value['caret'] = (None if value['caret']['clip'] is not None else
                            {k:value['caret'][k] for k in ('window','rect')})
                raw = json.dumps(value, separators=(',', ':'), ensure_ascii=True).encode()
                if len(raw) > MAX_METADATA_BYTES:
                    self.state.invalidate()
                    raw = json.dumps(self.state.snapshot(version=version), separators=(',', ':')).encode()
                fd, filename = tempfile.mkstemp(prefix='.desktop-metadata-', dir=self.directory)
                temporary = Path(filename)
                try:
                    with os.fdopen(fd, 'wb') as f:
                        f.write(raw)
                    os.replace(temporary, self.directory / name)
                finally:
                    temporary.unlink(missing_ok=True)
            self.state.damage.clear()
            return False

        def schedule(self, urgent=False):
            if urgent and self.pending:
                GLib.source_remove(self.pending)
                self.pending = 0
            if not self.pending:
                self.pending = GLib.idle_add(self.write) if urgent else GLib.timeout_add(50, self.write)

        def heartbeat(self):
            now = time.monotonic()
            if self.owner and now - self.last_pulse > 3:
                self.state.invalidate()
                self.needs_layout = True
            # A delayed pulse can resume before the retry deadline. It does
            # not restore the layout cleared by expiry; require a fresh
            # publish even when the old pulse loop has become healthy again.
            if self.owner and self.needs_layout and now >= self.next_retry:
                self.install()
            self.schedule()
            return True

        def method(self, connection, sender, path, interface, method, parameters, invocation):
            if method == 'GetSnapshot':
                invocation.return_value(GLib.Variant('(s)', (json.dumps(self.state.snapshot(), separators=(',', ':')),)))
                return
            if not self.owner or sender != self.owner:
                invocation.return_dbus_error(INTERFACE+'.AccessDenied', 'Only the session compositor may publish state')
                return
            if method == 'Pulse':
                # A reload may overlap the old script's delayed reply. Keep
                # that overlap bounded without rejecting the new script's
                # first pulse and permanently stopping its lease loop.
                if self.pulse_pending >= 2:
                    invocation.return_dbus_error(INTERFACE+'.Busy', 'A compositor pulse is already pending')
                    return
                self.last_pulse = time.monotonic()
                self.pulse_pending += 1
                def reply():
                    self.pulse_pending -= 1
                    invocation.return_value(GLib.Variant('(b)', (sender == self.owner,)))
                    return False
                GLib.timeout_add(750, reply)
                return
            try:
                value = parameters.unpack()[0]
                if len(value.encode()) > MAX_METADATA_BYTES:
                    raise DesktopMetadataError('oversized layout')
                if method == 'Publish':
                    changed = self.state.set_layout(json.loads(value))
                    self.needs_layout = False
                    if changed:
                        self.schedule(True)
                elif method == 'Damage':
                    if any(w['id'] == value for w in self.state.layout['windows']):
                        self.state.damage.add(value)
                        self.schedule()
                else:
                    raise DesktopMetadataError('unknown method')
                invocation.return_value(GLib.Variant('()', ()))
            except (ValueError, TypeError, RecursionError):
                self.needs_layout = True
                self.state.invalidate()
                self.schedule(True)
                invocation.return_dbus_error(INTERFACE+'.Invalid', 'Invalid bounded desktop state')

        def owner_changed(self, connection, sender, path, interface, signal_name, parameters):
            _, _, self.owner = parameters.unpack()
            self.script_path = None
            self.needs_layout = True
            self.state.invalidate()
            self.schedule(True)
            if self.owner:
                GLib.idle_add(self.install)

        def lock_changed(self, connection, sender, path, interface, signal_name, parameters):
            self.state.set_locked(bool(parameters.unpack()[0]))
            self.schedule(True)

        def lock_owner_changed(self, connection, sender, path, interface, signal_name, parameters):
            self.read_lock()

        def read_lock(self):
            try:
                active = self.bus.call_sync('org.freedesktop.ScreenSaver', '/ScreenSaver',
                    'org.freedesktop.ScreenSaver', 'GetActive', None, GLib.VariantType.new('(b)'),
                    Gio.DBusCallFlags.NO_AUTO_START, 1000, None).unpack()[0]
            except GLib.Error as error:
                # Standalone KWin can have no locker. An existing, failing
                # locker is uncertain: suppress hints until it recovers.
                missing = Gio.dbus_error_get_remote_error(error) in (
                    'org.freedesktop.DBus.Error.ServiceUnknown',
                    'org.freedesktop.DBus.Error.NameHasNoOwner')
                active = self.state.screen_locked or not missing
            self.state.set_locked(bool(active))
            self.schedule(True)

        def kwin_call(self, path, interface, method, parameters=None, reply=None):
            return self.bus.call_sync('org.kde.KWin', path, interface, method, parameters,
                reply, Gio.DBusCallFlags.NONE, 3000, None)

        def install(self):
            if not self.owner:
                return False
            self.next_retry = time.monotonic() + 5
            try:
                self.kwin_call('/Scripting', 'org.kde.kwin.Scripting', 'unloadScript', GLib.Variant('(s)', (PLUGIN,)))
                number = self.kwin_call('/Scripting', 'org.kde.kwin.Scripting', 'loadScript',
                    GLib.Variant('(ss)', (str(args.script.resolve()), PLUGIN))).unpack()[0]
                if number < 0:
                    raise RuntimeError('KWin rejected metadata script')
                # KWin 5 reuses scripts.size() as the D-Bus object ID. After
                # unloading a non-last script its new ID can collide with a
                # still-loaded script. The public start() dispatcher starts
                # loaded scripts without restarting those already running,
                # and avoids that broken per-object path on both KWin 5/6.
                self.kwin_call('/Scripting', 'org.kde.kwin.Scripting', 'start')
                self.script_path = str(number)
                self.last_pulse = time.monotonic()
            except (GLib.Error, RuntimeError) as error:
                self.state.invalidate()
                self.schedule(True)
                print('qsm-desktop-metadata: compositor integration unavailable: '+str(error), file=sys.stderr)
            return False

        def setup_accessibility(self):
            try:
                gi.require_version('Atspi', '2.0')
                from gi.repository import Atspi
                Atspi.init()
                self.atspi = Atspi
                self.atspi_listener = Atspi.EventListener.new(self.caret_event, None)
                for name in ['object:text-caret-moved', 'object:state-changed:focused']:
                    self.atspi_listener.register(name)
                self.state.caret_available = True
            except (ValueError, ImportError, GLib.Error):
                self.atspi = None

        def caret_event(self, event, user_data=None):
            qt_geometry = getattr(self, 'qt_geometry', None)
            if qt_geometry is not None and qt_geometry.prefer():
                return
            try:
                source = event.source
                if 'state-changed:focused' in event.type and not event.detail1:
                    self.state.caret = None
                    self.schedule(True)
                    return
                if not source.get_state_set().contains(self.atspi.StateType.FOCUSED):
                    return
                text = source.get_text_iface()
                if text is None:
                    self.state.caret = None
                    self.schedule(True)
                    return
                application = source.get_application()
                component = source.get_component_iface()
                wayland = os.environ.get('XDG_SESSION_TYPE') == 'wayland' or bool(os.environ.get('WAYLAND_DISPLAY'))
                coordinates = [self.atspi.CoordType.WINDOW] if wayland else [self.atspi.CoordType.SCREEN]
                for coordinate in coordinates:
                    bounds = accessible_text_bounds(text, component, coordinate)
                    if bounds is not None:
                        rectangle, clip = bounds
                        self.state.set_caret(application.get_process_id(), rectangle, clip=clip,
                            window_relative=coordinate == self.atspi.CoordType.WINDOW)
                        self.schedule(True)
                        return
                raise ValueError('caret geometry unavailable')
            except (AttributeError, ValueError, GLib.Error):
                self.state.caret = None
                self.schedule(True)

        def stop(self):
            self.qt_geometry.close()
            if self.pending:
                GLib.source_remove(self.pending)
                self.pending = 0
            self.state.invalidate()
            self.write()
            if self.owner:
                try:
                    self.kwin_call('/Scripting', 'org.kde.kwin.Scripting', 'unloadScript', GLib.Variant('(s)', (PLUGIN,)))
                except GLib.Error:
                    pass
            self.loop.quit()
            return False

    bridge = Bridge()
    for sig in [signal.SIGTERM, signal.SIGINT]:
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, bridge.stop)
    bridge.loop.run()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
