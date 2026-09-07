#!/usr/bin/env python3
"""Shared internal UI helpers for MiniOS session front ends."""

import gettext


def _(message):
    return gettext.dgettext('minios-session-manager', message)


def save_phase_text(phase):
    return {
        'prepare': _("Preparing session..."),
        'inventory': _("Scanning session changes..."),
        'capture': _("Collecting session changes..."),
        'compress': _("Compressing session..."),
        'verify': _("Verifying session..."),
        'publish': _("Finishing session save..."),
        'complete': _("Finishing session save..."),
    }.get(phase, _("Saving session..."))


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
