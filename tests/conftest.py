#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Pytest fixtures for minios-session-manager tests.
"""

import sys
import os
import pytest
import tempfile
import shutil
from unittest.mock import MagicMock, patch

# Add lib directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'lib'))


@pytest.fixture
def temp_sessions_dir():
    """Create a temporary sessions directory for testing."""
    tmpdir = tempfile.mkdtemp(prefix="minios-test-sessions-")
    yield tmpdir
    # Cleanup
    shutil.rmtree(tmpdir, ignore_errors=True)


@pytest.fixture
def mock_proc_cmdline():
    """Mock /proc/cmdline content."""
    return "BOOT_IMAGE=/minios/boot/vmlinuz union=overlayfs"


@pytest.fixture
def mock_proc_filesystems():
    """Mock /proc/filesystems content."""
    return """nodev	sysfs
nodev	tmpfs
nodev	bdev
nodev	proc
nodev	cpuset
nodev	cgroup
nodev	cgroup2
nodev	devtmpfs
nodev	configfs
nodev	debugfs
nodev	tracefs
nodev	securityfs
nodev	sockfs
nodev	bpf
nodev	pipefs
nodev	ramfs
nodev	hugetlbfs
nodev	devpts
	ext3
	ext2
	ext4
	squashfs
	vfat
nodev	overlayfs
nodev	aufs
nodev	fuse
nodev	fusectl
	fuseblk
"""


@pytest.fixture
def sample_session_json():
    """Sample session.json content."""
    return {
        "default": "001",
        "running": "001",
        "sessions": {
            "001": {
                "id": "001",
                "name": "Default Session",
                "mode": "native",
                "created": "2025-01-15T10:00:00",
                "modified": "2025-01-18T14:30:00",
                "version": "5.1.1",
                "edition": "standard"
            },
            "002": {
                "id": "002",
                "name": "Test Session",
                "mode": "sparse",
                "size_mb": 2000,
                "created": "2025-01-16T12:00:00",
                "modified": "2025-01-17T16:00:00",
                "version": "5.1.1",
                "edition": "standard"
            }
        }
    }


@pytest.fixture
def sample_minios_release():
    """Sample /etc/minios-release content."""
    return '''VERSION="5.1.1"
EDITION="standard"
BUILD_DATE="2025-01-15"
CODENAME="bookworm"
'''
