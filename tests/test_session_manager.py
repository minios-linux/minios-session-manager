#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tests for minios_session SessionManager class.
"""

import sys
import os
import io
import json
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
    with pytest.raises(ValueError, match='do not match'):
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

    def test_detect_aufs_from_cmdline(self, temp_sessions_dir):
        """Test detecting AUFS from kernel command line."""
        from minios_session import SessionManager
        
        cmdline = "BOOT_IMAGE=/minios/boot/vmlinuz union=aufs"
        
        with patch('os.path.exists', return_value=True), \
             patch('builtins.open', mock_open(read_data=cmdline)):
            sm = SessionManager(custom_sessions_dir=temp_sessions_dir)
            result = sm._get_current_union_fs()
            assert result == 'aufs'

    def test_detect_overlayfs_from_cmdline(self, temp_sessions_dir):
        """Test detecting OverlayFS from kernel command line."""
        from minios_session import SessionManager
        
        cmdline = "BOOT_IMAGE=/minios/boot/vmlinuz union=overlayfs"
        
        with patch('os.path.exists', return_value=True), \
             patch('builtins.open', mock_open(read_data=cmdline)):
            sm = SessionManager(custom_sessions_dir=temp_sessions_dir)
            result = sm._get_current_union_fs()
            assert result == 'overlayfs'

    def test_detect_from_filesystems(self, temp_sessions_dir, mock_proc_filesystems):
        """Test auto-detection from /proc/filesystems."""
        from minios_session import SessionManager
        
        cmdline = "BOOT_IMAGE=/minios/boot/vmlinuz"  # No union= parameter
        
        files = {
            '/proc/cmdline': cmdline,
            '/proc/filesystems': mock_proc_filesystems
        }
        
        def mock_open_func(path, *args, **kwargs):
            if path in files:
                return mock_open(read_data=files[path])()
            raise FileNotFoundError(path)
        
        with patch('os.path.exists', return_value=True), \
             patch('builtins.open', side_effect=mock_open_func):
            sm = SessionManager(custom_sessions_dir=temp_sessions_dir)
            result = sm._get_current_union_fs()
            assert result in ['aufs', 'overlayfs']


class TestGetSystemVersion:
    """Tests for _get_system_version method."""

    def test_read_version_from_release_file(self, temp_sessions_dir, sample_minios_release):
        """Test reading version from minios-release file."""
        from minios_session import SessionManager
        
        def exists_side_effect(path):
            return path in [temp_sessions_dir, '/etc/minios-release']
        
        with patch('os.path.exists', side_effect=exists_side_effect), \
             patch('builtins.open', mock_open(read_data=sample_minios_release)):
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
             patch('builtins.open', mock_open(read_data=sample_minios_release)):
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
            text=True,
            check=False,
        )

        assert result.returncode == 0
        assert 'Usage: minios-session-manager' in result.stdout
        assert 'minios-session --help' in result.stdout
        assert result.stderr == ''


class TestAuditRegressions:
    """Security and data-integrity regressions from the session storage audit."""

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
        assert 'Unable to check' in error

    def test_readonly_filesystem_has_no_mutable_modes(self, temp_sessions_dir):
        from minios_session import SessionManager

        sm = SessionManager(custom_sessions_dir=temp_sessions_dir)
        assert sm._get_compatible_session_modes({
            'is_readonly': True, 'is_posix_compatible': True, 'type': 'ext4'
        }) == []

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

        replace.assert_called_once()
        with open(sm.sessions_file, encoding='utf-8') as metadata_file:
            assert json.load(metadata_file) == {'default': None, 'sessions': {}}

    def test_cleanup_rejects_nonpositive_days(self, temp_sessions_dir):
        from minios_session import SessionManager

        sm = SessionManager(custom_sessions_dir=temp_sessions_dir)
        assert sm.cleanup_old_sessions(0) == (0, ['Days threshold must be greater than zero'])

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

        commands = [call.args[0] for call in run.call_args_list]
        assert ['losetup', '--find', '--show', '--', image] in commands
        assert any(command[:3] == ['cryptsetup', 'luksFormat', '--type'] for command in commands)
        crypt_calls = [call for call in run.call_args_list if call.args[0][0] == 'cryptsetup']
        assert all(b'secret' not in call.args[0] for call in crypt_calls)
        assert all(call.kwargs.get('input') == b'secret\n' for call in crypt_calls[:2])
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
        assert 'recovered' in message
        assert metadata['sessions']['1']['size'] == 6000
        assert not os.path.exists(journal_path)
