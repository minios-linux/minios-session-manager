#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tests for minios_session SessionManager class.
"""

import sys
import os
import json
import pytest
import tempfile
import shutil
from unittest.mock import patch, MagicMock, mock_open

# Add lib directory to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'lib'))


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
            
            assert sm._format_size(0) == "0 B"
            assert sm._format_size(500) == "500 B"

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
            assert call_count[0] >= 2


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
