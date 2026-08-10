#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for the persistence alert helper pure logic."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import minios_persistence_alert as alert  # noqa: E402


class TestStateReading:
    def test_read_state(self, tmp_path):
        p = tmp_path / "state"
        p.write_text("level=critical\ndegraded=0\nmode=dynfilefs\n")
        state = alert.read_state(str(p))
        assert state["level"] == "critical"
        assert state["mode"] == "dynfilefs"

    def test_read_state_missing(self, tmp_path):
        assert alert.read_state(str(tmp_path / "nope")) == {}


class TestAcknowledge:
    def test_ack_round_trip(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
        assert not alert.already_acknowledged("boot-xyz")
        alert.mark_acknowledged("boot-xyz")
        assert alert.already_acknowledged("boot-xyz")
        # A different boot id is independent.
        assert not alert.already_acknowledged("boot-other")


class TestBootWarnings:
    def test_format_groups_by_category(self):
        records = [
            {"boot_id": "b", "timestamp": "1", "severity": "warning",
             "category": "space", "message": "low space"},
            {"boot_id": "b", "timestamp": "2", "severity": "error",
             "category": "persistence", "message": "backend gone"},
            {"boot_id": "b", "timestamp": "3", "severity": "warning",
             "category": "space", "message": "still low"},
        ]
        text = alert.format_boot_warnings(records)
        assert "low space" in text and "still low" in text
        assert "backend gone" in text
        # Space category groups both of its messages.
        assert text.count("Disk space") == 1

    def test_worst_severity(self):
        assert alert.worst_severity([{"severity": "warning"}]) == "warning"
        assert alert.worst_severity(
            [{"severity": "warning"}, {"severity": "error"}]) == "error"

    def test_empty_format(self):
        assert alert.format_boot_warnings([]) == ""


class TestLevelMessage:
    def test_ok_has_no_message(self):
        assert alert.level_message({"level": "ok", "degraded": "0"}) is None

    def test_emergency_is_error(self):
        title, body, severity = alert.level_message({"level": "emergency", "degraded": "0"})
        assert severity == "error"

    def test_degraded_is_error(self):
        info = alert.level_message({"level": "ok", "degraded": "1"})
        assert info is not None and info[2] == "error"

    def test_advisory_is_warning(self):
        info = alert.level_message({"level": "advisory", "degraded": "0"})
        assert info[2] == "warning"
