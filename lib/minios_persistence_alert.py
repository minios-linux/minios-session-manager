#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MiniOS persistence alert helper (unprivileged, per graphical session).

Reads the tmpfs state published by minios-persistence-guard and surfaces, in the
MiniOS GTK style:
  * any unplanned boot warning the user may have missed on the console (once per
    boot), and
  * low-space / degraded persistence conditions.

It never writes to the persistent filesystem; its only state (the per-boot
acknowledgement) lives in the user's tmpfs runtime directory.

Copyright (C) 2026 MiniOS Linux
SPDX-License-Identifier: GPL-2.0-or-later
"""

import os
import sys
import gettext

_ = gettext.gettext

STATE_FILE = "/run/minios-persistence/state"
BOOT_WARNINGS_FILE = "/run/initramfs/minios-persistence/boot-warnings"

# The GUI only needs to poll every few minutes; the guard does the fast sampling.
POLL_SECONDS = 180

CATEGORY_LABELS = {
    "persistence": _("Persistence"),
    "mode": _("Storage mode"),
    "space": _("Disk space"),
    "fsck": _("Filesystem check"),
    "other": _("Other"),
}


def read_state(path=STATE_FILE):
    """Parse the guard's key=value state file into a dict."""
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


def current_boot_id():
    try:
        with open("/proc/sys/kernel/random/boot_id", encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError:
        return "unknown"


def runtime_dir():
    return os.environ.get("XDG_RUNTIME_DIR") or os.path.join("/tmp", f"minios-{os.getuid()}")


def ack_path(boot_id=None):
    boot_id = boot_id or current_boot_id()
    return os.path.join(runtime_dir(), f"minios-persistence-{boot_id}.ack")


def already_acknowledged(boot_id=None):
    return os.path.exists(ack_path(boot_id))


def mark_acknowledged(boot_id=None):
    path = ack_path(boot_id)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("seen\n")
    except OSError:
        pass


def parse_boot_warnings(text):
    """Parse the tab-separated boot-warning log into record dicts."""
    records = []
    for line in text.splitlines():
        parts = line.split("\t", 4)
        if len(parts) == 5:
            records.append({
                "boot_id": parts[0],
                "timestamp": parts[1],
                "severity": parts[2],
                "category": parts[3],
                "message": parts[4],
            })
    return records


def read_boot_warnings(path=BOOT_WARNINGS_FILE):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return parse_boot_warnings(handle.read())
    except OSError:
        return []


def format_boot_warnings(records):
    """Group boot-warning records by category into human-readable text."""
    if not records:
        return ""
    by_category = {}
    for record in records:
        by_category.setdefault(record["category"], []).append(record["message"])
    blocks = []
    for category, messages in by_category.items():
        label = CATEGORY_LABELS.get(category, category)
        lines = "\n".join(f"  \u2022 {m}" for m in messages)
        blocks.append(f"<b>{label}</b>\n{lines}")
    return "\n\n".join(blocks)


def worst_severity(records):
    return "error" if any(r["severity"] == "error" for r in records) else "warning"


def level_message(state):
    """Return (title, body, severity) for a persistence level, or None for ok."""
    level = state.get("level", "ok")
    degraded = state.get("degraded") == "1"
    if degraded or level == "emergency":
        return (_("Persistence is out of space"),
                _("The persistence device is full. New changes may not be saved. "
                  "Free up space or save your work and restart."),
                "error")
    if level == "critical":
        return (_("Persistence device almost full"),
                _("Very little space remains for saved changes. "
                  "Free up space or remove old sessions soon."),
                "warning")
    if level == "advisory":
        return (_("Low space on persistence device"),
                _("Space for saved changes is running low."),
                "warning")
    return None


# --- GTK front end (imported lazily so the pure logic stays testable) ---------

def _run_gui():
    import gi
    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk, GLib
    from minios_gui import apply_minios_css

    app_css = None
    for candidate in (
        "/usr/share/minios-session-manager/style.css",
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "share", "styles", "style.css"),
    ):
        if os.path.isfile(candidate):
            app_css = candidate
            break
    apply_minios_css(app_css)

    def show_dialog(title, body, severity, secondary_markup=None):
        message_type = Gtk.MessageType.ERROR if severity == "error" else Gtk.MessageType.WARNING
        dialog = Gtk.MessageDialog(message_type=message_type,
                                   buttons=Gtk.ButtonsType.NONE, text=title)
        if secondary_markup:
            dialog.format_secondary_markup(body + "\n\n" + secondary_markup)
        else:
            dialog.format_secondary_text(body)
        dialog.add_button(_("Dismiss"), Gtk.ResponseType.CLOSE)
        open_button = dialog.add_button(_("Open Session Manager"), 1)
        open_button.get_style_context().add_class("suggested-action")
        dialog.set_default_response(1)
        response = dialog.run()
        dialog.destroy()
        if response == 1:
            GLib.spawn_command_line_async("minios-session-manager")

    # One-time missed-boot-warnings dialog.
    if not already_acknowledged():
        records = read_boot_warnings()
        boot_id = current_boot_id()
        records = [r for r in records if r["boot_id"] in (boot_id, "unknown")]
        if records:
            show_dialog(_("Some startup warnings need your attention"),
                        _("These warnings appeared during startup:"),
                        worst_severity(records),
                        format_boot_warnings(records))
        mark_acknowledged()

    state = {"last_level": "ok"}

    def poll():
        current = read_state()
        level = current.get("level", "ok")
        if level != state["last_level"] and level in ("critical", "emergency"):
            info = level_message(current)
            if info:
                show_dialog(info[0], info[1], info[2])
        state["last_level"] = level
        return True

    poll()
    GLib.timeout_add_seconds(POLL_SECONDS, poll)
    Gtk.main()


def main(argv=None):
    try:
        _run_gui()
    except Exception as error:
        print(f"minios-persistence-alert: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
