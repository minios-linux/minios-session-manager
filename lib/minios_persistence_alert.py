#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MiniOS persistence alert helper (unprivileged, per graphical session).

Reads the tmpfs state published by minios-persistence-guard and surfaces, in the
MiniOS GTK style:
  * any unplanned boot warning the user may have missed on the console (once per
    boot), and
  * low-space / degraded persistence conditions.

It writes no persistence data itself. Per-boot warning acknowledgement stays in
tmpfs; one small first-use marker is stored in the user's config so it becomes
part of the session only through the normal save mechanism.

Copyright (C) 2026 MiniOS Linux
SPDX-License-Identifier: GPL-2.0-or-later
"""

import os
import html
import sys
import json
import gettext
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timezone

gettext.bindtextdomain('minios-session-manager', '/usr/share/locale')
gettext.textdomain('minios-session-manager')
_ = gettext.gettext

STATE_FILE = "/run/minios-persistence/state"
BOOT_WARNINGS_FILE = "/run/minios-persistence/boot-warnings"

# The GUI only needs to poll every few minutes; the guard does the fast sampling.
POLL_SECONDS = 180
TRAY_POLL_SECONDS = 5
SESSION_COMMAND = "/usr/bin/minios-session"
SQUASHFS_AUTOSAVE_INTERVALS = (0, 30, 60, 120, 240, 480)

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


def squashfs_notice_path():
    config_home = os.environ.get("XDG_CONFIG_HOME")
    if not config_home:
        config_home = os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(config_home, "minios-session-manager", "squashfs-notice-seen")


def squashfs_notice_seen(path=None):
    return os.path.exists(path or squashfs_notice_path())


def mark_squashfs_notice_seen(path=None):
    path = path or squashfs_notice_path()
    try:
        directory = os.path.dirname(path)
        os.makedirs(directory, mode=0o700, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("seen\n")
        return True
    except OSError:
        return False


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
        label = html.escape(str(CATEGORY_LABELS.get(category, category)), quote=False)
        lines = "\n".join(
            "  \u2022 {}".format(html.escape(str(message), quote=False))
            for message in messages)
        blocks.append("<b>{}</b>\n{}".format(label, lines))
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


def squashfs_session_id(state):
    """Return the active SquashFS session ID exposed by the guard, if any."""
    session = state.get("session", "")
    if (state.get("boot_level", "ok") == "ok" and
            state.get("mode") == "squashfs" and session.isdigit()):
        return session
    return None


def save_command(session_id, command=SESSION_COMMAND, progress=False):
    if not isinstance(session_id, str) or not session_id.isdigit():
        raise ValueError("invalid session id")
    argv = ["pkexec", command, "save", session_id, "--json"]
    if progress:
        argv.append("--progress")
    return argv


def settings_command(session_id, shutdown=None, autosave=None, command=SESSION_COMMAND):
    if not isinstance(session_id, str) or not session_id.isdigit():
        raise ValueError("invalid session id")
    argv = ["pkexec", command, "settings", session_id]
    if shutdown is not None:
        argv.extend(["--shutdown", "on" if shutdown else "off"])
    if autosave is not None:
        autosave = int(autosave)
        if autosave not in SQUASHFS_AUTOSAVE_INTERVALS:
            raise ValueError("invalid autosave interval")
        argv.extend(["--autosave", str(autosave)])
    argv.append("--json")
    return argv


def save_phase_text(phase):
    return {
        "prepare": _("Preparing session..."),
        "inventory": _("Scanning session changes..."),
        "capture": _("Collecting session changes..."),
        "compress": _("Compressing session..."),
        "verify": _("Verifying session..."),
        "publish": _("Finishing session save..."),
        "complete": _("Finishing session save..."),
    }.get(phase, _("Saving session..."))


def format_saved_time(value):
    if not value:
        return None
    try:
        stamp = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return stamp.astimezone().strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OverflowError):
        return None


def squashfs_tooltip(state, session_id):
    policy = state.get("policy", "manual")
    policy_text = (_("Automatic save at shutdown") if policy == "shutdown" else
                   _("Manual saves only"))
    text = _("SquashFS session #{} · {}").format(session_id, policy_text)
    saved = format_saved_time(state.get("saved"))
    if saved:
        text += _(" · Last saved {}").format(saved)
    return text


def save_result(returncode, stdout, stderr):
    """Return (success, message) from a single JSON result or streamed NDJSON."""
    message = (stderr or "").strip()
    lines = [line.strip() for line in (stdout or "").splitlines() if line.strip()]
    for line in reversed(lines):
        try:
            document = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(document, dict) and document.get("message"):
            message = str(document["message"])
            break
    if not message:
        message = _("Session saved successfully") if returncode == 0 else _(
            "The session could not be saved")
    return returncode == 0, message


# --- GTK front end (imported lazily so the pure logic stays testable) ---------

def _run_gui():
    import gi
    gi.require_version("Gtk", "3.0")
    from gi.repository import Gio, Gtk, GLib
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

    def send_notification(summary, body):
        try:
            connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            parameters = GLib.Variant(
                '(susssasa{sv}i)',
                (_('MiniOS Session Manager'), 0, 'document-save',
                 summary, body, [], {}, 5000))
            connection.call_sync(
                'org.freedesktop.Notifications', '/org/freedesktop/Notifications',
                'org.freedesktop.Notifications', 'Notify', parameters,
                GLib.VariantType.new('(u)'), Gio.DBusCallFlags.NONE, 2000, None)
            return True
        except Exception:
            return False

    state = {
        "last_level": "ok", "session": None, "policy": "manual",
        "autosave": 0, "saved": None, "settings_active": False,
        "syncing_menu": False, "settings_override_until": 0.0,
        "saved_override_until": 0.0, "notice_shown": False,
    }
    saving = {
        "active": False, "dialog": None, "label": None,
        "dialog_shown": False, "phase": "prepare",
    }

    tray = Gtk.StatusIcon()
    tray.set_from_icon_name("document-save")
    tray.set_title(_("MiniOS SquashFS Session"))
    tray.set_visible(False)

    menu = Gtk.Menu()
    save_item = Gtk.MenuItem(label=_("Save Now"))
    menu.append(save_item)
    menu.append(Gtk.SeparatorMenuItem())

    shutdown_item = Gtk.CheckMenuItem(label=_("Save automatically at shutdown"))
    menu.append(shutdown_item)

    periodic_item = Gtk.MenuItem(label=_("Periodic save"))
    periodic_menu = Gtk.Menu()
    autosave_items = {}
    first_autosave_item = None
    for minutes, label_text in (
            (0, _("Off")), (30, _("Every 30 minutes")),
            (60, _("Every 1 hour")), (120, _("Every 2 hours")),
            (240, _("Every 4 hours")), (480, _("Every 8 hours"))):
        item = Gtk.RadioMenuItem.new_with_label_from_widget(
            first_autosave_item, label_text)
        if first_autosave_item is None:
            first_autosave_item = item
        item.autosave_minutes = minutes
        autosave_items[minutes] = item
        periodic_menu.append(item)
    periodic_item.set_submenu(periodic_menu)
    menu.append(periodic_item)
    menu.append(Gtk.SeparatorMenuItem())

    open_item = Gtk.MenuItem(label=_("Open Session Manager"))
    menu.append(open_item)
    menu.show_all()

    def open_manager(*_args):
        GLib.spawn_command_line_async("minios-session-manager")

    def sync_menu():
        state["syncing_menu"] = True
        try:
            shutdown_item.set_active(state.get("policy") == "shutdown")
            try:
                interval = int(state.get("autosave") or 0)
            except (TypeError, ValueError):
                interval = 0
            autosave_items.get(interval, autosave_items[0]).set_active(True)
        finally:
            state["syncing_menu"] = False

    def update_tooltip():
        session_id = state.get("session")
        if session_id:
            tray.set_tooltip_text(squashfs_tooltip(state, session_id))

    def update_controls():
        enabled = bool(state.get("session")) and not saving["active"]
        save_item.set_sensitive(enabled)
        settings_enabled = enabled and not state["settings_active"]
        shutdown_item.set_sensitive(settings_enabled)
        periodic_item.set_sensitive(settings_enabled)

    def create_save_dialog():
        dialog = Gtk.Dialog(title=_("Saving Session"), modal=False)
        dialog.set_deletable(False)
        dialog.set_resizable(False)
        dialog.set_default_size(380, 120)
        box = dialog.get_content_area()
        box.set_spacing(14)
        box.set_margin_start(20)
        box.set_margin_end(20)
        box.set_margin_top(20)
        box.set_margin_bottom(20)
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14)
        spinner = Gtk.Spinner()
        spinner.start()
        row.pack_start(spinner, False, False, 0)
        label = Gtk.Label(label=save_phase_text(saving["phase"]))
        label.set_line_wrap(True)
        row.pack_start(label, True, True, 0)
        box.pack_start(row, True, True, 0)
        return dialog, label

    def update_save_phase(phase):
        saving["phase"] = phase
        if saving.get("label") is not None:
            saving["label"].set_text(save_phase_text(phase))
        return False

    def show_save_dialog_if_needed():
        if not saving["active"] or saving["dialog"] is not None:
            return False
        dialog, label = create_save_dialog()
        saving["dialog"] = dialog
        saving["label"] = label
        saving["dialog_shown"] = True
        dialog.show_all()
        return False

    def finish_save(session_id, returncode, stdout, stderr):
        dialog = saving.get("dialog")
        if dialog is not None:
            dialog.destroy()
        shown = saving.get("dialog_shown", False)
        saving.update({
            "active": False, "dialog": None, "label": None,
            "dialog_shown": False, "phase": "prepare",
        })
        success, message = save_result(returncode, stdout, stderr)
        if success:
            state["saved"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            state["saved_override_until"] = time.monotonic() + 35.0
            update_tooltip()
            if not shown:
                send_notification(
                    _("Session saved"),
                    _("SquashFS session #{} was saved successfully.").format(session_id))
        else:
            show_dialog(_("Failed to save session"), message, "error")
        update_controls()
        return False

    def save_now(*_args):
        current = read_state()
        session_id = squashfs_session_id(current)
        if not session_id or saving["active"]:
            return
        state["session"] = session_id
        saving["active"] = True
        saving["phase"] = "prepare"
        saving["dialog_shown"] = False
        update_controls()
        GLib.timeout_add(500, show_save_dialog_if_needed)

        def worker():
            output_lines = []
            try:
                with tempfile.TemporaryFile() as error_file:
                    process = subprocess.Popen(
                        save_command(session_id, progress=True),
                        stdout=subprocess.PIPE, stderr=error_file,
                        universal_newlines=True)
                    for line in process.stdout:
                        output_lines.append(line)
                        try:
                            event = json.loads(line)
                        except (ValueError, TypeError):
                            continue
                        if isinstance(event, dict) and event.get("type") == "phase":
                            GLib.idle_add(update_save_phase, event.get("phase"))
                    process.wait()
                    error_file.seek(0)
                    stderr = error_file.read().decode("utf-8", "replace")
                    result = (process.returncode, "".join(output_lines), stderr)
            except Exception as error:
                result = (1, "", str(error))
            GLib.idle_add(finish_save, session_id, *result)

        thread = threading.Thread(target=worker)
        thread.daemon = True
        thread.start()

    def finish_settings(returncode, stdout, stderr, desired_policy, desired_autosave):
        state["settings_active"] = False
        success, message = save_result(returncode, stdout, stderr)
        if success:
            if desired_policy is not None:
                state["policy"] = desired_policy
            if desired_autosave is not None:
                state["autosave"] = desired_autosave
            state["settings_override_until"] = time.monotonic() + 35.0
            sync_menu()
            update_tooltip()
        else:
            sync_menu()
            show_dialog(_("Failed to update save settings"), message, "error")
        update_controls()
        return False

    def run_settings(command, desired_policy=None, desired_autosave=None):
        if state["settings_active"] or not state.get("session"):
            return
        state["settings_active"] = True
        update_controls()

        def worker():
            try:
                process = subprocess.Popen(
                    command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    universal_newlines=True)
                stdout, stderr = process.communicate()
                result = (process.returncode, stdout, stderr)
            except Exception as error:
                result = (1, "", str(error))
            GLib.idle_add(
                finish_settings, *result, desired_policy, desired_autosave)

        thread = threading.Thread(target=worker)
        thread.daemon = True
        thread.start()

    def on_shutdown_toggled(item):
        if state["syncing_menu"] or state["settings_active"] or not state.get("session"):
            return
        enabled = item.get_active()
        desired = "shutdown" if enabled else "manual"
        if desired == state.get("policy"):
            return
        run_settings(
            settings_command(state["session"], shutdown=enabled),
            desired_policy=desired)

    def confirm_periodic_save(minutes):
        if minutes == 0:
            return True
        dialog = Gtk.MessageDialog(
            message_type=Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.NONE,
            text=_("Enable periodic session saving?"))
        dialog.format_secondary_text(_(
            "Periodic saving increases CPU usage and writes to storage. "
            "With the current SquashFS mode each save rebuilds the snapshot. "
            "An interval of 1 hour or longer is recommended."))
        dialog.add_button(_("Cancel"), Gtk.ResponseType.CANCEL)
        enable = dialog.add_button(_("Enable Periodic Save"), Gtk.ResponseType.OK)
        enable.get_style_context().add_class("suggested-action")
        dialog.set_default_response(Gtk.ResponseType.CANCEL)
        accepted = dialog.run() == Gtk.ResponseType.OK
        dialog.destroy()
        return accepted

    def on_autosave_toggled(item):
        if (not item.get_active() or state["syncing_menu"] or
                state["settings_active"] or not state.get("session")):
            return
        minutes = item.autosave_minutes
        try:
            current = int(state.get("autosave") or 0)
        except (TypeError, ValueError):
            current = 0
        if minutes == current:
            return
        if not confirm_periodic_save(minutes):
            sync_menu()
            return
        run_settings(
            settings_command(state["session"], autosave=minutes),
            desired_autosave=minutes)

    def popup_menu(icon, button, activate_time):
        update_tray()
        menu.popup(None, None, Gtk.StatusIcon.position_menu, icon, button, activate_time)

    tray.connect("activate", save_now)
    tray.connect("popup-menu", popup_menu)
    save_item.connect("activate", save_now)
    shutdown_item.connect("toggled", on_shutdown_toggled)
    for item in autosave_items.values():
        item.connect("toggled", on_autosave_toggled)
    open_item.connect("activate", open_manager)

    def update_tray():
        current = read_state()
        session_id = squashfs_session_id(current)
        state["session"] = session_id
        if time.monotonic() >= state["settings_override_until"]:
            state["policy"] = current.get("policy", "manual")
            try:
                state["autosave"] = int(current.get("autosave", "0") or 0)
            except (TypeError, ValueError):
                state["autosave"] = 0
        if time.monotonic() >= state["saved_override_until"]:
            state["saved"] = current.get("saved")
        tray.set_visible(bool(session_id))
        sync_menu()
        update_controls()
        update_tooltip()
        if session_id and not state["notice_shown"]:
            if squashfs_notice_seen():
                state["notice_shown"] = True
            else:
                if state.get("policy") == "shutdown":
                    body = _(
                        "Your session is stored as a compressed snapshot. Use the save "
                        "icon at any time. MiniOS will also save it automatically when shutting down.")
                else:
                    body = _(
                        "Your session is stored as a compressed snapshot. Use the save "
                        "icon at any time. Automatic saving can be configured from the tray menu.")
                if send_notification(_("SquashFS session is active"), body):
                    state["notice_shown"] = True
                    mark_squashfs_notice_seen()
        return True

    def poll():
        current = read_state()
        level = current.get("level", "ok")
        if level != state["last_level"] and level in ("critical", "emergency"):
            info = level_message(current)
            if info:
                show_dialog(info[0], info[1], info[2])
        state["last_level"] = level
        return True

    update_tray()
    poll()
    GLib.timeout_add_seconds(TRAY_POLL_SECONDS, update_tray)
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
