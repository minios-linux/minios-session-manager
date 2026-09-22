import contextlib
import stat
import struct
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
import minios_session as cli


def result(stdout=b'', code=0):
    return SimpleNamespace(returncode=code, stdout=stdout, stderr=b'')


@contextlib.contextmanager
def unlocked():
    yield


def manager_for(tmp_path):
    manager = cli.SessionManager.__new__(cli.SessionManager)
    manager.sessions_dir = str(tmp_path)
    manager._mutation_lock = unlocked
    manager._session_path = lambda *a, **kw: str(tmp_path)
    manager._read_sessions_metadata = lambda: {'sessions': {'1': {'mode': 'dynfilefs'}}}
    manager._invalidate_size_cache = Mock()
    manager._safe_unmount = Mock(return_value=True)
    manager._trim_block_filesystem = Mock()
    return manager


@pytest.mark.parametrize('compact', [False, True])
@pytest.mark.parametrize('encryption', ['none', 'luks'])
def test_detached_reclaim_owns_mounts_and_preserves_encryption_policy(tmp_path, compact, encryption):
    manager = manager_for(tmp_path)
    manager._read_sessions_metadata = lambda: {'sessions': {'1': {'mode': 'dynfilefs', 'encryption': encryption}}}
    image = tmp_path / 'virtual.dat'
    image.write_bytes(b'payload')
    events = []

    @contextlib.contextmanager
    def expose(path):
        events.append('open')
        yield str(image)
        events.append('close')

    manager._dynfilefs_reclaim_image = expose
    manager._get_dynfilefs_size = Mock(side_effect=[8192, 4096])
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        if args == ['dynfilefs']:
            return result(b'--reclaim --compact', 1)
        if args[0] == 'findmnt':
            return result(b'/dev/loop7\n')
        if args[0] == 'dynfilefs':
            assert kwargs['pass_fds']
            assert args[2].startswith('/proc/self/fd/')
        return result()

    with patch.object(cli.subprocess, 'run', side_effect=run):
        success, message, details = manager.reclaim_session('1', compact=compact)
    assert success, message
    assert details['freed_bytes'] == 4096
    assert details['complete'] is True
    assert events == ['open', 'close']
    assert calls[-1][1] == ('--compact' if compact else '--reclaim')
    assert manager._trim_block_filesystem.called is (encryption == 'none')
    assert manager._safe_unmount.called is (encryption == 'none')
    assert not any('cryptsetup' in args or '--scan-zeroes' in args for args in calls)


def test_running_dynfilefs_uses_online_worker(tmp_path):
    manager = manager_for(tmp_path)
    manager._read_sessions_metadata = lambda: {'running': '1', 'sessions': {'1': {'mode': 'dynfilefs'}}}
    manager._reclaim_running_dynfilefs = Mock(return_value={'complete': True})
    manager._reclaim_dynfilefs = Mock()
    with patch.object(cli.subprocess, 'run') as run:
        success, message, _ = manager.reclaim_session('1')
    assert success, message
    manager._reclaim_running_dynfilefs.assert_called_once_with('1', str(tmp_path), 'none', False)
    manager._reclaim_dynfilefs.assert_not_called()
    run.assert_not_called()


def test_old_backend_rejected_before_exposure(tmp_path):
    manager = manager_for(tmp_path)
    manager._dynfilefs_reclaim_image = Mock()
    with patch.object(cli.subprocess, 'run', return_value=result(b'old usage', 1)):
        success, message, _ = manager.reclaim_session('1')
    assert not success and '4.6.0' in message
    manager._dynfilefs_reclaim_image.assert_not_called()


def test_online_reclaim_reexecutes_the_same_backend(tmp_path):
    manager = manager_for(tmp_path)
    manager._check_dynfilefs_reclaim = Mock()
    manager._running_persistence_state = Mock(return_value={})
    manager._get_dynfilefs_size = Mock(side_effect=[8192, 4096])
    with patch.object(cli.subprocess, 'run', return_value=result(b'{"complete":true}')) as run:
        details = manager._reclaim_running_dynfilefs('1', str(tmp_path), 'none', True)
    assert run.call_args[0][0] == [
        cli.sys.executable, cli.os.path.realpath(cli.__file__),
        '--internal-dynfilefs-reclaim', str(tmp_path), '1', 'none', 'compact']
    assert details['freed_bytes'] == 4096


def test_internal_reclaim_validates_arguments_before_namespace():
    with patch.object(cli.SessionManager, '_reclaim_dynfilefs_in_namespace') as reclaim:
        assert cli._dynfilefs_reclaim_child([]) == 1
        assert cli._dynfilefs_reclaim_child(['/tmp', '1', 'none', 'invalid']) == 1
    reclaim.assert_not_called()


def test_trim_failure_prevents_compaction(tmp_path):
    manager = manager_for(tmp_path)
    image = tmp_path / 'virtual.dat'
    image.touch()

    @contextlib.contextmanager
    def expose(path):
        yield str(image)

    manager._dynfilefs_reclaim_image = expose
    manager._trim_block_filesystem.side_effect = OSError('trim failed')
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        return result(b'--reclaim --compact' if args == ['dynfilefs'] else b'/dev/loop7\n')

    with patch.object(cli.subprocess, 'run', side_effect=run):
        success, message, _ = manager.reclaim_session('1', compact=True)
    assert not success and 'trim failed' in message
    assert not any('--compact' in args for args in calls)
    manager._safe_unmount.assert_called_once()


def test_busy_fuse_mount_keeps_daemon(tmp_path):
    manager = manager_for(tmp_path)
    manager._wait_for_mount = Mock(return_value=True)
    manager._safe_unmount.return_value = False
    manager._cleanup_process = Mock()
    with patch.object(cli.subprocess, 'Popen'), \
         patch.object(cli.tempfile, 'mkdtemp', return_value=str(tmp_path)), \
         patch.object(cli.os.path, 'ismount', return_value=True):
        with pytest.raises(OSError, match='left running'):
            with manager._dynfilefs_reclaim_image(str(tmp_path)):
                pass
    manager._cleanup_process.assert_not_called()


def test_usage_counts_allocations_and_only_storage_members(tmp_path):
    manager = manager_for(tmp_path)
    (tmp_path / 'changes.dat').write_bytes(b'header')
    with (tmp_path / 'changes.dat.0').open('wb') as stream:
        stream.truncate(16 * 1024 * 1024)
    (tmp_path / 'changes.dat.backup').write_bytes(b'x' * 8192)
    expected = sum((tmp_path / name).stat().st_blocks * 512 for name in ('changes.dat', 'changes.dat.0'))
    assert manager._get_dynfilefs_size(str(tmp_path)) == expected
    assert expected < 16 * 1024 * 1024


def test_trim_uses_exact_descriptor_ioctl_not_path_resolution(tmp_path):
    manager = manager_for(tmp_path)
    libc = Mock()
    libc.syncfs.return_value = 0
    with patch.object(cli.os, 'stat', return_value=SimpleNamespace(st_mode=stat.S_IFBLK, st_rdev=7)), \
         patch.object(cli.os, 'fstat', return_value=SimpleNamespace(st_dev=7)), \
         patch.object(cli.ctypes, 'CDLL', return_value=libc), \
         patch.object(cli.fcntl, 'ioctl') as ioctl, \
         patch.object(cli.subprocess, 'run') as run:
        manager._trim_block_filesystem_fd('/dev/loop7', 42)
    libc.syncfs.assert_called_once_with(42)
    fd, request, data, mutate = ioctl.call_args[0]
    assert (fd, request, mutate) == (42, 0xc0185879, True)
    assert struct.unpack('=QQQ', data) == (0, (1 << 64) - 1, 0)
    run.assert_not_called()


@pytest.mark.parametrize('failure', ['sync', 'trim', 'identity'])
def test_trim_errors_propagate(tmp_path, failure):
    manager = manager_for(tmp_path)
    libc = Mock()
    libc.syncfs.return_value = -1 if failure == 'sync' else 0
    with patch.object(cli.os, 'stat', return_value=SimpleNamespace(st_mode=stat.S_IFBLK, st_rdev=7)), \
         patch.object(cli.os, 'fstat', return_value=SimpleNamespace(st_dev=8 if failure == 'identity' else 7)), \
         patch.object(cli.ctypes, 'CDLL', return_value=libc), \
         patch.object(cli.fcntl, 'ioctl', side_effect=OSError('trim error')) as ioctl:
        with pytest.raises(OSError):
            manager._trim_block_filesystem_fd('/dev/loop7', 42)
    assert ioctl.called is (failure == 'trim')


def mounts():
    lower = dict(id='1', parent='0', dev='0:55', root='/', path='/changes', rw=True,
                 fs='fuse.mount.dynfilefs', source='mount.dynfilefs')
    upper = dict(id='2', parent='1', dev='7:0', root='/', path='/changes', rw=True,
                 fs='ext4', source='/dev/loop0')
    return lower, upper


@pytest.mark.parametrize('bad', [None, 'readonly', 'parent', 'ambiguous', 'path'])
def test_mount_stack_requires_exact_writable_parent_pair(bad):
    lower, upper = mounts()
    entries = [lower, upper]
    if bad == 'readonly':
        upper['rw'] = False
    elif bad == 'parent':
        upper['parent'] = 'other'
    elif bad == 'ambiguous':
        entries.append(dict(upper))
    elif bad == 'path':
        upper['path'] = '/other'
    if bad:
        with pytest.raises(OSError):
            cli.SessionManager._select_dynfilefs_mounts(entries, ('/changes',))
    else:
        assert cli.SessionManager._select_dynfilefs_mounts(entries, ('/changes',)) == (lower, upper)


@pytest.mark.parametrize('flags,offset,limit', [(0, 0, 0), (1, 0, 0), (0, 512, 0), (0, 0, 512)])
def test_loop_identity_rejects_readonly_and_partial_images(flags, offset, limit):
    def ioctl(fd, request, data, mutate):
        assert request == cli.SessionManager.LOOP_GET_STATUS64
        struct.pack_into('=QQQQQIIII', data, 0, 55, 2, 0, offset, limit, 0, 0, 0, flags)
    with patch.object(cli.fcntl, 'ioctl', side_effect=ioctl):
        if flags or offset or limit:
            with pytest.raises(OSError):
                cli.SessionManager._reclaim_loop_identity(42)
        else:
            assert cli.SessionManager._reclaim_loop_identity(42) == (55, 2)


def test_namespace_failure_cannot_detach_any_mount(tmp_path):
    manager = manager_for(tmp_path)
    manager._running_persistence_state = Mock(return_value={})
    with patch.object(manager, '_private_reclaim_namespace', side_effect=OSError('unshare failed')), \
         patch.object(manager, '_reclaim_mount_stack') as stack, \
         patch.object(cli.ctypes, 'CDLL') as libc:
        with pytest.raises(OSError, match='unshare failed'):
            manager._reclaim_dynfilefs_in_namespace('1', 'none', True)
    stack.assert_not_called()
    libc.assert_not_called()


def test_old_running_daemon_fails_before_trim(tmp_path):
    manager = manager_for(tmp_path)
    manager._running_persistence_state = Mock(return_value={})
    manager._trim_block_filesystem_fd = Mock()
    lower, upper = mounts()
    lower['dev'] = '0:55'
    libc = Mock()
    libc.umount2.return_value = 0
    stats = [SimpleNamespace(st_mode=stat.S_IFBLK, st_rdev=7),
             SimpleNamespace(st_dev=7),
             SimpleNamespace(st_mode=stat.S_IFREG, st_dev=55, st_ino=2, st_size=65536)]
    upper['dev'] = '0:7'
    with patch.object(manager, '_private_reclaim_namespace'), \
         patch.object(manager, '_reclaim_mount_stack', return_value=[lower, upper]), \
         patch.object(manager, 'DYNFILEFS_CHANGES_PATHS', ('/changes',)), \
         patch.object(manager, '_reclaim_loop_identity', return_value=(55, 2)), \
         patch.object(cli.os, 'open', side_effect=[101, 102, 103]), \
         patch.object(cli.os, 'fstat', side_effect=stats), \
         patch.object(cli.os, 'close'), \
         patch.object(cli.ctypes, 'CDLL', return_value=libc), \
         patch.object(cli.fcntl, 'ioctl', side_effect=OSError('ENOTTY')), \
         patch.object(cli.subprocess, 'run') as run:
        with pytest.raises(OSError, match='running DynFileFS daemon'):
            manager._reclaim_dynfilefs_in_namespace('1', 'none', True)
    manager._trim_block_filesystem_fd.assert_not_called()
    run.assert_not_called()
