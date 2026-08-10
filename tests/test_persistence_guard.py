#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for the MiniOS persistence guard pure logic."""

import os
import sys

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
