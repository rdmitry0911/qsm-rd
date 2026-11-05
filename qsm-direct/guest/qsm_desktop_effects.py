#!/usr/bin/python3
"""Turn KWin's Breeze window shadows off or back on for the calling user.

Over changing content a moving shadow is new blended pixels on every frame, which
neither copy detection nor pixel history can reuse. On a narrow path that costs more
than the window itself. Nothing here runs by itself: the user chooses when.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

CONFIG = ('--file', 'breezerc', '--group', 'Common', '--key', 'ShadowSize')
DECORATION = ('--file', 'kwinrc', '--group', 'org.kde.kdecoration2', '--key', 'library')
OFF = 'ShadowNone'


def tool(*names):
    for name in names:
        path = shutil.which(name)
        if path:
            return path
    raise SystemExit(f'{names[0]} is required: this works on KDE Plasma only')


def run(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout.strip()


def state_path():
    base = os.environ.get('XDG_STATE_HOME') or str(Path.home() / '.local/state')
    return Path(base) / 'qsm-desktop-effects.json'


def reload_kwin():
    # Without a running session the setting simply applies at the next login.
    qdbus = shutil.which('qdbus6') or shutil.which('qdbus')
    if not qdbus or subprocess.run([qdbus, 'org.kde.KWin', '/KWin', 'reconfigure'], capture_output=True).returncode:
        print('KWin was not reached: the change applies at the next login', file=sys.stderr)


def reduce():
    read = tool('kreadconfig6', 'kreadconfig5')
    write = tool('kwriteconfig6', 'kwriteconfig5')
    library = run(read, *DECORATION)
    if library and library != 'org.kde.breeze':
        print(f'The window decoration is {library}, which this setting does not affect', file=sys.stderr)
    state = state_path()
    # Reducing twice must not record the reduced value as the original.
    if not state.exists():
        state.parent.mkdir(parents=True, exist_ok=True)
        temporary = state.with_suffix('.tmp')
        temporary.write_text(json.dumps({'shadow_size': run(read, *CONFIG)}) + '\n')
        temporary.replace(state)
    run(write, *CONFIG, OFF)
    reload_kwin()
    print('reduced')


def restore():
    state = state_path()
    if not state.exists():
        print('nothing to restore')
        return
    write = tool('kwriteconfig6', 'kwriteconfig5')
    earlier = json.loads(state.read_text())['shadow_size']
    if earlier:
        run(write, *CONFIG, earlier)
    else:
        run(write, *CONFIG, '--delete')
    reload_kwin()
    state.unlink()
    print('restored')


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('action', choices=('reduce', 'restore', 'status'))
    action = parser.parse_args().action
    if action == 'reduce':
        reduce()
    elif action == 'restore':
        restore()
    else:
        print('reduced' if state_path().exists() else 'normal')


if __name__ == '__main__':
    main()
