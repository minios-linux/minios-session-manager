#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MiniOS persistence guard.

A lightweight root daemon that monitors the backing storage and the inner
persistence filesystem for a DynFileFS/dynblk (or native/raw/LUKS) session,
forecasts exhaustion, and publishes state to tmpfs for the desktop notifier.

Correctness of persistence is enforced inside dynblk; this guard only warns and,
optionally, helps a graceful shutdown. It never writes to the monitored
persistent filesystem (all its output goes to /run, a tmpfs).

Copyright (C) 2026 MiniOS Linux
SPDX-License-Identifier: GPL-2.0-or-later
"""

import os
import sys
import time
import errno
import signal
import tempfile

RUN_DIR = "/run/minios-persistence"
STATE_FILE = os.path.join(RUN_DIR, "state")
INITRAMFS_RUN_DIR = "/run/initramfs/minios-persistence"
BOOT_WARNINGS_FILE = os.path.join(INITRAMFS_RUN_DIR, "boot-warnings")
PUBLIC_BOOT_WARNINGS_FILE = os.path.join(RUN_DIR, "boot-warnings")
BOOT_STATE_FILE = os.path.join(INITRAMFS_RUN_DIR, "boot-state")

SESSION_PATHS = [
    "/run/initramfs/memory/data/minios/changes",
    "/lib/live/mount/medium/minios/changes",
]

# Sampling cadence: fast in the daemon, the GUI only polls every few minutes.
SAMPLE_INTERVAL = 30
CRITICAL_SAMPLE_INTERVAL = 10
HISTORY_SECONDS = 60 * 60

# Two-sided threshold clamps against the backing filesystem (bytes and percent).
MiB = 1024 * 1024
GiB = 1024 * MiB
THRESHOLDS = {
    # level: (percent, min_bytes, max_bytes, eta_seconds)
    "advisory": (0.05, 512 * MiB, 4 * GiB, 30 * 60),
    "critical": (0.01, 128 * MiB, 1 * GiB, 5 * 60),
    "emergency": (0.0025, 32 * MiB, 256 * MiB, 2 * 60),
}
LEVEL_ORDER = ["ok", "advisory", "critical", "emergency"]


def clamp_threshold(total_bytes, percent, min_bytes, max_bytes):
    """Return the free-byte threshold for a level, clamped on both sides."""
    value = total_bytes * percent
    if value < min_bytes:
        value = min_bytes
    if value > max_bytes:
        value = max_bytes
    return value


def parse_mountinfo(text):
    """Parse /proc/self/mountinfo text into a list of dicts."""
    mounts = []
    for line in text.splitlines():
        fields = line.split(" ")
        if "-" not in fields:
            continue
        sep = fields.index("-")
        if sep + 2 >= len(fields) or len(fields) < 5:
            continue
        mount_point = fields[4].replace("\\040", " ")
        mounts.append({
            "mount_point": mount_point,
            "fstype": fields[sep + 1],
            "device": fields[sep + 2],
            "mount_options": fields[5] if len(fields) > 5 else "",
        })
    return mounts


def find_backing_mount(mounts, target):
    """Longest-prefix mountinfo match for the filesystem that holds `target`."""
    best = None
    for mount in mounts:
        mp = mount["mount_point"]
        if target == mp or target.startswith(mp.rstrip("/") + "/"):
            if best is None or len(mp) > len(best["mount_point"]):
                best = mount
    return best


def read_space(path):
    """Return (free_bytes, total_bytes, free_inodes) for the filesystem at path."""
    st = os.statvfs(path)
    free_bytes = st.f_bavail * st.f_frsize
    total_bytes = st.f_blocks * st.f_frsize
    free_inodes = st.f_favail
    return free_bytes, total_bytes, free_inodes


def estimate_eta_seconds(history):
    """Estimate seconds until the backing filesystem is full from recent samples.

    `history` is a list of (timestamp, free_bytes). Uses the robust slope over
    the retained window; returns None when not draining or not enough data.
    """
    if len(history) < 3:
        return None
    t0, f0 = history[0]
    t1, f1 = history[-1]
    dt = t1 - t0
    if dt <= 0:
        return None
    rate = (f0 - f1) / dt  # bytes consumed per second
    if rate <= 0:
        return None
    return f1 / rate


def compute_level(free_bytes, total_bytes, eta_seconds, thresholds=THRESHOLDS):
    """Return the highest triggered level from space and forecast conditions."""
    level = "ok"
    for candidate in ("advisory", "critical", "emergency"):
        percent, min_bytes, max_bytes, eta_limit = thresholds[candidate]
        space_hit = free_bytes <= clamp_threshold(total_bytes, percent, min_bytes, max_bytes)
        eta_hit = eta_seconds is not None and eta_seconds <= eta_limit
        if space_hit or eta_hit:
            level = candidate
    return level


def worse_level(a, b):
    """Return the more severe of two levels."""
    return a if LEVEL_ORDER.index(a) >= LEVEL_ORDER.index(b) else b


def parse_boot_warnings(text):
    """Parse the initramfs boot-warning log into a list of record dicts."""
    records = []
    for line in text.splitlines():
        parts = line.split("\t", 4)
        if len(parts) != 5:
            continue
        bootid, ts, severity, category, message = parts
        records.append({
            "boot_id": bootid,
            "timestamp": ts,
            "severity": severity,
            "category": category,
            "message": message,
        })
    return records


def ingest_boot_warnings(path=BOOT_WARNINGS_FILE):
    """Read and parse captured boot warnings, tolerating a missing file."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return parse_boot_warnings(handle.read())
    except OSError:
        return []


def write_boot_warnings_atomic(records, path=PUBLIC_BOOT_WARNINGS_FILE):
    """Publish validated boot warnings to world-readable tmpfs for the notifier."""
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".boot-warnings-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            for record in records:
                handle.write("{}\t{}\t{}\t{}\t{}\n".format(
                    record["boot_id"], record["timestamp"], record["severity"],
                    record["category"], record["message"]))
        os.chmod(temp_name, 0o644)
        os.replace(temp_name, path)
        temp_name = None
    finally:
        if temp_name and os.path.exists(temp_name):
            os.unlink(temp_name)


def serialize_state(state):
    """Serialize the state dict to the key=value format the GUI reads."""
    lines = []
    for key in ("boot_level", "level", "degraded", "session", "mode",
                "policy", "autosave", "saved",
                "free_outer_bytes", "free_inner_bytes",
                "free_outer_inodes", "free_inner_inodes",
                "eta_seconds", "boot_warnings_pending", "updated"):
        if key in state and state[key] is not None:
            lines.append(f"{key}={state[key]}")
    return "\n".join(lines) + "\n"


def write_state_atomic(state, path=STATE_FILE):
    """Publish state to tmpfs atomically (temp -> rename), world-readable."""
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".state-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(serialize_state(state))
        os.chmod(temp_name, 0o644)
        os.replace(temp_name, path)
        temp_name = None
    finally:
        if temp_name and os.path.exists(temp_name):
            os.unlink(temp_name)


def read_boot_state(path=BOOT_STATE_FILE):
    """Read the initramfs boot-state marker (boot_level/mode/session)."""
    result = {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if "=" in line:
                    key, value = line.split("=", 1)
                    result[key] = value
    except OSError:
        pass
    return result


def read_squashfs_settings(sessions_dir, session_id):
    """Read the small set of SquashFS settings needed by the desktop helper."""
    if not sessions_dir or not session_id or not session_id.isdigit():
        return {}
    values = {}
    counts = {}
    fields = ("policy", "autosave", "saved")
    prefixes = {field: "session_{}[{}]=".format(field, session_id)
                for field in fields}
    try:
        with open(os.path.join(sessions_dir, "session.conf"), "r",
                  encoding="utf-8", errors="replace") as handle:
            for raw_line in handle:
                line = raw_line.rstrip("\n")
                for field, prefix in prefixes.items():
                    if line.startswith(prefix):
                        counts[field] = counts.get(field, 0) + 1
                        values[field] = line[len(prefix):]
    except OSError:
        return {}
    if any(counts.get(field, 0) > 1 for field in fields):
        return {}

    policy = values.get("policy", "manual")
    if policy not in ("manual", "shutdown"):
        return {}
    autosave = values.get("autosave", "0")
    if autosave not in ("0", "30", "60", "120", "240", "480"):
        return {}
    saved = values.get("saved")
    result = {"policy": policy, "autosave": autosave}
    if saved and "\r" not in saved and "\n" not in saved:
        result["saved"] = saved
    return result


class PersistenceGuard:
    """Samples backing and inner filesystems and publishes state to tmpfs."""

    def __init__(self, sessions_dir=None, state_path=STATE_FILE):
        self.sessions_dir = sessions_dir or self._detect_sessions_dir()
        self.state_path = state_path
        self.history = []  # list of (timestamp, free_bytes)
        self._stop = False

    @staticmethod
    def _detect_sessions_dir():
        for path in SESSION_PATHS:
            if os.path.exists(path):
                return path
        return None

    def _read_mounts(self):
        with open("/proc/self/mountinfo", "r", encoding="utf-8") as handle:
            return parse_mountinfo(handle.read())

    def backing_and_inner(self):
        """Resolve the backing directory and the inner (union upper) path."""
        backing = self.sessions_dir
        inner = None
        if self.sessions_dir:
            # The union upper (inner ext4) is the changes mount when persistence
            # is active; fall back to the sessions dir for native mode.
            for candidate in ("/run/initramfs/memory/changes", "/memory/changes"):
                if os.path.ismount(candidate):
                    inner = candidate
                    break
        return backing, inner

    def sample(self, now=None, boot_warnings=None):
        """Take one measurement and return the state dict."""
        now = now if now is not None else time.time()
        backing, inner = self.backing_and_inner()

        state = {
            "level": "ok",
            "degraded": 0,
            "updated": int(now),
        }

        free_outer = total_outer = None
        if backing and os.path.exists(backing):
            try:
                free_outer, total_outer, free_outer_inodes = read_space(backing)
                state["free_outer_bytes"] = free_outer
                state["free_outer_inodes"] = free_outer_inodes
            except OSError:
                pass

        if inner:
            try:
                free_inner, _total_inner, free_inner_inodes = read_space(inner)
                state["free_inner_bytes"] = free_inner
                state["free_inner_inodes"] = free_inner_inodes
            except OSError:
                pass

        eta = None
        if free_outer is not None and total_outer:
            self.history.append((now, free_outer))
            self.history = [(t, f) for (t, f) in self.history if now - t <= HISTORY_SECONDS]
            eta = estimate_eta_seconds(self.history)
            if eta is not None:
                state["eta_seconds"] = int(eta)
            state["level"] = compute_level(free_outer, total_outer, eta)

        boot = read_boot_state(BOOT_STATE_FILE)
        if boot.get("boot_level") == "failed":
            state["degraded"] = 1
            state["level"] = worse_level(state["level"], "critical")
        if boot.get("mode"):
            state["mode"] = boot["mode"]
        if boot.get("session"):
            state["session"] = boot["session"]
        state["boot_level"] = boot.get("boot_level", "ok")
        if state.get("mode") == "squashfs" and state.get("session"):
            state.update(read_squashfs_settings(self.sessions_dir, state["session"]))

        if boot_warnings is None:
            boot_warnings = ingest_boot_warnings(BOOT_WARNINGS_FILE)
        state["boot_warnings_pending"] = len(boot_warnings)
        return state

    def sample_interval(self, level):
        return CRITICAL_SAMPLE_INTERVAL if level in ("critical", "emergency") else SAMPLE_INTERVAL

    def run_once(self):
        boot_warnings = ingest_boot_warnings(BOOT_WARNINGS_FILE)
        state = self.sample(boot_warnings=boot_warnings)
        write_boot_warnings_atomic(boot_warnings)
        write_state_atomic(state, self.state_path)
        return state

    def stop(self, *args):
        self._stop = True

    def run(self):
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)
        level = "ok"
        while not self._stop:
            try:
                state = self.run_once()
                level = state.get("level", "ok")
            except Exception as error:  # never die on a transient error
                print(f"minios-persistence-guard: {error}", file=sys.stderr)
            for _ in range(self.sample_interval(level)):
                if self._stop:
                    break
                time.sleep(1)


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    sessions_dir = argv[0] if argv else None
    guard = PersistenceGuard(sessions_dir=sessions_dir)
    if not guard.sessions_dir:
        # Nothing to monitor; publish a minimal ok state and exit cleanly.
        try:
            boot_warnings = ingest_boot_warnings()
            write_boot_warnings_atomic(boot_warnings)
            write_state_atomic({"level": "ok", "degraded": 0,
                                "boot_warnings_pending": len(boot_warnings)})
        except OSError as error:
            if error.errno not in (errno.EACCES, errno.EROFS):
                raise
        return 0
    guard.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
