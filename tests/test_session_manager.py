#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tests for minios_session SessionManager class.
"""

import sys
import os
import io
import json
import hashlib
import errno
import stat
import subprocess
import pytest
import tempfile
import shutil
from types import SimpleNamespace
from unittest.mock import patch, MagicMock, mock_open

# Add lib directory to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'lib'))


def test_luks_password_stdin_confirmation(monkeypatch):
    from minios_session import luks_password_from_args

    monkeypatch.setattr(sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(b'secret\nsecret\n')))
    assert luks_password_from_args(SimpleNamespace(password_stdin=True), confirm=True) == b'secret'


def test_luks_password_stdin_confirmation_rejects_mismatch(monkeypatch):
    from minios_session import luks_password_from_args

    monkeypatch.setattr(sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(b'secret\nother\n')))
    with pytest.raises(ValueError):
        luks_password_from_args(SimpleNamespace(password_stdin=True), confirm=True)


class TestSessionManagerInit:
    """Tests for SessionManager initialization."""

    def test_init_with_custom_dir(self, temp_sessions_dir):
        """Test initialization with custom sessions directory."""
        from minios_session import SessionManager
        
        with patch('os.path.exists', return_value=True):
            sm = SessionManager(custom_sessions_dir=temp_sessions_dir)
            assert sm.custom_sessions_dir == temp_sessions_dir

    def test_init_creates_cache_dir(self, temp_sessions_dir):
        """Test that cache directory is created on init."""
        from minios_session import SessionManager
        
        with patch('os.path.exists', side_effect=lambda p: p == temp_sessions_dir):
            sm = SessionManager(custom_sessions_dir=temp_sessions_dir)
            assert sm.cache_dir is not None

    def test_cache_symlink_does_not_chmod_target(self, temp_sessions_dir):
        from minios_session import SessionManager

        victim = tempfile.mkdtemp(prefix='minios-cache-victim-')
        cache_link = os.path.join(temp_sessions_dir, 'cache-link')
        os.chmod(victim, 0o777)
        os.symlink(victim, cache_link)
        sm = SessionManager.__new__(SessionManager)
        sm.cache_dir = cache_link
        sm.cache_file = os.path.join(cache_link, 'session_sizes.json')
        try:
            sm._ensure_cache_dir()
            assert stat.S_IMODE(os.stat(victim).st_mode) == 0o777
            assert sm.cache_dir != cache_link
            assert not os.path.islink(sm.cache_dir)
        finally:
            if sm.cache_dir != cache_link:
                shutil.rmtree(sm.cache_dir, ignore_errors=True)
            shutil.rmtree(victim, ignore_errors=True)


class TestFormatSize:
    """Tests for _format_size method."""

    def test_format_bytes(self, temp_sessions_dir):
        """Test formatting byte values."""
        from minios_session import SessionManager
        
        with patch('os.path.exists', return_value=True):
            sm = SessionManager(custom_sessions_dir=temp_sessions_dir)
            
            assert sm._format_size(0) == "0.0B"
            assert sm._format_size(500) == "500.0B"

    def test_format_kilobytes(self, temp_sessions_dir):
        """Test formatting kilobyte values."""
        from minios_session import SessionManager
        
        with patch('os.path.exists', return_value=True):
            sm = SessionManager(custom_sessions_dir=temp_sessions_dir)
            
            result = sm._format_size(1024)
            assert "KB" in result or "K" in result

    def test_format_megabytes(self, temp_sessions_dir):
        """Test formatting megabyte values."""
        from minios_session import SessionManager
        
        with patch('os.path.exists', return_value=True):
            sm = SessionManager(custom_sessions_dir=temp_sessions_dir)
            
            result = sm._format_size(1024 * 1024)
            assert "MB" in result or "M" in result

    def test_format_gigabytes(self, temp_sessions_dir):
        """Test formatting gigabyte values."""
        from minios_session import SessionManager
        
        with patch('os.path.exists', return_value=True):
            sm = SessionManager(custom_sessions_dir=temp_sessions_dir)
            
            result = sm._format_size(1024 * 1024 * 1024)
            assert "GB" in result or "G" in result


class TestGetCurrentUnionFs:
    """Tests for _get_current_union_fs method."""

    def test_detect_observed_aufs_root(self, temp_sessions_dir):
        """Test detecting AUFS from the effective root mount."""
        from minios_session import SessionManager

        mountinfo = '36 25 0:32 / / rw,relatime - aufs none rw\n'
        with patch('os.path.exists', return_value=True), \
             patch('builtins.open', side_effect=lambda *_args, **_kwargs: io.StringIO(mountinfo)):
            sm = SessionManager(custom_sessions_dir=temp_sessions_dir)
            result = sm._get_current_union_fs()
            assert result == 'aufs'

    def test_detect_observed_overlay_root(self, temp_sessions_dir):
        """Test normalizing the kernel's overlay mount name."""
        from minios_session import SessionManager

        mountinfo = '36 25 0:32 / / rw,relatime - overlay overlay rw\n'
        with patch('os.path.exists', return_value=True), \
             patch('builtins.open', side_effect=lambda *_args, **_kwargs: io.StringIO(mountinfo)):
            sm = SessionManager(custom_sessions_dir=temp_sessions_dir)
            result = sm._get_current_union_fs()
            assert result == 'overlayfs'

    def test_does_not_treat_supported_union_as_active(self, temp_sessions_dir):
        """Kernel support or command-line intent is not the observed root."""
        from minios_session import SessionManager

        mountinfo = '36 25 8:1 / / rw,relatime - ext4 /dev/sda1 rw\n'
        with patch('os.path.exists', return_value=True), \
             patch('builtins.open', side_effect=lambda *_args, **_kwargs: io.StringIO(mountinfo)):
            sm = SessionManager(custom_sessions_dir=temp_sessions_dir)
            result = sm._get_current_union_fs()
            assert result == 'unknown'


class TestGetSystemVersion:
    """Tests for _get_system_version method."""

    def test_read_version_from_release_file(self, temp_sessions_dir, sample_minios_release):
        """Test reading version from minios-release file."""
        from minios_session import SessionManager
        
        def exists_side_effect(path):
            return path in [temp_sessions_dir, '/etc/minios-release']
        
        with patch('os.path.exists', side_effect=exists_side_effect), \
             patch('builtins.open', side_effect=lambda *_args, **_kwargs: io.StringIO(sample_minios_release)):
            sm = SessionManager(custom_sessions_dir=temp_sessions_dir)
            version = sm._get_system_version()
            assert version == "5.1.1"

    def test_version_unknown_when_file_missing(self, temp_sessions_dir):
        """Test returning 'unknown' when release file is missing."""
        from minios_session import SessionManager
        
        def exists_side_effect(path):
            return path == temp_sessions_dir
        
        with patch('os.path.exists', side_effect=exists_side_effect):
            sm = SessionManager(custom_sessions_dir=temp_sessions_dir)
            version = sm._get_system_version()
            assert version == "unknown"


class TestGetSystemEdition:
    """Tests for _get_system_edition method."""

    def test_read_edition_from_release_file(self, temp_sessions_dir, sample_minios_release):
        """Test reading edition from minios-release file."""
        from minios_session import SessionManager
        
        def exists_side_effect(path):
            return path in [temp_sessions_dir, '/etc/minios-release']
        
        with patch('os.path.exists', side_effect=exists_side_effect), \
             patch('builtins.open', side_effect=lambda *_args, **_kwargs: io.StringIO(sample_minios_release)):
            sm = SessionManager(custom_sessions_dir=temp_sessions_dir)
            edition = sm._get_system_edition()
            assert edition == "standard"


class TestCheckFreeSpace:
    """Tests for _check_free_space method."""

    def test_sufficient_space(self, temp_sessions_dir):
        """Test when there is sufficient disk space."""
        from minios_session import SessionManager
        
        # Mock statvfs to return plenty of space
        mock_stat = MagicMock()
        mock_stat.f_bavail = 1000000
        mock_stat.f_frsize = 4096  # ~4GB free
        
        with patch('os.path.exists', return_value=True), \
             patch('os.path.isfile', return_value=False), \
             patch('os.statvfs', return_value=mock_stat):
            sm = SessionManager(custom_sessions_dir=temp_sessions_dir)
            has_space, error = sm._check_free_space(temp_sessions_dir, 100)
            assert has_space is True
            assert error is None

    def test_insufficient_space(self, temp_sessions_dir):
        """Test when there is insufficient disk space."""
        from minios_session import SessionManager
        
        # Mock statvfs to return very little space
        mock_stat = MagicMock()
        mock_stat.f_bavail = 10
        mock_stat.f_frsize = 4096  # ~40KB free
        
        with patch('os.path.exists', return_value=True), \
             patch('os.path.isfile', return_value=False), \
             patch('os.statvfs', return_value=mock_stat):
            sm = SessionManager(custom_sessions_dir=temp_sessions_dir)
            has_space, error = sm._check_free_space(temp_sessions_dir, 1000)  # Need 1GB
            assert has_space is False
            assert error is not None
            assert "Insufficient" in error


class TestReadSessionsMetadata:
    """Tests for _read_sessions_metadata method."""

    def test_read_json_format(self, temp_sessions_dir, sample_session_json):
        """Test reading JSON format metadata."""
        from minios_session import SessionManager
        
        json_path = os.path.join(temp_sessions_dir, "session.json")
        
        def exists_side_effect(path):
            return path in [temp_sessions_dir, json_path]
        
        with patch('os.path.exists', side_effect=exists_side_effect), \
             patch('builtins.open', mock_open(read_data=json.dumps(sample_session_json))):
            sm = SessionManager(custom_sessions_dir=temp_sessions_dir)
            sm.sessions_file = json_path
            sm.session_format = "json"
            
            metadata = sm._read_sessions_metadata()
            assert metadata["default"] == "001"
            assert "001" in metadata["sessions"]
            assert metadata["sessions"]["001"]["mode"] == "native"

    def test_empty_metadata_when_file_missing(self, temp_sessions_dir):
        """Test returning empty metadata when file is missing."""
        from minios_session import SessionManager
        
        with patch('os.path.exists', side_effect=lambda p: p == temp_sessions_dir):
            sm = SessionManager(custom_sessions_dir=temp_sessions_dir)
            sm.sessions_file = None
            
            metadata = sm._read_sessions_metadata()
            assert metadata == {"default": None, "sessions": {}}


class TestCheckSessionsDirectoryStatus:
    """Tests for check_sessions_directory_status method."""

    def test_directory_not_found(self, temp_sessions_dir):
        """Test status when sessions directory is not found."""
        from minios_session import SessionManager
        
        with patch('os.path.exists', return_value=False):
            sm = SessionManager(custom_sessions_dir="/nonexistent")
            sm.sessions_dir = None
            
            status = sm.check_sessions_directory_status()
            assert status['success'] is False
            assert status['found'] is False

    def test_directory_found_and_writable(self, temp_sessions_dir):
        """Test status when directory exists and is writable."""
        from minios_session import SessionManager
        
        with patch('os.path.exists', return_value=True), \
             patch('subprocess.run') as mock_run, \
             patch('tempfile.NamedTemporaryFile'):
            mock_run.return_value = MagicMock(stdout='ext4\n', returncode=0)
            
            sm = SessionManager(custom_sessions_dir=temp_sessions_dir)
            sm.sessions_dir = temp_sessions_dir
            
            status = sm.check_sessions_directory_status()
            assert status['success'] is True
            assert status['found'] is True
            assert status['writable'] is True


class TestSafeUnmount:
    """Tests for _safe_unmount method."""

    def test_successful_unmount(self, temp_sessions_dir):
        """Test successful unmount operation."""
        from minios_session import SessionManager
        
        with patch('os.path.exists', return_value=True), \
             patch('os.path.ismount', side_effect=[True, False]), \
             patch('subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            
            sm = SessionManager(custom_sessions_dir=temp_sessions_dir)
            result = sm._safe_unmount('/mnt/test')
            assert result is True

    def test_unmount_not_mounted(self, temp_sessions_dir):
        """Test unmount when path is not mounted."""
        from minios_session import SessionManager
        
        with patch('os.path.exists', return_value=True), \
             patch('os.path.ismount', return_value=False):
            sm = SessionManager(custom_sessions_dir=temp_sessions_dir)
            result = sm._safe_unmount('/mnt/test')
            assert result is True

    def test_unmount_with_retries(self, temp_sessions_dir):
        """Test unmount with retries on failure."""
        from minios_session import SessionManager
        
        call_count = [0]
        
        def run_side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] < 3:
                return MagicMock(returncode=1)
            return MagicMock(returncode=0)
        
        with patch('os.path.exists', return_value=True), \
             patch('os.path.ismount', side_effect=[True, True, True, False]), \
             patch('subprocess.run', side_effect=run_side_effect), \
             patch('time.sleep'):
            sm = SessionManager(custom_sessions_dir=temp_sessions_dir)
            result = sm._safe_unmount('/mnt/test', max_retries=5)
            assert result is True
            assert call_count[0] >= 1


class TestSafeRmtree:
    """Tests for _safe_rmtree method."""

    def test_successful_removal(self, temp_sessions_dir):
        """Test successful directory removal."""
        from minios_session import SessionManager
        
        with patch('os.path.exists', return_value=True), \
             patch('shutil.rmtree') as mock_rmtree:
            sm = SessionManager(custom_sessions_dir=temp_sessions_dir)
            result = sm._safe_rmtree('/tmp/testdir')
            assert result is True
            mock_rmtree.assert_called()

    def test_removal_nonexistent_path(self, temp_sessions_dir):
        """Test removal of nonexistent path."""
        from minios_session import SessionManager
        
        def exists_side_effect(path):
            if path == temp_sessions_dir:
                return True
            return False
        
        with patch('os.path.exists', side_effect=exists_side_effect):
            sm = SessionManager(custom_sessions_dir=temp_sessions_dir)
            result = sm._safe_rmtree('/nonexistent/path')
            assert result is True

    def test_removal_failure(self, temp_sessions_dir):
        """Test handling of removal failure."""
        from minios_session import SessionManager
        
        with patch('os.path.exists', return_value=True), \
             patch('shutil.rmtree', side_effect=OSError("Permission denied")), \
             patch('subprocess.run', return_value=MagicMock(returncode=1)):
            sm = SessionManager(custom_sessions_dir=temp_sessions_dir)
            result = sm._safe_rmtree('/tmp/testdir')
            assert result is False


class TestMakeTempDir:
    """Tests for _make_temp_dir method."""

    def test_creates_temp_directory(self, temp_sessions_dir):
        """Test temporary directory creation."""
        from minios_session import SessionManager
        
        created_dirs = []
        
        def makedirs_side_effect(path, **kwargs):
            created_dirs.append(path)
        
        with patch('os.path.exists', return_value=True), \
             patch('os.makedirs', side_effect=makedirs_side_effect):
            sm = SessionManager(custom_sessions_dir=temp_sessions_dir)
            sm.sessions_dir = temp_sessions_dir
            
            result = sm._make_temp_dir()
            assert result.startswith(temp_sessions_dir)
            assert '.tmp_' in result


class TestCliHelp:
    """Tests for help output."""

    def test_main_help_does_not_require_root(self, capsys):
        """Test that CLI help is available without root privileges."""
        from minios_session import main

        with patch.object(sys, 'argv', ['minios-session', '--help']), \
             patch('os.geteuid', return_value=1000), \
             pytest.raises(SystemExit) as exc:
            main()

        captured = capsys.readouterr()

        assert exc.value.code == 0
        assert 'usage:' in captured.out
        assert 'create [MODE] [SIZE]' in captured.out
        assert 'requires root privileges' not in captured.err

    def test_gui_launcher_help(self):
        """Test that GUI launcher prints a usage message."""
        launcher = os.path.join(os.path.dirname(__file__), '..', 'bin', 'minios-session-manager')

        result = subprocess.run(
            [launcher, '--help'],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            check=False,
        )

        assert result.returncode == 0
        assert 'Usage: minios-session-manager' in result.stdout
        assert 'minios-session --help' in result.stdout
        assert result.stderr == ''


class TestAuditRegressions:
    """Security and data-integrity regressions from the session storage audit."""

    def test_gui_cli_uses_python36_text_mode(self):
        import importlib.util
        import types

        gi = types.ModuleType('gi')
        gi.require_version = lambda *_args: None
        repository = types.ModuleType('gi.repository')
        repository.Gdk = MagicMock()
        repository.Gio = MagicMock()
        repository.Gtk = MagicMock()
        repository.GLib = MagicMock()
        repository.Pango = MagicMock()
        minios_gui = types.ModuleType('minios_gui')
        for name in ('StatusBanner', 'apply_minios_css', 'ask_confirmation',
                     'new_header_bar', 'new_icon', 'show_error_dialog',
                     'show_info_dialog'):
            setattr(minios_gui, name, MagicMock())

        module_path = os.path.join(
            os.path.dirname(__file__), '..', 'lib', 'minios_session_manager.py')
        spec = importlib.util.spec_from_file_location(
            'minios_session_manager_python36_test', module_path)
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {
                'gi': gi,
                'gi.repository': repository,
                'minios_gui': minios_gui}):
            spec.loader.exec_module(module)

        gui = object.__new__(module.SessionManagerGUI)
        gui.cli_command = 'minios-session'
        gui._cli_lock = module.threading.Lock()
        gui._cli_process = None
        gui._closing = False
        process = MagicMock(returncode=0)
        process.communicate.return_value = ('output', '')
        with patch.object(module.subprocess, 'Popen', return_value=process) as popen:
            assert gui._run_cli_command(['list', '--json'], 'input') == (
                True, 'output', '')

        kwargs = popen.call_args[1]
        assert kwargs['universal_newlines'] is True
        assert 'text' not in kwargs
        process.communicate.assert_called_once_with(input='input')

    def test_status_reports_missing_sessions_directory(self, tmp_path, capsys):
        import minios_session

        missing = str(tmp_path / 'missing-sessions')
        argv = ['minios-session', '--json', '--sessions-dir', missing, 'status']
        with patch('os.geteuid', return_value=0), patch.object(sys, 'argv', argv):
            minios_session.main()

        captured = capsys.readouterr()
        status = json.loads(captured.out)
        assert captured.err == ''
        assert status['found'] is False
        assert status['writable'] is False
        assert status['sessions_dir'] is None

    def test_gui_ram_only_refresh_does_not_query_missing_sessions(self):
        from minios_session_manager import SessionManagerGUI

        gui = object.__new__(SessionManagerGUI)
        gui._refresh_generation = 0
        gui.sessions_status = {'found': False, 'writable': False}
        gui._run_cli_command = MagicMock()
        gui._process_session_data = MagicMock()

        gui.refresh_session_list()

        gui._run_cli_command.assert_not_called()
        gui._process_session_data.assert_called_once_with(
            1, True, '[]', '', None, None)

    def test_gui_decodes_structured_cli_errors(self):
        from minios_session_manager import SessionManagerGUI

        error = json.dumps({
            'success': False,
            'error': 'Не удалось найти каталог сессий.',
            'details': 'Сохранение сессий не включено.',
        })
        message = SessionManagerGUI._cli_error_text(error)

        assert message == (
            'Не удалось найти каталог сессий.\n\n'
            'Сохранение сессий не включено.')
        assert '\\u' not in message

    def test_session_id_cannot_escape_custom_sessions_dir(self, temp_sessions_dir):
        from minios_session import SessionManager

        sm = SessionManager(custom_sessions_dir=temp_sessions_dir)
        for session_id in ('../outside', '/tmp/outside', '1/../2', ''):
            with pytest.raises(ValueError):
                sm._session_path(session_id)

    def test_reservation_creates_distinct_session_directories(self, temp_sessions_dir):
        from minios_session import SessionManager

        sm = SessionManager(custom_sessions_dir=temp_sessions_dir)
        first_id, first_path = sm._reserve_session()
        second_id, second_path = sm._reserve_session()

        assert (first_id, second_id) == ('1', '2')
        assert os.path.isdir(first_path)
        assert os.path.isdir(second_path)

    def test_free_space_check_fails_closed(self, temp_sessions_dir):
        from minios_session import SessionManager

        sm = SessionManager(custom_sessions_dir=temp_sessions_dir)
        with patch('os.statvfs', side_effect=OSError('unavailable')):
            has_space, error = sm._check_free_space(temp_sessions_dir, 100)

        assert has_space is False
        assert 'unavailable' in error

    def test_readonly_filesystem_has_no_mutable_modes(self, temp_sessions_dir):
        from minios_session import SessionManager

        sm = SessionManager(custom_sessions_dir=temp_sessions_dir)
        assert sm._get_compatible_session_modes({
            'is_readonly': True, 'is_posix_compatible': True, 'type': 'ext4'
        }) == []

    def test_squashfs_mode_requires_exact_staging_filesystem(self,
                                                              temp_sessions_dir):
        from minios_session import SessionManager

        sm = SessionManager(custom_sessions_dir=temp_sessions_dir)
        ext4_modes = sm._get_compatible_session_modes({
            'is_readonly': False, 'is_posix_compatible': True, 'type': 'ext4'
        })
        vfat_modes = sm._get_compatible_session_modes({
            'is_readonly': False, 'is_posix_compatible': False, 'type': 'vfat'
        })
        assert 'squashfs' in ext4_modes
        assert 'squashfs' not in vfat_modes


    def test_fat32_container_limit_matches_perchsize_policy(self, temp_sessions_dir):
        from minios_session import SessionManager

        sm = SessionManager(custom_sessions_dir=temp_sessions_dir)
        assert sm._get_filesystem_limitations({'type': 'vfat'})['max_file_size'] == 4000

    def test_metadata_write_uses_atomic_replace(self, temp_sessions_dir):
        from minios_session import SessionManager

        sm = SessionManager(custom_sessions_dir=temp_sessions_dir)
        sm.sessions_file = os.path.join(temp_sessions_dir, 'session.json')
        sm.session_format = 'json'
        with patch('os.replace', wraps=os.replace) as replace:
            assert sm._write_sessions_metadata({'default': None, 'sessions': {}})

        assert replace.call_count == 2
        replaced_targets = [call[0][1] for call in replace.call_args_list]
        assert sm.sessions_file in replaced_targets
        assert os.path.exists(os.path.join(temp_sessions_dir, 'session.conf'))
        with open(sm.sessions_file, encoding='utf-8') as metadata_file:
            assert json.load(metadata_file) == {'default': None, 'sessions': {}}

    def test_metadata_directory_sync_failure_is_commit_uncertain(self,
                                                                  temp_sessions_dir):
        from minios_session import MetadataCommitUncertain, SessionManager

        sm = SessionManager(custom_sessions_dir=temp_sessions_dir)
        path = os.path.join(temp_sessions_dir, 'session.conf')
        real_fsync = os.fsync

        def fsync(descriptor):
            if stat.S_ISDIR(os.fstat(descriptor).st_mode):
                raise OSError(errno.EIO, 'injected directory sync failure')
            return real_fsync(descriptor)

        with patch('os.fsync', side_effect=fsync), \
             pytest.raises(MetadataCommitUncertain):
            sm._atomic_write_metadata_file(
                path, 'conf', {'default': None, 'sessions': {}})

        assert os.path.exists(path)

    def test_conf_primary_keeps_json_in_sync(self, temp_sessions_dir):
        from minios_session import SessionManager

        sm = SessionManager(custom_sessions_dir=temp_sessions_dir)
        sm.sessions_file = os.path.join(temp_sessions_dir, 'session.conf')
        sm.session_format = 'conf'
        with patch('os.replace', wraps=os.replace) as replace:
            assert sm._write_sessions_metadata({'default': None, 'sessions': {}})

        assert replace.call_count == 2
        with open(os.path.join(temp_sessions_dir, 'session.conf'),
                  encoding='utf-8') as metadata_file:
            assert 'default=' in metadata_file.read()
        with open(os.path.join(temp_sessions_dir, 'session.json'),
                  encoding='utf-8') as metadata_file:
            assert json.load(metadata_file) == {'default': None, 'sessions': {}}

    def test_new_store_writes_equivalent_formats(self, temp_sessions_dir):
        from minios_session import SessionManager

        sm = SessionManager(custom_sessions_dir=temp_sessions_dir)
        assert sm._write_sessions_metadata(
            {'default': '1', 'sessions': {'1': {'mode': 'dynfilefs'}}})
        assert sm.sessions_file == os.path.join(temp_sessions_dir, 'session.json')
        assert sm.session_format == 'json'

        json_path = os.path.join(temp_sessions_dir, 'session.json')
        conf_path = os.path.join(temp_sessions_dir, 'session.conf')
        assert os.path.exists(conf_path)
        assert os.path.exists(json_path)
        with open(conf_path, encoding='utf-8') as mirror:
            content = mirror.read()
        assert 'default=1' in content
        assert 'session_mode[1]=dynfilefs' in content
        with open(json_path, encoding='utf-8') as metadata_file:
            assert json.load(metadata_file)['sessions']['1']['mode'] == 'dynfilefs'

    def test_detection_prefers_json_when_both_formats_exist(self, temp_sessions_dir):
        from minios_session import SessionManager

        with open(os.path.join(temp_sessions_dir, 'session.json'), 'w') as f:
            json.dump({'default': '9', 'sessions': {}}, f)
        with open(os.path.join(temp_sessions_dir, 'session.conf'), 'w') as f:
            f.write('default=1\nsession_mode[1]=native\n')

        sm = SessionManager(custom_sessions_dir=temp_sessions_dir)
        assert sm.sessions_file == os.path.join(temp_sessions_dir, 'session.json')
        assert sm.session_format == 'json'
        assert sm._write_sessions_metadata({'default': '2', 'sessions': {}})
        with open(os.path.join(temp_sessions_dir, 'session.json')) as metadata_file:
            assert json.load(metadata_file)['default'] == '2'
        with open(os.path.join(temp_sessions_dir, 'session.conf')) as metadata_file:
            assert metadata_file.readline().strip() == 'default=2'

    def test_malformed_selected_json_blocks_metadata_mutation(self, temp_sessions_dir):
        from minios_session import SessionManager

        json_path = os.path.join(temp_sessions_dir, 'session.json')
        conf_path = os.path.join(temp_sessions_dir, 'session.conf')
        with open(json_path, 'w') as metadata_file:
            metadata_file.write('{invalid')
        with open(conf_path, 'w') as metadata_file:
            metadata_file.write('default=1\nsession_mode[1]=native\n')

        sm = SessionManager(custom_sessions_dir=temp_sessions_dir)
        assert sm.metadata_valid is False
        assert not sm._write_sessions_metadata({'default': '2', 'sessions': {}})
        with open(conf_path) as metadata_file:
            assert metadata_file.readline().strip() == 'default=1'

    def test_malformed_selected_conf_blocks_metadata_mutation(self, temp_sessions_dir):
        from minios_session import SessionManager

        conf_path = os.path.join(temp_sessions_dir, 'session.conf')
        with open(conf_path, 'w') as metadata_file:
            metadata_file.write('default=1\nsession_mode[1]garbage=raw\n')

        sm = SessionManager(custom_sessions_dir=temp_sessions_dir)
        assert sm._read_sessions_metadata() == {'default': None, 'sessions': {}}
        assert sm.metadata_valid is False
        assert not sm._write_sessions_metadata({
            'default': '1', 'sessions': {'1': {'mode': 'native'}}})
        with open(conf_path) as metadata_file:
            assert 'session_mode[1]garbage=raw' in metadata_file.read()

    def test_metadata_rejects_conf_value_injection(self, temp_sessions_dir):
        from minios_session import SessionManager

        sm = SessionManager(custom_sessions_dir=temp_sessions_dir)
        metadata = {
            'default': '1',
            'sessions': {'1': {'version': '5.0\nsession_mode[1]=raw'}},
        }

        assert not sm._write_sessions_metadata(metadata)
        assert not os.path.exists(os.path.join(temp_sessions_dir, 'session.conf'))
        assert not os.path.exists(os.path.join(temp_sessions_dir, 'session.json'))

    def test_json_publication_failure_leaves_current_conf(self, temp_sessions_dir):
        from minios_session import SessionManager

        sm = SessionManager(custom_sessions_dir=temp_sessions_dir)
        json_path = os.path.join(temp_sessions_dir, 'session.json')
        conf_path = os.path.join(temp_sessions_dir, 'session.conf')
        real_replace = os.replace

        def replace(src, dst):
            if dst == json_path:
                raise OSError('json publication failed')
            return real_replace(src, dst)

        with patch('os.replace', side_effect=replace):
            assert sm._write_sessions_metadata({
                'default': '1', 'sessions': {'1': {'mode': 'native'}}})

        assert sm.sessions_file == conf_path
        assert sm.session_format == 'conf'
        assert not os.path.exists(json_path)
        with open(conf_path) as metadata_file:
            assert 'session_mode[1]=native' in metadata_file.read()

    def test_json_only_store_survives_conf_fallback_failure(self, temp_sessions_dir):
        from minios_session import SessionManager

        json_path = os.path.join(temp_sessions_dir, 'session.json')
        original = {'default': '1', 'sessions': {'1': {'mode': 'native'}}}
        with open(json_path, 'w') as metadata_file:
            json.dump(original, metadata_file)
        sm = SessionManager(custom_sessions_dir=temp_sessions_dir)

        with patch.object(sm, '_atomic_write_metadata_file',
                          side_effect=OSError('conf publication failed')):
            assert not sm._write_sessions_metadata({
                'default': '2', 'sessions': {'2': {'mode': 'raw'}}})

        with open(json_path) as metadata_file:
            assert json.load(metadata_file) == original

    def test_existing_manager_falls_back_when_json_disappears(self, temp_sessions_dir):
        from minios_session import SessionManager

        writer = SessionManager(custom_sessions_dir=temp_sessions_dir)
        assert writer._write_sessions_metadata({
            'default': '1', 'sessions': {'1': {'mode': 'native'}}})
        existing = SessionManager(custom_sessions_dir=temp_sessions_dir)
        json_path = os.path.join(temp_sessions_dir, 'session.json')
        real_replace = os.replace

        def replace(src, dst):
            if dst == json_path:
                raise OSError('json publication failed')
            return real_replace(src, dst)

        with patch('os.replace', side_effect=replace):
            assert writer._write_sessions_metadata({
                'default': '2',
                'sessions': {
                    '1': {'mode': 'native'},
                    '2': {'mode': 'raw'},
                },
            })

        metadata = existing._read_sessions_metadata()
        assert metadata['default'] == '2'
        assert set(metadata['sessions']) == {'1', '2'}
        assert existing.session_format == 'conf'

    def test_dynfilefs_admission_uses_thin_floor(self, temp_sessions_dir):
        from minios_session import SessionManager

        sm = SessionManager(custom_sessions_dir=temp_sessions_dir)
        sm.check_sessions_directory_status = lambda: {'writable': True}
        sm._validate_target_mode = lambda mode, size=None: (True, None)
        sm._check_dynfilefs_available = lambda: True

        captured = []

        def fake_check(path, required_mb):
            captured.append(required_mb)
            return False, "stop"

        sm._check_free_space = fake_check

        sm.create_session(session_mode='dynfilefs', size_mb=4000)
        sm.create_session(session_mode='raw', size_mb=4000)

        # DynFileFS admits on a small floor; raw requires the full size.
        assert captured == [sm.DYNFILEFS_INITIAL_MB, 4000]

    def test_cleanup_rejects_nonpositive_days(self, temp_sessions_dir):
        from minios_session import SessionManager

        sm = SessionManager(custom_sessions_dir=temp_sessions_dir)
        removed, errors = sm.cleanup_old_sessions(0)
        assert removed == 0
        assert len(errors) == 1

    def test_native_copy_preserves_whiteouts(self, temp_sessions_dir):
        from minios_session import SessionManager

        source = os.path.join(temp_sessions_dir, '1')
        target = os.path.join(temp_sessions_dir, '2')
        os.mkdir(source)
        os.mkdir(target)
        sm = SessionManager(custom_sessions_dir=temp_sessions_dir)
        with patch('subprocess.run', return_value=MagicMock(returncode=0)) as run:
            assert sm._copy_session_direct(source, target, 'native')

        command = run.call_args[0][0]
        assert '--exclude=.wh.*' not in command

    def test_native_copy_rejects_partial_rsync(self, temp_sessions_dir):
        from minios_session import SessionManager

        source = os.path.join(temp_sessions_dir, '1')
        target = os.path.join(temp_sessions_dir, '2')
        os.mkdir(source)
        os.mkdir(target)
        sm = SessionManager(custom_sessions_dir=temp_sessions_dir)
        partial = MagicMock(returncode=23, stderr=b'partial transfer')
        with patch('subprocess.run', return_value=partial):
            assert sm._copy_session_direct(source, target, 'native') is False

    def test_conversion_rejects_partial_rsync(self, temp_sessions_dir):
        from minios_session import SessionManager

        source = os.path.join(temp_sessions_dir, '1')
        target = os.path.join(temp_sessions_dir, '2')
        os.mkdir(source)
        os.mkdir(target)
        sm = SessionManager(custom_sessions_dir=temp_sessions_dir)
        source_mount = MagicMock()
        source_mount.__enter__.return_value = source
        target_mount = MagicMock()
        target_mount.__enter__.return_value = target
        partial = MagicMock(returncode=24, stderr=b'vanished source file')
        with patch.object(sm, '_mount_session_read', return_value=source_mount), \
                patch.object(sm, '_mount_session_write', return_value=target_mount), \
                patch('subprocess.run', return_value=partial):
            assert sm._copy_session_with_conversion(
                source, target, 'native', 'raw', 100) is False

    def test_convert_new_session_delegates_to_copy_without_replacing_source(self, temp_sessions_dir):
        from minios_session import SessionManager

        sm = SessionManager(custom_sessions_dir=temp_sessions_dir)
        with patch.object(sm, 'copy_session', return_value=(True, 'copied')) as copy:
            assert sm.convert_session('1', 'raw', size_mb=100, in_place=False) == (True, 'copied')

        copy.assert_called_once_with('1', to_mode='raw', size_mb=100)

    def test_lock_rejects_symlink(self, temp_sessions_dir):
        from minios_session import SessionManager

        sm = SessionManager(custom_sessions_dir=temp_sessions_dir)
        os.symlink('/tmp', os.path.join(temp_sessions_dir, '.session.lock'))
        with pytest.raises(OSError):
            with sm._mutation_lock():
                pass

    def test_archive_validation_rejects_path_escape_and_special_members(self, temp_sessions_dir):
        from minios_session import SessionManager

        sm = SessionManager(custom_sessions_dir=temp_sessions_dir)
        unsafe_path = MagicMock(returncode=0, stdout=b'-rw-r--r-- 0/0 1 2026-01-01 00:00 ../outside\n')
        special_file = MagicMock(returncode=0, stdout=b'crw-r--r-- 0/0 0 2026-01-01 00:00 data/device\n')
        with patch('subprocess.run', return_value=unsafe_path):
            assert sm._validate_archive('session.tar.zst')[0] is False
        with patch('subprocess.run', return_value=special_file):
            assert sm._validate_archive('session.tar.zst')[0] is False

    def test_archive_validation_has_independent_extraction_limit(self, temp_sessions_dir):
        from minios_session import SessionManager

        sm = SessionManager(custom_sessions_dir=temp_sessions_dir)
        listing = ('-rw-r--r-- 0/0 {} 2026-01-01 00:00 data/large\n'.format(
            sm.MAX_ARCHIVE_EXTRACTED_BYTES + 1)).encode()
        with patch('subprocess.run', return_value=MagicMock(returncode=0, stdout=listing)):
            assert sm._validate_archive('session.tar.zst')[0] is False

    def test_archive_validation_accepts_data_members_only(self, temp_sessions_dir):
        from minios_session import SessionManager

        sm = SessionManager(custom_sessions_dir=temp_sessions_dir)
        listing = b''.join((
            b'-rw-r--r-- 0/0 2 2026-01-01 00:00 metadata.json\n',
            b'-rw-r--r-- 0/0 2 2026-01-01 00:00 session.info\n',
            b'drwxr-xr-x 0/0 0 2026-01-01 00:00 data/\n',
            b'-rw-r--r-- 0/0 1 2026-01-01 00:00 data/file\n',
        ))
        with patch('subprocess.run', return_value=MagicMock(returncode=0, stdout=listing)):
            assert sm._validate_archive('session.tar.zst') == (True, None)


class TestSquashfsGating:
    @staticmethod
    def _manager(temp_sessions_dir):
        from minios_session import SessionManager

        session_path = os.path.join(temp_sessions_dir, '1')
        os.mkdir(session_path)
        payload = b'hsqs-snapshot'
        with open(os.path.join(session_path, 'changes.sb'), 'wb') as artifact:
            artifact.write(payload)
        with open(os.path.join(temp_sessions_dir, 'session.json'), 'w') as metadata_file:
            json.dump({
                'default': '2',
                'sessions': {
                    '1': {'mode': 'squashfs', 'version': '5.0',
                          'edition': 'standard', 'union': 'overlayfs',
                          'policy': 'manual',
                          'digest': hashlib.sha256(payload).hexdigest(),
                          'compressed': len(payload), 'uncompressed': 0,
                          'entries': 0},
                    '2': {'mode': 'native', 'version': '5.0',
                          'edition': 'standard', 'union': 'overlayfs'},
                },
            }, metadata_file)
        manager = SessionManager(custom_sessions_dir=temp_sessions_dir)
        manager._get_current_union_fs = lambda: 'overlayfs'
        return manager

    def test_activation_accepts_legacy_squashfs_without_policy_as_manual(self,
                                                                       temp_sessions_dir):
        sm = self._manager(temp_sessions_dir)
        with open(os.path.join(temp_sessions_dir, 'session.json')) as metadata_file:
            metadata = json.load(metadata_file)
        metadata['sessions']['1'].pop('policy', None)
        with open(os.path.join(temp_sessions_dir, 'session.json'), 'w') as metadata_file:
            json.dump(metadata, metadata_file)

        success, message = sm.activate_session('1')

        assert success is True and 'activated' in message
        with open(os.path.join(temp_sessions_dir, 'session.json')) as metadata_file:
            saved = json.load(metadata_file)
        assert saved['default'] == '1'
        assert 'policy' not in saved['sessions']['1']

    @pytest.mark.parametrize('policy', ('manual', 'shutdown'))
    def test_activation_accepts_supported_squashfs_policy(self, temp_sessions_dir, policy):
        sm = self._manager(temp_sessions_dir)
        with open(os.path.join(temp_sessions_dir, 'session.json')) as metadata_file:
            metadata = json.load(metadata_file)
        metadata['sessions']['1']['policy'] = policy
        with open(os.path.join(temp_sessions_dir, 'session.json'), 'w') as metadata_file:
            json.dump(metadata, metadata_file)

        success, message = sm.activate_session('1')

        assert success is True and 'activated' in message
        with open(os.path.join(temp_sessions_dir, 'session.json')) as metadata_file:
            assert json.load(metadata_file)['default'] == '1'

    def test_activation_rejects_changed_squashfs_without_changing_default(self,
                                                                          temp_sessions_dir):
        sm = self._manager(temp_sessions_dir)
        with open(os.path.join(temp_sessions_dir, '1', 'changes.sb'), 'ab') as artifact:
            artifact.write(b'changed')

        success, message = sm.activate_session('1')

        assert success is False and 'metadata' in message
        with open(os.path.join(temp_sessions_dir, 'session.json')) as metadata_file:
            assert json.load(metadata_file)['default'] == '2'

    def test_data_operations_reject_squashfs_before_reservation(self,
                                                                 temp_sessions_dir):
        sm = self._manager(temp_sessions_dir)
        output = os.path.join(temp_sessions_dir, 'export.tar.zst')

        with patch.object(sm, '_reserve_session',
                          side_effect=AssertionError('must not reserve')):
            copied, copy_message = sm.copy_session('1')
            converted, convert_message = sm.convert_session('1', 'native')
        exported, export_message = sm.export_session('1', output)

        assert copied is False and 'save support' in copy_message
        assert converted is False and 'save support' in convert_message
        assert exported is False and 'save support' in export_message
        assert not os.path.exists(output)

    def test_create_cleanup_removes_reservation_after_metadata_error(self,
                                                                      temp_sessions_dir):
        from minios_session import SessionManager

        with open(os.path.join(temp_sessions_dir, 'session.json'), 'w') as metadata_file:
            json.dump([], metadata_file)
        sm = SessionManager(custom_sessions_dir=temp_sessions_dir)
        sm.check_sessions_directory_status = lambda: {'writable': True}
        sm._validate_target_mode = lambda mode, size=None: (True, None)
        sm._check_free_space = lambda path, required_mb: (True, None)

        success, _message = sm.create_session('native')

        assert success is False
        assert not os.path.exists(os.path.join(temp_sessions_dir, '1'))


class TestSquashfsSave:
    @staticmethod
    def _footprint(uncompressed_size=2048, entry_count=12):
        regular_inodes = 1 if entry_count else 0
        return {
            'product_kind': 'minios-extraction-footprint',
            'schema_version': 1,
            'regular_file_bytes': uncompressed_size,
            'regular_file_inodes': regular_inodes,
            'directory_count': 1,
            'symlink_count': 0,
            'symlink_target_bytes': 0,
            'whiteout_count': 0,
            'inode_count': 1 + regular_inodes,
            'directory_entry_count': entry_count,
            'filename_bytes': entry_count * 6,
            'hardlink_reference_count': max(0, entry_count - regular_inodes),
            'xattr_count': 0,
            'xattr_name_bytes': 0,
            'xattr_value_bytes': 0,
            'compressor': 'zstd',
            'block_size': 1024 * 1024,
        }

    @staticmethod
    def _manager(temp_sessions_dir, running='1', previous=None):
        from minios_session import SessionManager

        session_path = os.path.join(temp_sessions_dir, '1')
        os.mkdir(session_path, 0o700)
        session = {
            'mode': 'squashfs', 'version': '5.0', 'edition': 'standard',
            'union': 'overlayfs', 'policy': 'shutdown',
        }
        if previous:
            session.update(previous)
        metadata = {'default': '1', 'sessions': {'1': session}}
        if running is not None:
            metadata['running'] = running
        with open(os.path.join(temp_sessions_dir, 'session.json'), 'w') as metadata_file:
            json.dump(metadata, metadata_file)
        manager = SessionManager(custom_sessions_dir=temp_sessions_dir)
        manager._validate_squashfs_runtime = lambda _session_id: (True, None)
        return manager, session_path

    @staticmethod
    def _capture(payload=b'hsqs-new-snapshot', **overrides):
        def run(output_path, work_parent):
            assert os.path.dirname(output_path) == work_parent
            with open(output_path, 'wb') as output:
                output.write(payload)
            output_stat = os.stat(output_path)
            result = {
                'type': 'result',
                'product_kind': 'minios-tool-result',
                'schema_version': 1,
                'tool': 'savechanges',
                'operation': 'capture-module',
                'output': output_path,
                'output_identity': {
                    'device': output_stat.st_dev,
                    'inode': output_stat.st_ino,
                },
                'compressed_size': len(payload),
                'uncompressed_size': 2048,
                'entry_count': 12,
                'extraction_footprint': TestSquashfsSave._footprint(),
                'sha256': hashlib.sha256(payload).hexdigest(),
                'profile': 'exact',
                'union_backend': 'overlayfs',
            }
            result.update(overrides)
            return result
        return run

    def test_create_squashfs_captures_current_changes_and_publishes_metadata(
            self, temp_sessions_dir):
        from minios_session import SessionManager

        sm = SessionManager(custom_sessions_dir=temp_sessions_dir)
        sm.check_sessions_directory_status = lambda: {'writable': True}
        sm._validate_target_mode = lambda mode, size=None: (True, None)
        sm._check_free_space = lambda *_args: (_ for _ in ()).throw(
            AssertionError('fixed-size admission must not run for squashfs'))
        sm._get_current_union_fs = lambda: 'overlayfs'
        capture = self._capture()

        def locked_capture(output_path, work_parent):
            assert sm._lock_depth > 0
            return capture(output_path, work_parent)

        with patch.object(sm, '_run_savechanges', side_effect=locked_capture):
            success, message = sm.create_session('squashfs')

        assert success is True and 'squashfs' in message
        session_path = os.path.join(temp_sessions_dir, '1')
        with open(os.path.join(session_path, 'changes.sb'), 'rb') as artifact:
            assert artifact.read() == b'hsqs-new-snapshot'
        assert not any(name.startswith('.changes.sb.new-')
                       for name in os.listdir(session_path))
        with open(os.path.join(temp_sessions_dir, 'session.json')) as metadata_file:
            session = json.load(metadata_file)['sessions']['1']
        assert session['mode'] == 'squashfs'
        assert session['policy'] == 'shutdown'
        assert session['digest'] == hashlib.sha256(b'hsqs-new-snapshot').hexdigest()
        assert session['compressed'] == '17'
        assert session['uncompressed'] == '2048'
        assert session['entries'] == '12'
        assert session['generation'] == '1'
        assert session['union'] == 'overlayfs'

    def test_create_squashfs_accepts_manual_policy(self, temp_sessions_dir):
        from minios_session import SessionManager

        sm = SessionManager(custom_sessions_dir=temp_sessions_dir)
        sm.check_sessions_directory_status = lambda: {'writable': True}
        sm._validate_target_mode = lambda mode, size=None: (True, None)
        sm._get_current_union_fs = lambda: 'overlayfs'
        with patch.object(sm, '_run_savechanges', side_effect=self._capture()):
            success, _message = sm.create_session('squashfs', policy='manual')

        assert success is True
        with open(os.path.join(temp_sessions_dir, 'session.json')) as metadata_file:
            session = json.load(metadata_file)['sessions']['1']
        assert session['policy'] == 'manual'

    def test_create_preserves_session_after_uncertain_metadata_commit(self,
                                                                      temp_sessions_dir):
        from minios_session import MetadataCommitUncertain, SessionManager

        sm = SessionManager(custom_sessions_dir=temp_sessions_dir)
        sm.check_sessions_directory_status = lambda: {'writable': True}
        sm._validate_target_mode = lambda mode, size=None: (True, None)
        sm._get_current_union_fs = lambda: 'overlayfs'

        def uncertain_commit(metadata):
            with open(os.path.join(temp_sessions_dir, 'session.json'), 'w') as metadata_file:
                json.dump(metadata, metadata_file)
            raise MetadataCommitUncertain('injected directory fsync failure')

        sm._write_sessions_metadata = uncertain_commit
        with patch.object(sm, '_run_savechanges', side_effect=self._capture()):
            success, message = sm.create_session('squashfs')

        assert success is False
        assert 'preserved' in message
        assert os.path.isfile(os.path.join(temp_sessions_dir, '1', 'changes.sb'))
        with open(os.path.join(temp_sessions_dir, 'session.json')) as metadata_file:
            assert '1' in json.load(metadata_file)['sessions']

    def test_create_squashfs_stores_periodic_save_interval(self, temp_sessions_dir):
        from minios_session import SessionManager

        sm = SessionManager(custom_sessions_dir=temp_sessions_dir)
        sm.check_sessions_directory_status = lambda: {'writable': True}
        sm._validate_target_mode = lambda mode, size=None: (True, None)
        sm._get_current_union_fs = lambda: 'overlayfs'
        with patch.object(sm, '_run_savechanges', side_effect=self._capture()):
            success, _message = sm.create_session('squashfs', autosave=30)

        assert success is True
        with open(os.path.join(temp_sessions_dir, 'session.json')) as metadata_file:
            session = json.load(metadata_file)['sessions']['1']
        assert session['autosave'] == '30'

    def test_create_squashfs_rejects_too_frequent_periodic_save(self,
                                                                 temp_sessions_dir):
        from minios_session import SessionManager

        sm = SessionManager(custom_sessions_dir=temp_sessions_dir)
        success, message = sm.create_session('squashfs', autosave=10)
        assert success is False
        assert 'interval' in message.lower()

    def test_runner_uses_fixed_exact_machine_contract(self, temp_sessions_dir):
        sm, session_path = self._manager(temp_sessions_dir)
        command = os.path.join(temp_sessions_dir, 'savechanges')
        with open(command, 'w') as executable:
            executable.write('#!/bin/sh\n')
        os.chmod(command, 0o700)
        sm.SAVECHANGES_COMMAND = command
        output_path = os.path.join(session_path, '.changes.sb.new-test')
        phases = [
            {'type': 'phase', 'phase': phase}
            for phase in sm.SAVECHANGES_PHASES
        ]
        result = {
            'type': 'result', 'product_kind': 'minios-tool-result',
            'schema_version': 1, 'tool': 'savechanges',
            'operation': 'capture-module', 'output': output_path,
            'output_identity': {'device': 1, 'inode': 2},
            'compressed_size': 4, 'uncompressed_size': 0,
            'entry_count': 0,
            'extraction_footprint': self._footprint(0, 0),
            'sha256': 'b' * 64,
            'profile': 'exact', 'union_backend': 'overlayfs',
        }
        stdout = ''.join(json.dumps(event) + '\n' for event in phases + [result])
        process = MagicMock(returncode=0)
        process.stdout = io.StringIO(stdout)

        seen_phases = []
        with patch('subprocess.Popen', return_value=process) as popen:
            assert sm._run_savechanges(
                output_path, session_path,
                progress_callback=seen_phases.append) == result

        assert seen_phases == list(sm.SAVECHANGES_PHASES)
        assert popen.call_args[0][0] == [
            command, '--json', '--profile', 'exact', '--work-parent',
            session_path, output_path,
        ]
        kwargs = popen.call_args[1]
        assert kwargs['start_new_session'] is True
        assert kwargs['universal_newlines'] is True
        assert 'PKEXEC_UID' not in kwargs['env']


    def test_save_fails_closed_without_runtime_authority(self, temp_sessions_dir):
        from minios_session import SessionManager

        session_path = os.path.join(temp_sessions_dir, '1')
        os.mkdir(session_path, 0o700)
        with open(os.path.join(temp_sessions_dir, 'session.json'), 'w') as metadata_file:
            json.dump({
                'default': '1', 'running': '1',
                'sessions': {'1': {'mode': 'squashfs'}},
            }, metadata_file)
        sm = SessionManager(custom_sessions_dir=temp_sessions_dir)

        with patch.object(sm, '_run_savechanges',
                          side_effect=AssertionError('must not capture')):
            success, message, result = sm.save_session('1')

        assert success is False and 'custom sessions directory' in message
        assert result is None

    def test_runner_rejects_duplicate_and_nonfinite_json(self, temp_sessions_dir):
        sm, session_path = self._manager(temp_sessions_dir)
        command = os.path.join(temp_sessions_dir, 'savechanges')
        with open(command, 'w') as executable:
            executable.write('#!/bin/sh\n')
        os.chmod(command, 0o700)
        sm.SAVECHANGES_COMMAND = command
        output_path = os.path.join(session_path, '.changes.sb.new-test')
        valid_phases = ''.join(
            json.dumps({'type': 'phase', 'phase': phase}) + '\n'
            for phase in sm.SAVECHANGES_PHASES)

        for malformed in (
                '{"type":"result","type":"result"}\n',
                '{"type":"result","extra":NaN}\n'):
            process = MagicMock(returncode=0)
            process.stdout = io.StringIO(valid_phases + malformed)
            with patch('subprocess.Popen', return_value=process), \
                 pytest.raises(ValueError):
                sm._run_savechanges(output_path, session_path)

    def test_runner_rejects_malformed_extraction_footprint(self,
                                                            temp_sessions_dir):
        sm, session_path = self._manager(temp_sessions_dir)
        command = os.path.join(temp_sessions_dir, 'savechanges')
        with open(command, 'w') as executable:
            executable.write('#!/bin/sh\n')
        os.chmod(command, 0o700)
        sm.SAVECHANGES_COMMAND = command
        output_path = os.path.join(session_path, '.changes.sb.new-test')
        phases = [
            {'type': 'phase', 'phase': phase}
            for phase in sm.SAVECHANGES_PHASES
        ]
        base_result = {
            'type': 'result', 'product_kind': 'minios-tool-result',
            'schema_version': 1, 'tool': 'savechanges',
            'operation': 'capture-module', 'output': output_path,
            'output_identity': {'device': 1, 'inode': 2},
            'compressed_size': 4, 'uncompressed_size': 2048,
            'entry_count': 12, 'sha256': 'b' * 64,
            'profile': 'exact', 'union_backend': 'overlayfs',
        }
        missing_field = self._footprint()
        missing_field.pop('xattr_count')
        extra_field = self._footprint()
        extra_field['extra'] = 0
        wrong_product_kind = self._footprint()
        wrong_product_kind['product_kind'] = 'unknown'
        wrong_version = self._footprint()
        wrong_version['schema_version'] = 2
        wrong_version_type = self._footprint()
        wrong_version_type['schema_version'] = True
        wrong_value_type = self._footprint()
        wrong_value_type['symlink_count'] = False
        negative_value = self._footprint()
        negative_value['filename_bytes'] = -1
        empty_root = self._footprint()
        empty_root['directory_count'] = 0
        invalid_compressor = self._footprint()
        invalid_compressor['compressor'] = 'unknown'
        invalid_block_size = self._footprint()
        invalid_block_size['block_size'] = 0
        non_power_of_two_block = self._footprint()
        non_power_of_two_block['block_size'] = 10000
        wrong_uncompressed_size = self._footprint()
        wrong_uncompressed_size['regular_file_bytes'] += 1
        wrong_entry_count = self._footprint()
        wrong_entry_count['directory_entry_count'] += 1
        wrong_inode_count = self._footprint()
        wrong_inode_count['inode_count'] += 1
        malformed_footprints = (
            None, missing_field, extra_field, wrong_product_kind, wrong_version,
            wrong_version_type, wrong_value_type, negative_value, empty_root,
            invalid_compressor, invalid_block_size, non_power_of_two_block,
            wrong_uncompressed_size, wrong_entry_count, wrong_inode_count,
        )

        for footprint in malformed_footprints:
            result = dict(base_result, extraction_footprint=footprint)
            stdout = ''.join(
                json.dumps(event) + '\n' for event in phases + [result])
            process = MagicMock(returncode=0)
            process.stdout = io.StringIO(stdout)
            with patch('subprocess.Popen', return_value=process), \
                 pytest.raises(ValueError):
                sm._run_savechanges(output_path, session_path)




    def test_runtime_authority_accepts_matching_durable_boot_state(self,
                                                                    temp_sessions_dir):
        from minios_session import SessionManager

        state_path = os.path.join(temp_sessions_dir, 'boot-state')
        boot_id_path = os.path.join(temp_sessions_dir, 'boot-id')
        sessions_stat = os.stat(temp_sessions_dir)
        with open(state_path, 'w') as state_file:
            state_file.write(
                'boot_id=test-boot\nboot_level=ok\nmode=squashfs\nsession=1\n'
                'durable=1\nwritable=1\nsessions_device={}\nsessions_inode={}\n'
                'active_generation=current\n'.format(
                    sessions_stat.st_dev, sessions_stat.st_ino))
        with open(boot_id_path, 'w') as boot_id_file:
            boot_id_file.write('test-boot\n')
        os.chmod(state_path, 0o600)
        sm = SessionManager(custom_sessions_dir=temp_sessions_dir)
        sm.custom_sessions_dir = None
        sm.BOOT_STATE_FILE = state_path
        sm.BOOT_ID_FILE = boot_id_path
        sm.SESSION_PATHS = (temp_sessions_dir,)
        filesystem = {
            'type': 'ext4', 'is_readonly': False, 'is_posix_compatible': True,
        }
        with patch.object(sm, '_detect_filesystem_type',
                          return_value=(filesystem, None)):
            assert sm._validate_squashfs_runtime('1') == (True, None)
        filesystem['type'] = 'vfat'
        filesystem['is_posix_compatible'] = False
        with patch.object(sm, '_detect_filesystem_type',
                          return_value=(filesystem, None)):
            valid, message = sm._validate_squashfs_runtime('1')
        assert valid is False
        assert 'cannot preserve exact capture metadata' in message



class TestLuksMode:
    def test_perchsize_uses_decimal_units_and_one_tb_limit(self):
        from minios_session import parse_perch_size

        assert parse_perch_size('4000') == 4000
        assert parse_perch_size('4GB') == 4000
        assert parse_perch_size('1TB') == 1000000
        with pytest.raises(Exception):
            parse_perch_size('1001TB')

    def test_luks_capability_requires_initrd_marker(self, temp_sessions_dir):
        from minios_session import SessionManager

        sm = SessionManager(custom_sessions_dir=temp_sessions_dir)
        with patch('shutil.which', return_value='/usr/bin/tool'), \
             patch('os.path.isfile', side_effect=lambda path: path == '/run/initramfs/etc/minios-initramfs-crypt'):
            assert sm._check_luks_available() == (True, None)

    def test_luks_is_not_exposed_without_initrd_marker(self, temp_sessions_dir):
        from minios_session import SessionManager

        sm = SessionManager(custom_sessions_dir=temp_sessions_dir)
        filesystem = {'type': 'ext4', 'is_readonly': False, 'is_posix_compatible': True}
        with patch('shutil.which', return_value='/usr/bin/tool'), \
             patch('os.path.isfile', return_value=False):
            assert 'luks' not in sm._get_compatible_session_modes(filesystem)

    def test_luks_creation_uses_loop_mapper_and_stdin_password(self, temp_sessions_dir):
        from minios_session import SessionManager

        sm = SessionManager(custom_sessions_dir=temp_sessions_dir)
        image = os.path.join(temp_sessions_dir, 'changes.luks')
        loop = MagicMock(returncode=0, stdout=b'/dev/loop7\n', stderr=b'')
        ok = MagicMock(returncode=0, stdout=b'', stderr=b'')
        with patch('subprocess.run', side_effect=[ok, loop, ok, ok, ok, ok, ok, ok]) as run:
            sm._create_luks_container(temp_sessions_dir, 4000, b'secret')

        commands = [call[0][0] for call in run.call_args_list]
        assert ['losetup', '--find', '--show', '--', image] in commands
        assert any(command[:3] == ['cryptsetup', 'luksFormat', '--type'] for command in commands)
        crypt_calls = [call for call in run.call_args_list if call[0][0][0] == 'cryptsetup']
        assert all(b'secret' not in call[0][0] for call in crypt_calls)
        assert all(call[1].get('input') == b'secret\n' for call in crypt_calls[:2])
        assert any(command[:2] == ['cryptsetup', 'close'] for command in commands)

    def test_luks_target_observes_fat32_limit(self, temp_sessions_dir):
        from minios_session import SessionManager

        sm = SessionManager(custom_sessions_dir=temp_sessions_dir)
        with patch.object(sm, '_detect_filesystem_type', return_value=({'type': 'vfat', 'is_readonly': False,
                                                                         'is_posix_compatible': False}, None)), \
             patch.object(sm, '_check_luks_available', return_value=(True, None)):
            valid, message = sm._validate_target_mode('luks', 4096)
        assert not valid
        assert 'FAT32' in message

    def test_luks_resize_recovers_durable_post_growth_metadata(self, temp_sessions_dir):
        from minios_session import SessionManager

        session_path = os.path.join(temp_sessions_dir, '1')
        os.mkdir(session_path)
        open(os.path.join(session_path, 'changes.luks'), 'wb').close()
        journal_path = os.path.join(session_path, '.luks-resize.json')
        with open(journal_path, 'w') as journal:
            json.dump({'size': 6000}, journal)
        sm = SessionManager(custom_sessions_dir=temp_sessions_dir)
        metadata = {'sessions': {'1': {'mode': 'luks', 'size': 4000}}}
        with patch('os.path.getsize', return_value=6000 * 1024 * 1024), \
             patch.object(sm, '_write_sessions_metadata', return_value=True):
            success, message = sm._resize_luks_session(session_path, 7000, '1', metadata, b'password')

        assert success
        assert message
        assert metadata['sessions']['1']['size'] == 6000
        assert not os.path.exists(journal_path)


class TestSquashfsUserExperience:
    @staticmethod
    def _write_metadata(root, metadata):
        with open(os.path.join(root, 'session.json'), 'w') as handle:
            json.dump(metadata, handle)

    def test_list_uses_logical_size_and_saved_timestamp(self, temp_sessions_dir):
        from minios_session import SessionManager

        session_path = os.path.join(temp_sessions_dir, '1')
        os.mkdir(session_path)
        with open(os.path.join(session_path, 'changes.sb'), 'wb') as artifact:
            artifact.write(b'hsqs-small')
        self._write_metadata(temp_sessions_dir, {
            'default': '1',
            'sessions': {'1': {
                'mode': 'squashfs', 'version': '20260816',
                'edition': 'standard', 'union': 'overlayfs',
                'uncompressed': 9048675, 'compressed': 10,
                'saved': '2026-08-18T13:00:04Z',
            }},
        })
        session = SessionManager(custom_sessions_dir=temp_sessions_dir).list_sessions()[0]
        assert session['size'] == 9048675
        assert session['size_display'] == '8.6MB'
        assert session['modified'].isoformat() == '2026-08-18T13:00:04'

    def test_save_settings_are_independent(self, temp_sessions_dir):
        from minios_session import SessionManager

        session_path = os.path.join(temp_sessions_dir, '1')
        os.mkdir(session_path)
        self._write_metadata(temp_sessions_dir, {
            'default': '1', 'running': '1',
            'sessions': {'1': {'mode': 'squashfs', 'policy': 'shutdown'}},
        })
        manager = SessionManager(custom_sessions_dir=temp_sessions_dir)
        success, _message, settings = manager.set_squashfs_save_settings(
            '1', shutdown=False, autosave=30)
        assert success
        assert settings == {'shutdown': False, 'autosave': 30}
        with open(os.path.join(temp_sessions_dir, 'session.json')) as handle:
            session = json.load(handle)['sessions']['1']
        assert session['policy'] == 'manual'
        assert session['autosave'] == '30'

    def test_periodic_save_runs_only_when_due(self, temp_sessions_dir):
        from datetime import datetime
        from minios_session import SessionManager

        session_path = os.path.join(temp_sessions_dir, '1')
        os.mkdir(session_path)
        self._write_metadata(temp_sessions_dir, {
            'default': '1', 'running': '1',
            'sessions': {'1': {'mode': 'squashfs', 'autosave': 30,
                               'saved': '2026-08-18T11:00:00Z'}},
        })
        manager = SessionManager(custom_sessions_dir=temp_sessions_dir)
        original_open = open

        def open_with_uptime(path, *args, **kwargs):
            if path == '/proc/uptime':
                return io.StringIO('3600.0 0.0\n')
            return original_open(path, *args, **kwargs)

        with patch('builtins.open', side_effect=open_with_uptime), \
             patch.object(manager, 'save_session',
                          return_value=(True, 'saved', {'generation': 2})) as save:
            performed, success, _message, capture = manager.autosave_running_session(
                now=datetime(2026, 8, 18, 12, 0, 0))
        assert performed and success
        assert capture == {'generation': 2}
        save.assert_called_once_with('1')

        self._write_metadata(temp_sessions_dir, {
            'default': '1', 'running': '1',
            'sessions': {'1': {'mode': 'squashfs', 'autosave': 30,
                               'saved': '2026-08-18T11:45:00Z'}},
        })
        manager = SessionManager(custom_sessions_dir=temp_sessions_dir)
        with patch('builtins.open', side_effect=open_with_uptime), \
             patch.object(manager, 'save_session') as save:
            performed, success, _message, capture = manager.autosave_running_session(
                now=datetime(2026, 8, 18, 12, 0, 0))
        assert performed is False and success is True and capture is None
        save.assert_not_called()

    def test_running_squashfs_can_handoff_to_active_current_boot_snapshot(
            self, temp_sessions_dir):
        from minios_session import SessionManager

        for session_id in ('1', '2'):
            os.mkdir(os.path.join(temp_sessions_dir, session_id))
        self._write_metadata(temp_sessions_dir, {
            'default': '2', 'running': '1',
            'sessions': {
                '1': {'mode': 'squashfs', 'union': 'overlayfs'},
                '2': {'mode': 'squashfs', 'union': 'overlayfs',
                      'capture_boot_id': 'boot-1'},
            },
        })
        manager = SessionManager(custom_sessions_dir=temp_sessions_dir)
        manager._current_boot_id = lambda: 'boot-1'
        manager._validate_squashfs_activation = lambda *_args: (True, None)
        manager._validate_squashfs_runtime = lambda *_args: (True, None)
        replaced = []
        manager._replace_boot_state_session = lambda old, new: replaced.append((old, new))

        success, message = manager.delete_session('1', handoff=True)
        assert success and message
        assert replaced == [('1', '2')]
        assert not os.path.exists(os.path.join(temp_sessions_dir, '1'))
        with open(os.path.join(temp_sessions_dir, 'session.json')) as handle:
            metadata = json.load(handle)
        assert metadata['default'] == '2'
        assert metadata['running'] == '2'
        assert '1' not in metadata['sessions']

    def test_running_squashfs_handoff_rejects_old_active_snapshot(
            self, temp_sessions_dir):
        from minios_session import SessionManager

        for session_id in ('1', '2'):
            os.mkdir(os.path.join(temp_sessions_dir, session_id))
        self._write_metadata(temp_sessions_dir, {
            'default': '2', 'running': '1',
            'sessions': {
                '1': {'mode': 'squashfs', 'union': 'overlayfs'},
                '2': {'mode': 'squashfs', 'union': 'overlayfs',
                      'capture_boot_id': 'older-boot'},
            },
        })
        manager = SessionManager(custom_sessions_dir=temp_sessions_dir)
        manager._current_boot_id = lambda: 'boot-1'
        manager._replace_boot_state_session = MagicMock()
        success, message = manager.delete_session('1', handoff=True)
        assert success is False
        assert 'during this boot' in message.lower()
        assert os.path.isdir(os.path.join(temp_sessions_dir, '1'))
        manager._replace_boot_state_session.assert_not_called()


class TestSquashfsSaveDelegation:
    @staticmethod
    def _manager(temp_sessions_dir, command):
        from minios_session import SessionManager
        manager = SessionManager(custom_sessions_dir=temp_sessions_dir)
        manager.custom_sessions_dir = None
        manager.SQUASHFS_SAVE_COMMAND = command
        return manager

    @staticmethod
    def _command(temp_sessions_dir):
        command = os.path.join(temp_sessions_dir, 'minios-squashfs-save')
        with open(command, 'w') as executable:
            executable.write('#!/bin/sh\nexit 0\n')
        os.chmod(command, 0o700)
        return command

    def test_save_delegates_progress_to_tools_backend(self, temp_sessions_dir):
        command = self._command(temp_sessions_dir)
        manager = self._manager(temp_sessions_dir, command)
        output = ''.join(
            json.dumps({'phase': phase, 'type': 'phase'}) + '\n'
            for phase in manager.SAVECHANGES_PHASES)
        output += ('{"capture":{"generation":8},"message":'
                   '"Session 1 saved successfully","success":true}\n')
        process = MagicMock(returncode=0)
        process.stdout = io.StringIO(output)
        seen = []
        with patch('subprocess.Popen', return_value=process) as popen:
            success, message, capture = manager.save_session('1', progress_callback=seen.append)
        assert success is True
        assert message == 'Session 1 saved successfully'
        assert capture == {'generation': 8}
        assert seen == list(manager.SAVECHANGES_PHASES)
        assert popen.call_args[0][0] == [command, '1', '--json', '--progress']

    def test_shutdown_finalize_is_forwarded_to_tools_backend(self, temp_sessions_dir):
        command = self._command(temp_sessions_dir)
        manager = self._manager(temp_sessions_dir, command)
        process = MagicMock(returncode=0)
        process.stdout = io.StringIO(
            '{"capture":{"shutdown_finalized":true},"message":"saved","success":true}\n')
        with patch('subprocess.Popen', return_value=process) as popen:
            success, _message, capture = manager.save_session('1', finalize_shutdown=True)
        assert success is True
        assert capture['shutdown_finalized'] is True
        assert popen.call_args[0][0] == [
            command, '1', '--json', '--shutdown-finalize']

    def test_missing_tools_backend_fails_before_process_start(self, temp_sessions_dir):
        manager = self._manager(
            temp_sessions_dir, os.path.join(temp_sessions_dir, 'missing-backend'))
        with patch('subprocess.Popen', side_effect=AssertionError('must not run')):
            success, message, capture = manager.save_session('1')
        assert success is False
        assert 'backend is unavailable' in message
        assert capture is None

    @pytest.mark.parametrize('result', [
        '{"capture":{},"message":"saved","success":"false"}\n',
        '{"capture":{},"message":"saved","message":"again","success":true}\n',
        '{"capture":{},"message":"saved","success":NaN}\n',
        '[]\n',
    ])
    def test_delegated_save_rejects_malformed_results(self, temp_sessions_dir, result):
        command = self._command(temp_sessions_dir)
        manager = self._manager(temp_sessions_dir, command)
        process = MagicMock(returncode=0)
        process.stdout = io.StringIO(result)

        with patch('subprocess.Popen', return_value=process):
            success, _message, capture = manager.save_session('1')

        assert success is False
        assert capture is None
