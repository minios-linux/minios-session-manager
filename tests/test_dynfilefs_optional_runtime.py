"""MiniOS Bionic uses DynFileFS from initrd; a root package is optional."""

import contextlib
import os
import stat
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
import minios_session as cli


@pytest.fixture
def no_helper():
    # Do not monkeypatch os.stat globally across pytest's tmp_path setup.
    with patch.object(cli, 'dynfilefs_executable', return_value=None):
        yield


def fs_info():
    return {'type': 'ext4', 'is_readonly': False, 'is_posix_compatible': True}


def make_manager():
    manager = cli.SessionManager.__new__(cli.SessionManager)
    manager._check_dynblk_available = lambda: False
    manager._detect_filesystem_type = lambda: (fs_info(), None)
    return manager


def fake_initrd_mode(permissions=0o755, uid=0):
    return SimpleNamespace(st_mode=stat.S_IFREG | permissions, st_uid=uid)


def test_boot_initrd_binary_is_fallback_when_system_package_is_absent(monkeypatch):
    binary = '/run/initramfs/bin/dynfilefs'
    monkeypatch.setattr(cli, 'INITRD_DYNFILEFS_EXECUTABLE', binary)
    with patch.object(cli.os, 'stat', return_value=fake_initrd_mode()) as stat_call, \
         patch.object(cli.os, 'access', return_value=True), \
         patch.object(cli.shutil, 'which', return_value=None) as which:
        assert cli.dynfilefs_executable() == binary
        assert cli.require_dynfilefs_executable() == binary
        assert 'dynfilefs' in make_manager()._get_compatible_session_modes(fs_info())
    assert stat_call.call_count == 3
    assert [call[0][0] for call in which.call_args_list] == [
        'dynfilefs', 'mount.dynfilefs'] * 3


def test_system_binary_has_priority_over_existing_initrd():
    with patch.object(cli.shutil, 'which', side_effect=lambda name:
                      '/usr/sbin/dynfilefs' if name == 'dynfilefs' else None) as which, \
         patch.object(cli.os, 'stat') as stat_call:
        assert cli.dynfilefs_executable() == '/usr/sbin/dynfilefs'
        assert cli.require_dynfilefs_executable() == '/usr/sbin/dynfilefs'
        assert 'dynfilefs' in make_manager()._get_compatible_session_modes(fs_info())
    stat_call.assert_not_called()
    assert [call[0][0] for call in which.call_args_list] == ['dynfilefs'] * 3


def test_installed_mount_helper_has_priority_over_existing_initrd():
    with patch.object(cli.shutil, 'which', side_effect=lambda name:
                      '/usr/sbin/mount.dynfilefs' if name == 'mount.dynfilefs' else None), \
         patch.object(cli.os, 'stat') as stat_call:
        assert cli.dynfilefs_executable() == '/usr/sbin/mount.dynfilefs'
    stat_call.assert_not_called()


def test_system_helper_fallback_without_initrd_copy():
    with patch.object(cli.os, 'stat', side_effect=FileNotFoundError), \
         patch.object(cli.shutil, 'which', side_effect=lambda name: '/usr/sbin/dynfilefs' if name == 'dynfilefs' else None):
        assert cli.require_dynfilefs_executable() == '/usr/sbin/dynfilefs'
        assert 'dynfilefs' in make_manager()._get_compatible_session_modes(fs_info())


def test_mount_helper_is_a_supported_system_fallback():
    with patch.object(cli.os, 'stat', side_effect=FileNotFoundError), \
         patch.object(cli.shutil, 'which', side_effect=lambda name: '/usr/sbin/mount.dynfilefs' if name == 'mount.dynfilefs' else None):
        assert cli.require_dynfilefs_executable() == '/usr/sbin/mount.dynfilefs'


@pytest.mark.parametrize('state,can_execute', [
    (fake_initrd_mode(0o777), True),
    (fake_initrd_mode(0o755, uid=1000), True),
    (fake_initrd_mode(0o644), True),
    (fake_initrd_mode(0o755), False),
    (SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_uid=0), True),
])
def test_untrusted_or_unusable_initrd_helper_is_not_run(state, can_execute):
    with patch.object(cli.os, 'stat', return_value=state), \
         patch.object(cli.os, 'access', return_value=can_execute), \
         patch.object(cli.shutil, 'which', return_value=None):
        assert cli.dynfilefs_executable() is None
        with pytest.raises(OSError, match='initrd'):
            cli.require_dynfilefs_executable()


def test_no_helper_keeps_other_modes_and_hides_dynfilefs(no_helper):
    manager = make_manager()
    assert not manager._check_dynfilefs_available()
    modes = manager._get_compatible_session_modes(fs_info())
    assert modes == ['native', 'squashfs', 'raw']
    assert manager._validate_target_mode('raw') == (True, None)
    success, error = manager._validate_target_mode('dynfilefs')
    assert success is False and 'initrd' in error and 'install dynfilefs package' not in error


def test_existing_dynfilefs_session_can_be_activated_without_helper(no_helper, tmp_path):
    source = tmp_path / '1'
    source.mkdir()
    manager = make_manager()
    manager.sessions_dir = str(tmp_path)
    manager._session_path = lambda *a, **kw: str(source)
    manager._session_lease = lambda *a: contextlib.ExitStack()
    manager._mutation_lock = contextlib.ExitStack
    manager._normalize_metadata = lambda metadata: metadata
    manager._session_configuration_supported = lambda data: True
    record = {'default': '2', 'sessions': {'1': {'mode': 'dynfilefs', 'encryption': 'none'}}}
    manager._read_sessions_metadata = lambda: record
    manager._write_sessions_metadata = Mock(return_value=True)
    success, message = manager.activate_session('1')
    assert success, message
    assert record['default'] == '1'
    manager._write_sessions_metadata.assert_called_once()


def test_missing_helper_fails_upgrade_before_creating_new_session(no_helper, tmp_path):
    source = tmp_path / '1'
    source.mkdir()
    (source / 'changes.dat').write_bytes(b'original')
    manager = make_manager()
    @contextlib.contextmanager
    def lease(*args):
        yield str(source)
    manager._session_lease = lease
    manager._mutation_lock = contextlib.ExitStack
    manager._read_sessions_metadata = lambda: {
        'default': '1', 'sessions': {'1': {'mode': 'dynfilefs', 'encryption': 'none'}}}
    manager._session_configuration_supported = lambda data: True
    manager.get_running_session = lambda: None
    manager._reserve_session = Mock()
    with patch.object(cli.subprocess, 'run') as run:
        success, error, details = manager.upgrade_dynfilefs_session('1')
    assert not success and 'initrd' in error and not details
    assert (source / 'changes.dat').read_bytes() == b'original'
    manager._reserve_session.assert_not_called()
    run.assert_not_called()


def test_missing_helper_does_not_start_reclaim(no_helper):
    manager = make_manager()
    with patch.object(cli.subprocess, 'run') as run:
        with pytest.raises(OSError, match='initrd'):
            manager._check_dynfilefs_reclaim()
    run.assert_not_called()


def test_version_probe_uses_retained_binary_without_a_package():
    args = []
    def command(argv, **kwargs):
        args.append(argv)
        return SimpleNamespace(returncode=1, stdout=b'--reclaim --compact', stderr=b'')
    manager = make_manager()
    with patch.object(cli, 'require_dynfilefs_executable', return_value='/run/initramfs/bin/dynfilefs'), \
         patch.object(cli.subprocess, 'run', side_effect=command):
        manager._check_dynfilefs_reclaim()
    assert args == [['/run/initramfs/bin/dynfilefs']]
