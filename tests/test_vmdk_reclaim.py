"""Client integration contracts; all device operations are mocked."""
import contextlib
import fcntl
import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
import minios_session as cli


def result(stdout=b''):
    return SimpleNamespace(returncode=0, stdout=stdout, stderr=b'')


@contextlib.contextmanager
def null_context():
    """Python 3.6-compatible replacement for contextlib.nullcontext."""
    yield


@pytest.mark.parametrize('mode,name', [('dynblk', 'volume000.db'), ('vmdk', 'volume.vmdk')])
def test_create_selects_storage_format(tmp_path, mode, name):
    manager = cli.SessionManager.__new__(cli.SessionManager)
    manager._get_dynblk_compression_codecs = lambda: ('none',)
    manager._run_dynblk = Mock(return_value=result(b'/dev/dynblk9\n'))
    with patch.object(cli.subprocess, 'run', return_value=result()):
        success, message = manager._create_dynblk_session(str(tmp_path), 64, storage_format=mode)
    assert success, message
    args = manager._run_dynblk.call_args[0][0]
    assert args[1] == str(tmp_path / name)
    assert args[args.index('--format') + 1] == mode


@pytest.mark.parametrize('mode', ['dynblk', 'vmdk'])
def test_capacity_metadata_is_always_mib(tmp_path, mode):
    manager = cli.SessionManager.__new__(cli.SessionManager)
    info = manager._get_session_size_info(str(tmp_path), {'mode': mode, 'size': 2 * 1024 * 1024})
    assert info['total_size'] == 2 << 40


def test_vmdk_clone_locks_descriptor_and_copies_all_parts(tmp_path):
    manager = cli.SessionManager.__new__(cli.SessionManager)
    source, target = tmp_path / 'source', tmp_path / 'target'
    source.mkdir(); target.mkdir()
    names = ['volume.vmdk', 'volume-s001.vmdk', 'volume-s1000.vmdk']
    for name in names:
        (source / name).write_bytes(name.encode())
    with (source / names[0]).open('rb') as descriptor:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert not manager._copy_session_direct(str(source), str(target), 'vmdk')
    assert manager._copy_session_direct(str(source), str(target), 'vmdk')
    assert sorted(p.name for p in target.iterdir()) == sorted(names)
    for name in names:
        assert (target / name).read_bytes() == name.encode()


def test_mixed_image_namespace_is_refused(tmp_path):
    (tmp_path / 'volume000.db').touch(); (tmp_path / 'volume.vmdk').touch()
    with pytest.raises(OSError, match='Ambiguous'):
        cli.SessionManager._dynblk_volume(str(tmp_path))


@pytest.mark.parametrize('mode', ['dynblk', 'vmdk'])
@pytest.mark.parametrize('running,compact,encryption', [(False, False, 'none'), (True, True, 'none'), (False, False, 'luks')])
def test_reclaim_ownership_and_opt_in_compaction(tmp_path, mode, running, compact, encryption):
    manager = cli.SessionManager.__new__(cli.SessionManager)
    manager.sessions_dir = str(tmp_path)
    manager._mutation_lock = null_context
    manager._read_sessions_metadata = lambda: {
        'sessions': {'1': {'mode': mode, 'encryption': encryption}},
        'running': '1' if running else None}
    manager._session_path = lambda *a, **kw: str(tmp_path)
    manager._running_block_device = Mock(return_value='/dev/dynblk9')
    manager._block_filesystem_mount = Mock(return_value='/mock/changes')
    manager._trim_block_filesystem = Mock()
    manager._dynblk_status = lambda device: {'storage_format': mode, 'cache': 'writeback'}
    manager._invalidate_size_cache = Mock()
    manager._safe_unmount = Mock(return_value=True)
    calls = []
    def backend(args):
        calls.append(args)
        if args[0] == 'load':
            return result(b'/dev/dynblk9\n')
        if args[0] == 'reclaim':
            return result(json.dumps({'complete': True} if '--execute' in args else {'dry_run': True}).encode())
        return result()
    manager._run_dynblk = backend
    temporary = tmp_path / 'temporary-mount'
    temporary.mkdir()
    with patch.object(cli.os, 'open', return_value=999), \
         patch.object(cli.os, 'close'), \
         patch.object(cli.os, 'fstat', return_value=SimpleNamespace(st_mode=stat.S_IFBLK)), \
         patch.object(cli.tempfile, 'mkdtemp', return_value=str(temporary)), \
         patch.object(cli.subprocess, 'run', return_value=result()):
        success, message, details = manager.reclaim_session('1', compact=compact)
    assert success, message
    assert details['complete'] is True
    executed = [a for a in calls if a[0] == 'reclaim' and '--execute' in a]
    assert len(executed) == 1
    assert ('--compact' in executed[0]) is compact
    assert any(a[0] == 'unload' for a in calls) is (not running)
    assert manager._trim_block_filesystem.called is (encryption == 'none')
    assert not any('cryptsetup' in str(a) for a in calls)


def test_mount_escape_decoding():
    assert cli.SessionManager._decode_mount_field(r'/mnt/a\040b') == '/mnt/a b'


def test_vmdk_compression_is_rejected_without_creating_storage(tmp_path):
    manager = cli.SessionManager.__new__(cli.SessionManager)
    manager._get_dynblk_compression_codecs = lambda: ('none', 'lz4')
    manager._run_dynblk = Mock()
    success, _message = manager._create_dynblk_session(
        str(tmp_path), 64, compression='lz4', storage_format='vmdk')
    assert not success
    manager._run_dynblk.assert_not_called()


@pytest.mark.parametrize('wrong_key', [None, 'boot_id', 'sessions_inode'])
def test_running_reclaim_binds_to_this_boot_and_store(tmp_path, wrong_key):
    manager = cli.SessionManager.__new__(cli.SessionManager)
    manager.sessions_dir = str(tmp_path)
    manager.BOOT_STATE_FILE = str(tmp_path / 'boot-state')
    manager.BOOT_ID_FILE = str(tmp_path / 'boot-id')
    boot = '11111111-2222-3333-4444-555555555555'
    Path(manager.BOOT_ID_FILE).write_text(boot)
    store = tmp_path.stat()
    state = {'boot_id': boot, 'boot_level': 'ok', 'session': '1', 'mode': 'vmdk',
             'encryption': 'none', 'durable': '1', 'writable': '1',
             'sessions_device': str(store.st_dev), 'sessions_inode': str(store.st_ino),
             'dynblk_device': '/dev/dynblk9'}
    if wrong_key:
        state[wrong_key] = 'invalid'
    Path(manager.BOOT_STATE_FILE).write_text(''.join(f'{k}={v}\n' for k, v in state.items()))
    trusted = SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_uid=0, st_nlink=1, st_size=1000)
    with patch.object(cli.os, 'fstat', return_value=trusted):
        if wrong_key:
            with pytest.raises(OSError, match='this boot'):
                manager._running_block_device('1', 'vmdk', 'none')
        else:
            assert manager._running_block_device('1', 'vmdk', 'none') == '/dev/dynblk9'


@pytest.mark.parametrize('mode,name', [('dynblk', 'volume000.db'), ('vmdk', 'volume.vmdk')])
@pytest.mark.parametrize('writable', [False, True])
def test_image_mount_pins_format_and_readonly(tmp_path, mode, name, writable):
    manager = cli.SessionManager.__new__(cli.SessionManager)
    (tmp_path / name).touch()
    manager._run_dynblk = Mock(return_value=result(b'/dev/dynblk9\n'))
    manager._safe_unmount = Mock(return_value=True)
    manager._safe_rmtree = Mock()
    with patch.object(cli.tempfile, 'mkdtemp', return_value=str(tmp_path / 'mnt')), \
         patch.object(cli.subprocess, 'run', return_value=result()):
        with manager._mount_dynblk(str(tmp_path), writable, storage_format=mode):
            pass
    args = manager._run_dynblk.call_args[0][0]
    assert args[args.index('--format') + 1] == mode
    assert ('--read-only' in args) is (not writable)


def test_gui_reclaim_reports_backend_failure():
    from minios_session_manager import SessionManagerGUI
    gui = SessionManagerGUI.__new__(SessionManagerGUI)
    gui._show_loading = Mock()
    gui.refresh_session_list = Mock()
    gui._show_info = Mock()
    gui._show_error = Mock()
    gui._on_reclaim_complete(True, '{"success":false,"message":"failed"}', '')
    gui._show_info.assert_not_called()
    gui._show_error.assert_called_once_with('failed')
