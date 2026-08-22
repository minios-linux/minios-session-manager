#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for the MiniOS persistence guard pure logic."""

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import minios_persistence_guard as guard  # noqa: E402

MiB = 1024 * 1024
GiB = 1024 * MiB


class TestThresholds:
    def test_clamp_is_two_sided(self):
        # 5% of 100 GiB is 5 GiB, clamped down to the 4 GiB advisory cap.
        assert guard.clamp_threshold(100 * GiB, 0.05, 512 * MiB, 4 * GiB) == 4 * GiB
        # 5% of 1 GiB is ~51 MiB, clamped up to the 512 MiB floor.
        assert guard.clamp_threshold(1 * GiB, 0.05, 512 * MiB, 4 * GiB) == 512 * MiB


class TestLevel:
    def test_ok_when_plenty(self):
        assert guard.compute_level(50 * GiB, 100 * GiB, None) == "ok"

    def test_advisory_by_space(self):
        # Below the advisory clamp (4 GiB) but above critical.
        assert guard.compute_level(3 * GiB, 100 * GiB, None) == "advisory"

    def test_emergency_by_space(self):
        assert guard.compute_level(16 * MiB, 100 * GiB, None) == "emergency"

    def test_forecast_can_raise_level(self):
        # Plenty of space, but full in 90 seconds -> emergency by forecast.
        assert guard.compute_level(50 * GiB, 100 * GiB, 90) == "emergency"

    def test_worse_level(self):
        assert guard.worse_level("ok", "critical") == "critical"
        assert guard.worse_level("emergency", "advisory") == "emergency"


class TestMountinfo:
    SAMPLE = (
        "24 30 0:22 / /run rw,nosuid shared:5 - tmpfs tmpfs rw\n"
        "40 24 0:33 / /run/initramfs/memory/data rw shared:9 - ext4 /dev/sdb1 rw\n"
        "41 40 0:34 / /run/initramfs/memory/changes rw shared:9 - ext4 /dev/loop0 rw\n"
    )

    def test_longest_prefix(self):
        mounts = guard.parse_mountinfo(self.SAMPLE)
        target = "/run/initramfs/memory/data/minios/changes"
        backing = guard.find_backing_mount(mounts, target)
        assert backing["device"] == "/dev/sdb1"
        assert backing["fstype"] == "ext4"

    def test_inner_is_separate(self):
        mounts = guard.parse_mountinfo(self.SAMPLE)
        inner = guard.find_backing_mount(mounts, "/run/initramfs/memory/changes")
        assert inner["device"] == "/dev/loop0"


class TestForecast:
    def test_no_eta_when_not_draining(self):
        history = [(0, 100), (10, 100), (20, 100)]
        assert guard.estimate_eta_seconds(history) is None

    def test_eta_from_slope(self):
        # Losing 10 bytes/s, 100 bytes left at the last sample -> 10 s.
        history = [(0, 300), (10, 200), (20, 100)]
        assert guard.estimate_eta_seconds(history) == 10


class TestBootWarnings:
    def test_parse_records(self):
        text = (
            "boot-1\t12.3\twarning\tspace\tdisk almost full\n"
            "boot-1\t13.0\terror\tpersistence\tbackend gone\n"
            "malformed line without tabs\n"
        )
        records = guard.parse_boot_warnings(text)
        assert len(records) == 2
        assert records[0]["category"] == "space"
        assert records[1]["severity"] == "error"

    def test_ingest_missing_file(self, tmp_path):
        assert guard.ingest_boot_warnings(str(tmp_path / "nope")) == []


    def test_public_copy_is_world_readable(self, tmp_path):
        path = str(tmp_path / "boot-warnings")
        records = [{
            "boot_id": "boot-1", "timestamp": "12.3", "severity": "warning",
            "category": "space", "message": "disk almost full",
        }]
        guard.write_boot_warnings_atomic(records, path)
        assert guard.ingest_boot_warnings(path) == records
        assert (os.stat(path).st_mode & 0o004) != 0


class TestState:
    def test_serialize_round_trip(self):
        state = {
            "level": "critical",
            "degraded": 1,
            "free_outer_bytes": 12345,
            "boot_warnings_pending": 2,
            "mode": "dynfilefs",
        }
        text = guard.serialize_state(state)
        assert "level=critical" in text
        assert "degraded=1" in text
        assert "free_outer_bytes=12345" in text
        assert "mode=dynfilefs" in text

    def test_write_state_atomic(self, tmp_path):
        path = str(tmp_path / "state")
        guard.write_state_atomic({"level": "ok", "degraded": 0}, path)
        with open(path, encoding="utf-8") as handle:
            content = handle.read()
        assert "level=ok" in content
        # World-readable so an unprivileged notifier can read it.
        assert (os.stat(path).st_mode & 0o004) != 0


    def test_run_once_publishes_boot_warning_copy(self, tmp_path, monkeypatch):
        source = tmp_path / "private-warnings"
        source.write_text("boot-1\t1.0\twarning\tmode\tnew session created\n")
        public = tmp_path / "public-warnings"
        state_path = tmp_path / "state"
        monkeypatch.setattr(guard, "BOOT_WARNINGS_FILE", str(source))
        original = guard.write_boot_warnings_atomic
        monkeypatch.setattr(
            guard, "write_boot_warnings_atomic",
            lambda records: original(records, str(public)))

        g = guard.PersistenceGuard(sessions_dir=None, state_path=str(state_path))
        g.backing_and_inner = lambda: (None, None)
        state = g.run_once()
        assert state["boot_warnings_pending"] == 1
        records = guard.ingest_boot_warnings(str(public))
        assert len(records) == 1
        assert records[0]["message"] == "new session created"
        assert (os.stat(public).st_mode & 0o004) != 0

    def test_read_boot_state(self, tmp_path):
        p = tmp_path / "boot-state"
        p.write_text("boot_level=failed\nmode=dynfilefs\nsession=3\n")
        result = guard.read_boot_state(str(p))
        assert result["boot_level"] == "failed"
        assert result["mode"] == "dynfilefs"


class TestSampleIntegration:
    def test_degraded_boot_state_forces_critical(self, tmp_path, monkeypatch):
        monkeypatch.setattr(guard, "BOOT_STATE_FILE", str(tmp_path / "boot-state"))
        monkeypatch.setattr(guard, "BOOT_WARNINGS_FILE", str(tmp_path / "boot-warnings"))
        (tmp_path / "boot-state").write_text("boot_level=failed\nmode=dynfilefs\nsession=1\n")

        g = guard.PersistenceGuard(sessions_dir=None)
        g.backing_and_inner = lambda: (None, None)
        state = g.sample(now=1000)
        assert state["degraded"] == 1
        assert state["level"] in ("critical", "emergency")
        assert state["mode"] == "dynfilefs"


def test_systemd_unit_creates_runtime_directory_before_sandboxing():
    unit = (Path(__file__).resolve().parent.parent /
            "share" / "systemd" / "minios-persistence-guard.service")
    text = unit.read_text()
    assert "RuntimeDirectory=minios-persistence" in text
    assert "RuntimeDirectoryMode=0755" in text
    assert "ReadWritePaths=/run/minios-persistence" in text


def test_squashfs_settings_are_published_for_tray(tmp_path, monkeypatch):
    sessions = tmp_path / "changes"
    sessions.mkdir()
    (sessions / "session.conf").write_text(
        "default=1\nrunning=1\n"
        "session_mode[1]=squashfs\n"
        "session_policy[1]=shutdown\n"
        "session_autosave[1]=60\n"
        "session_saved[1]=2026-08-18T13:00:04Z\n")
    boot_state = tmp_path / "boot-state"
    boot_state.write_text("boot_level=ok\nmode=squashfs\nsession=1\n")
    monkeypatch.setattr(guard, "BOOT_STATE_FILE", str(boot_state))
    monkeypatch.setattr(guard, "BOOT_WARNINGS_FILE", str(tmp_path / "warnings"))

    g = guard.PersistenceGuard(sessions_dir=str(sessions))
    g.backing_and_inner = lambda: (None, None)
    state = g.sample(now=1000)
    assert state["policy"] == "shutdown"
    assert state["autosave"] == "60"
    assert state["saved"] == "2026-08-18T13:00:04Z"
    serialized = guard.serialize_state(state)
    assert "policy=shutdown" in serialized
    assert "autosave=60" in serialized
