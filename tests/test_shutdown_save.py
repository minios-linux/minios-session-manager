#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for the optional Session Manager periodic-save schedule."""

from pathlib import Path
import os
import subprocess
import time


ROOT = Path(__file__).resolve().parent.parent
AUTOSAVE_SERVICE = ROOT / "share" / "systemd" / "minios-session-autosave.service"
AUTOSAVE_TIMER = ROOT / "share" / "systemd" / "minios-session-autosave.timer"
AUTOSAVE_SYSV = ROOT / "share" / "init.d" / "minios-session-autosave"
DEBIAN_RULES = ROOT / "debian" / "rules"


def test_periodic_save_timer_checks_every_30_minutes():
    service = AUTOSAVE_SERVICE.read_text()
    timer = AUTOSAVE_TIMER.read_text()
    assert "ExecStart=/usr/bin/minios-session autosave" in service
    assert "OnBootSec=30min" in timer
    assert "OnUnitActiveSec=30min" in timer
    assert "WantedBy=timers.target" in timer


def test_session_manager_does_not_own_shutdown_save_service():
    assert not (ROOT / "bin" / "minios-session-shutdown-save").exists()
    assert not (ROOT / "share" / "systemd" /
                "minios-session-shutdown-save.service").exists()


def test_devuan_registers_guard_and_periodic_autosave():
    autosave = AUTOSAVE_SYSV.read_text()
    rules = DEBIAN_RULES.read_text()
    assert "# Default-Start:     2 3 4 5" in autosave
    assert "# Default-Stop:      0 1 6" in autosave
    assert "INTERVAL=${MINIOS_AUTOSAVE_POLL_SECONDS:-1800}" in autosave
    assert "dh_installinit --name=minios-persistence-guard --only-scripts" in rules
    assert "dh_installinit --name=minios-session-autosave --only-scripts" in rules


def test_devuan_periodic_daemon_uses_existing_autosave_backend(tmp_path):
    called = tmp_path / "called"
    command = tmp_path / "minios-session"
    command.write_text("#!/bin/sh\nprintf '%s\n' \"$*\" > \"$MINIOS_TEST_CALLED\"\n")
    command.chmod(0o755)
    env = os.environ.copy()
    env.update({
        "MINIOS_AUTOSAVE_COMMAND": str(command),
        "MINIOS_AUTOSAVE_POLL_SECONDS": "1",
        "MINIOS_TEST_CALLED": str(called),
    })
    process = subprocess.Popen([str(AUTOSAVE_SYSV), "daemon"], env=env)
    try:
        deadline = time.time() + 4
        while time.time() < deadline and not called.exists():
            time.sleep(0.05)
        assert called.read_text().strip() == "autosave --json"
    finally:
        process.terminate()
        process.wait(timeout=3)
