#!/usr/bin/env python3
"""Root-only disposable overmount integration; pass a dynfilefs >=4.6 binary.

Run in a test VM for actual kernel/union coverage. No existing block device is
accepted: only this script's temporary DynFileFS image is formatted/mounted.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time


def main():
    binary = str(Path(sys.argv[1]).resolve())
    encrypted = '--luks' in sys.argv[2:]
    union = '--union' in sys.argv[2:]
    lib = str(Path(__file__).resolve().parents[1] / 'lib')
    with tempfile.TemporaryDirectory(prefix='session-online-') as directory:
        root = Path(directory)
        store = root / 'sessions'
        session = store / '1'
        session.mkdir(parents=True)
        mount = root / 'changes'
        mount.mkdir()
        tools = root / 'bin'
        tools.mkdir()
        (tools / 'dynfilefs').symlink_to(binary)
        environment = dict(os.environ, PATH=str(tools) + ':' + os.environ['PATH'], PYTHONPATH=lib)
        with (root / 'daemon.log').open('w+') as log:
            process = subprocess.Popen([binary, '-f', str(session / 'changes.dat'),
                                        '-m', str(mount), '-s', '64', '-p', '32', '-d'],
                                       stdout=log, stderr=log)
            inner = False
            union_mounted = False
            loop = None
            mapper = None
            try:
                image = mount / 'virtual.dat'
                for _ in range(100):
                    if image.exists():
                        break
                    if process.poll() is not None:
                        raise RuntimeError('daemon exited')
                    time.sleep(0.05)
                else:
                    raise RuntimeError('FUSE mount timeout')
                inner_source = str(image)
                if encrypted:
                    key = root / 'key'
                    key.write_bytes(os.urandom(32))
                    key.chmod(0o600)
                    loop = subprocess.check_output(['losetup', '--find', '--show', str(image)]).decode().strip()
                    subprocess.run(['cryptsetup', 'luksFormat', '--batch-mode', '--type', 'luks2',
                                    '--pbkdf', 'pbkdf2', '--key-file', str(key), loop], check=True)
                    mapper = 'session-online-{}'.format(os.getpid())
                    subprocess.run(['cryptsetup', 'open', '--key-file', str(key), loop, mapper], check=True)
                    inner_source = '/dev/mapper/' + mapper
                subprocess.run(['mkfs.ext4', '-q', '-F', '-E', 'nodiscard', inner_source], check=True)
                subprocess.run(['mount', '-t', 'ext4', '-o', 'rw' if encrypted else 'loop', inner_source, str(mount)], check=True)
                inner = True
                working = mount
                if union:
                    readonly = root / 'readonly'
                    readonly.mkdir()
                    working = root / 'union'
                    working.mkdir()
                    if '--aufs' in sys.argv[2:]:
                        subprocess.run(['mount', '-t', 'aufs', '-o', 'br={}=rw:{}=ro'.format(mount, readonly),
                                        'none', str(working)], check=True)
                    else:
                        (mount / 'upper').mkdir()
                        (mount / 'work').mkdir()
                        options = 'lowerdir={},upperdir={},workdir={}'.format(
                            readonly, mount / 'upper', mount / 'work')
                        subprocess.run(['mount', '-t', 'overlay', '-o', options,
                                        'overlay', str(working)], check=True)
                    union_mounted = True
                keep = os.urandom(65536)
                (working / 'keep').write_bytes(keep)
                (working / 'victim').write_bytes(os.urandom(8 * 1024 * 1024))
                subprocess.run(['sync', '-f', str(mount)], check=True)
                before = sum(p.stat().st_blocks * 512 for p in session.iterdir())
                (working / 'victim').unlink()
                subprocess.run(['sync', '-f', str(mount)], check=True)
                identity = store.stat()
                state = {'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                         'boot_level': 'ok', 'session': '1', 'mode': 'dynfilefs',
                         'encryption': 'luks' if encrypted else 'none', 'durable': '1', 'writable': '1',
                         'crypt_mapper': mapper or 'none', 'loop_device': loop or 'none',
                         'sessions_device': str(identity.st_dev), 'sessions_inode': str(identity.st_ino)}
                boot_state = root / 'boot-state'
                boot_state.write_text(''.join('{}={}\n'.format(k, v) for k, v in state.items()))
                boot_state.chmod(0o600)
                original_mounts = Path('/proc/self/mountinfo').read_text()
                script = '''
import json, sys
import minios_dynfilefs_reclaim as worker
from minios_session import SessionManager
manager = SessionManager.__new__(SessionManager)
manager.sessions_dir = sys.argv[1]
manager.BOOT_STATE_FILE = sys.argv[2]
worker.CHANGES_PATHS = (sys.argv[3],)
print(json.dumps(worker.reclaim(manager, '1', sys.argv[4], True)))
'''
                reply = subprocess.check_output([sys.executable, '-c', script, str(store),
                                                 str(boot_state), str(mount), state['encryption']], env=environment)
                assert json.loads(reply)['complete'] is True
                assert Path('/proc/self/mountinfo').read_text() == original_mounts
                assert (working / 'keep').read_bytes() == keep
                (working / 'after').write_bytes(b'still writable')
                subprocess.run(['sync', '-f', str(mount)], check=True)
                after = sum(p.stat().st_blocks * 512 for p in session.iterdir())
                if not encrypted:
                    assert after < before, (before, after)
                else:
                    status = subprocess.check_output(['cryptsetup', 'status', mapper]).decode()
                    assert 'discards' not in status
                assert process.poll() is None
                print('PASS online overmount (encryption={}, union={}): unchanged host mounts, live daemon, data/write intact; {} -> {}'.format(encrypted, union, before, after))
            finally:
                if union_mounted:
                    subprocess.run(['umount', str(working)], check=True)
                if inner:
                    subprocess.run(['umount', str(mount)], check=True)
                if mapper:
                    subprocess.run(['cryptsetup', 'close', mapper], check=True)
                if loop:
                    subprocess.run(['losetup', '--detach', loop], check=True)
                if os.path.ismount(str(mount)):
                    subprocess.run(['umount', str(mount)], check=True)
                process.wait(timeout=10)
            # Exercise the real detached SessionManager path after the same
            # image is no longer mounted. No fixture substitutes backend I/O.
            detached = '''
import json, sys
from minios_session import SessionManager
manager = SessionManager.__new__(SessionManager)
manager._invalidate_size_cache = lambda session_id: None
print(json.dumps(manager._reclaim_dynfilefs('1', sys.argv[1], sys.argv[2], True)))
'''
            reply = subprocess.check_output([sys.executable, '-c', detached, str(session),
                                             'luks' if encrypted else 'none'], env=environment)
            assert json.loads(reply)['complete'] is True
            print('PASS detached SessionManager reclaim (encryption={})'.format(encrypted))


if __name__ == '__main__':
    main()
