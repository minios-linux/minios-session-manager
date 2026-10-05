#!/usr/bin/env python3
"""Shared internal UI helpers for MiniOS session front ends."""

import gettext
import os
import shutil
import tempfile


def _(message):
    return gettext.dgettext('minios-session-manager', message)


def save_phase_text(phase):
    if isinstance(phase, dict):
        done, total = phase.get('copied_bytes'), phase.get('total_bytes')
        if type(done) is int and type(total) is int and 0 <= done <= total:
            from minios_gui import format_bytes
            return _("Copied {} of {}").format(format_bytes(done), format_bytes(total))
        return _("Saving session...")
    return {
        'prepare': _("Preparing session..."),
        'prepare-container': _("Session writes will pause during copying. Some applications may temporarily stop responding."),
        'inventory': _("Scanning session changes..."),
        'capture': _("Collecting session changes..."),
        'freeze': _("Pausing session writes. Applications may temporarily stop responding."),
        'copy': _("Copying the session to its original storage..."),
        'compress': _("Compressing session..."),
        'verify': _("Verifying session..."),
        'publish': _("Finishing session save..."),
        'complete': _("Finishing session save..."),
    }.get(phase, _("Saving session..."))


def save_progress(event):
    if isinstance(event, dict):
        done, total = event.get('copied_bytes'), event.get('total_bytes')
        if type(done) is int and type(total) is int and 0 <= done <= total and total > 0:
            return done / total
    return None


def store_state_text(state):
    return {
        'available': _("Original storage is connected"),
        'missing': _("Connect the original storage to save the session"),
        'readonly': _("Original storage is read-only"),
        'ambiguous': _("More than one device matches the original storage"),
    }.get(state, _("Original storage is unavailable"))


class SaveCancellation:
    """A caller-owned marker that remains writable while the session is frozen."""
    def __init__(self):
        parent = os.path.realpath(os.environ.get('XDG_RUNTIME_DIR') or '/tmp')
        self.directory = tempfile.mkdtemp(prefix='minios-session-save-', dir=parent)
        self.path = os.path.join(self.directory, 'cancel')
        self.requested = False

    def cancel(self):
        self.requested = True
        try:
            descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            os.close(descriptor)
        except FileExistsError:
            pass

    def close(self):
        shutil.rmtree(self.directory, ignore_errors=True)


def send_desktop_notification(summary, body, timeout_ms=5000):
    """Send a best-effort freedesktop notification for session activity."""
    try:
        from gi.repository import Gio, GLib

        connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        parameters = GLib.Variant(
            '(susssasa{sv}i)',
            (_('MiniOS Session Manager'), 0, 'document-save',
             summary, body, [], {}, timeout_ms))
        connection.call_sync(
            'org.freedesktop.Notifications', '/org/freedesktop/Notifications',
            'org.freedesktop.Notifications', 'Notify', parameters,
            GLib.VariantType.new('(u)'), Gio.DBusCallFlags.NONE, 2000, None)
        return True
    except Exception:
        return False
