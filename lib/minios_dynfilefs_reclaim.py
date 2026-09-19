#!/usr/bin/env python3
"""Private-namespace worker for the running DynFileFS session.

MiniOS mounts inner ext4 over the FUSE mount. Detach that upper mount only in
this short-lived, recursively private namespace; the host's union/ext4/loop
and daemon remain mounted and running. No loop device is ever detached here.
"""
import ctypes
import fcntl
import json
import os
import re
import stat
import struct
import subprocess
import sys

from minios_session import SessionManager, _

CHANGES_PATHS = ('/run/initramfs/memory/changes', '/memory/changes')
RECLAIM_IOCTL = 0xc018df01  # DynFileFS 4.6 fixed-width 24-byte request
LOOP_GET_STATUS64 = 0x4c05


def private_namespace():
    libc = ctypes.CDLL(None, use_errno=True)
    libc.unshare.argtypes = [ctypes.c_int]
    libc.mount.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p,
                          ctypes.c_ulong, ctypes.c_void_p]
    before = os.stat('/proc/self/ns/mnt').st_ino
    if libc.unshare(0x00020000) != 0:  # CLONE_NEWNS
        raise OSError(ctypes.get_errno(), _('Cannot isolate session mounts.'))
    if libc.mount(None, b'/', None, (1 << 18) | (1 << 14), None) != 0:  # PRIVATE|REC
        raise OSError(ctypes.get_errno(), _('Cannot isolate session mounts.'))
    if os.stat('/proc/self/ns/mnt').st_ino == before:
        raise OSError(_('Cannot isolate session mounts.'))


def mount_stack():
    entries = []
    with open('/proc/self/mountinfo', encoding='utf-8') as stream:
        for line in stream:
            fields = line.split()
            split = fields.index('-')
            entries.append({'id': fields[0], 'parent': fields[1], 'dev': fields[2],
                            'root': fields[3], 'path': SessionManager._decode_mount_field(fields[4]),
                            'rw': 'rw' in fields[5].split(','), 'fs': fields[split + 1],
                            'source': SessionManager._decode_mount_field(fields[split + 2])})
    return entries


def select_mounts(entries, paths):
    pairs = []
    for lower in entries:
        if (lower['path'] not in paths or lower['root'] != '/' or not lower['rw'] or
                not (lower['fs'] == 'fuse' or lower['fs'].startswith('fuse.'))):
            continue
        for upper in entries:
            if (upper['path'] == lower['path'] and upper['parent'] == lower['id'] and
                    upper['root'] == '/' and upper['rw'] and upper['fs'] == 'ext4'):
                pairs.append((lower, upper))
    if len(pairs) != 1:
        raise OSError(_('Cannot identify the running DynFileFS mount stack.'))
    return pairs[0]


def loop_identity(fd):
    data = bytearray(232)  # struct loop_info64, same layout on i686/amd64
    fcntl.ioctl(fd, LOOP_GET_STATUS64, data, True)
    device, inode, _rdevice, offset, limit = struct.unpack_from('=QQQQQ', data)
    _number, _encryption, _key_size, flags = struct.unpack_from('=IIII', data, 40)
    if offset or limit or flags & 1:  # no partition offset/limit, no LO_FLAGS_READ_ONLY
        raise OSError(_('Unsupported session loop configuration.'))
    return device, inode


def reclaim(manager, session_id, encryption, compact):
    state = manager._running_persistence_state(session_id, 'dynfilefs', encryption)
    # Namespace isolation is mandatory even when invoked directly, and occurs
    # before any detach. No caller option can bypass it.
    private_namespace()
    lower, upper = select_mounts(mount_stack(), CHANGES_PATHS)
    mount_point = upper['path']
    device = upper['source'] if encryption == 'none' else state.get('loop_device', '')
    if not re.fullmatch(r'/dev/loop[0-9]+', device):
        raise OSError(_('Cannot identify the running session loop device.'))
    if encryption != 'none':
        mapper = state.get('crypt_mapper', '')
        if (not re.fullmatch(r'[A-Za-z0-9_.+-]+', mapper) or mapper == 'none' or
                os.stat('/dev/mapper/' + mapper).st_rdev != os.stat(upper['source']).st_rdev):
            raise OSError(_('Session encryption mapping changed.'))
    loop_fd = os.open(device, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
    mount_fd = None
    image_fd = None
    try:
        loop_stat = os.fstat(loop_fd)
        if not stat.S_ISBLK(loop_stat.st_mode):
            raise OSError(_('Expected a block device.'))
        backing_identity = loop_identity(loop_fd)
        expected_dev = '{}:{}'.format(os.major(backing_identity[0]), os.minor(backing_identity[0]))
        if lower['dev'] != expected_dev:
            raise OSError(_('Session loop device does not belong to DynFileFS.'))
        mount_fd = os.open(mount_point, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        actual_dev = os.fstat(mount_fd).st_dev
        if '{}:{}'.format(os.major(actual_dev), os.minor(actual_dev)) != upper['dev']:
            raise OSError(_('Session mount changed before space reclamation.'))
        if encryption == 'none' and actual_dev != loop_stat.st_rdev:
            raise OSError(_('Session mount changed before space reclamation.'))
        libc = ctypes.CDLL(None, use_errno=True)
        libc.umount2.argtypes = [ctypes.c_char_p, ctypes.c_int]
        if libc.umount2(os.fsencode(mount_point), 2) != 0:  # MNT_DETACH, private namespace only
            raise OSError(ctypes.get_errno(), _('Cannot expose the running DynFileFS image.'))
        image_fd = os.open(os.path.join(mount_point, 'virtual.dat'),
                           os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
        image_stat = os.fstat(image_fd)
        if (not stat.S_ISREG(image_stat.st_mode) or
                (image_stat.st_dev, image_stat.st_ino) != backing_identity):
            raise OSError(_('Session image changed before space reclamation.'))
        # Probe the actual running daemon, not just the newly installed CLI.
        # EOF cursor completes without scanning or reclaiming any block.
        request = bytearray(struct.pack('=QQII', image_stat.st_size, 0, 0, 0))
        try:
            fcntl.ioctl(image_fd, RECLAIM_IOCTL, request, True)
        except OSError as error:
            raise OSError(_('The running DynFileFS daemon does not support reclamation or is not writable: {}').format(error))
        if struct.unpack('=QQII', request)[3] != 1:
            raise OSError(_('Invalid DynFileFS reclaim response.'))
        if encryption == 'none':
            # The descriptor still pins the original ext4, even though its
            # pathname now resolves to FUSE inside this namespace.
            manager._trim_block_filesystem_fd(device, mount_fd)
        command = ['dynfilefs', '--compact' if compact else '--reclaim',
                   '/proc/self/fd/{}'.format(image_fd)]
        result = subprocess.run(command, pass_fds=(image_fd,), stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE)
        if result.returncode:
            raise OSError(result.stderr.decode(errors='replace').strip() or
                          _('Space reclamation did not complete.'))
        return {'complete': True, 'storage_format': 'dynfilefs', 'compact': compact}
    finally:
        for fd in (image_fd, mount_fd, loop_fd):
            if fd is not None:
                os.close(fd)


def main():
    try:
        if len(sys.argv) != 5 or sys.argv[3] not in ('none', 'luks') or sys.argv[4] not in ('reclaim', 'compact'):
            raise ValueError('Invalid DynFileFS worker arguments')
        manager = SessionManager.__new__(SessionManager)
        manager.sessions_dir = sys.argv[1]
        session_id = manager._validate_session_id(sys.argv[2])
        result = reclaim(manager, session_id, sys.argv[3], sys.argv[4] == 'compact')
        print(json.dumps(result))
        return 0
    except Exception as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
