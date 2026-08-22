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


class TestBootWarningSource:
    def test_alert_reads_guard_published_warning_copy(self):
        assert alert.BOOT_WARNINGS_FILE == "/run/minios-persistence/boot-warnings"


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

    def test_format_escapes_warning_markup(self):
        records = [{
            "boot_id": "b", "timestamp": "1", "severity": "warning",
            "category": "other", "message": "<b>not markup</b> & text",
        }]
        text = alert.format_boot_warnings(records)
        assert "&lt;b&gt;not markup&lt;/b&gt; &amp; text" in text
        assert "<b>not markup</b>" not in text

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


class TestSquashfsTray:
    def test_icon_appears_only_for_active_squashfs_session(self):
        assert alert.squashfs_session_id({
            "boot_level": "ok", "mode": "squashfs", "session": "7"}) == "7"
        assert alert.squashfs_session_id({
            "boot_level": "ok", "mode": "raw", "session": "7"}) is None
        assert alert.squashfs_session_id({
            "boot_level": "failed", "mode": "squashfs", "session": "7"}) is None
        assert alert.squashfs_session_id({
            "boot_level": "ok", "mode": "squashfs", "session": "../7"}) is None

    def test_save_command_uses_privileged_session_backend(self):
        assert alert.save_command("3", "/usr/bin/minios-session") == [
            "pkexec", "/usr/bin/minios-session", "save", "3", "--json"]

    def test_save_result_prefers_cli_message(self):
        success, message = alert.save_result(
            0, '{"success": true, "message": "saved"}\n', "")
        assert success is True and message == "saved"
        success, message = alert.save_result(1, "", "permission denied")
        assert success is False and message == "permission denied"


class TestSquashfsSaveUx:
    def test_progress_save_command_uses_same_backend(self):
        assert alert.save_command("3", "/usr/bin/minios-session", progress=True) == [
            "pkexec", "/usr/bin/minios-session", "save", "3", "--json", "--progress"]

    def test_settings_command_enforces_supported_intervals(self):
        assert alert.settings_command(
            "3", shutdown=True, autosave=60,
            command="/usr/bin/minios-session") == [
                "pkexec", "/usr/bin/minios-session", "settings", "3",
                "--shutdown", "on", "--autosave", "60", "--json"]
        try:
            alert.settings_command("3", autosave=10)
        except ValueError:
            pass
        else:
            raise AssertionError("10-minute automatic saving must not be accepted")

    def test_streamed_save_result_uses_final_result_message(self):
        output = (
            '{"type":"phase","phase":"compress"}\n'
            '{"success":true,"message":"saved"}\n')
        assert alert.save_result(0, output, "") == (True, "saved")

    def test_one_time_squashfs_notice_marker_is_persistent(self, tmp_path):
        path = str(tmp_path / "config" / "notice")
        assert not alert.squashfs_notice_seen(path)
        assert alert.mark_squashfs_notice_seen(path)
        assert alert.squashfs_notice_seen(path)
