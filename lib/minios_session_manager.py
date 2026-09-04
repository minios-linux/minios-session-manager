#!/usr/bin/env python3
"""
MiniOS Session Manager GUI

Graphical interface for managing MiniOS persistent sessions.
This GUI application calls the CLI utility (minios-session) to perform actual operations.
"""

import gi
import os
import sys
import json
import shutil
import subprocess
import tempfile
import threading
import time
import gettext
from datetime import datetime

gi.require_version('Gtk', '3.0')
gi.require_version('Gdk', '3.0')
from gi.repository import Gdk, Gio, Gtk, GLib, Pango
from minios_gui import (StatusBanner, apply_minios_css, ask_confirmation,
                        new_header_bar, new_icon, show_error_dialog, show_info_dialog)

# Internationalization setup
try:
    gettext.bindtextdomain('minios-session-manager', '/usr/share/locale')
    gettext.textdomain('minios-session-manager')
    _ = gettext.gettext
except Exception:
    _ = lambda x: x


def _style_dialog_affirmative(dialog, label):
    button = dialog.get_widget_for_response(Gtk.ResponseType.OK)
    if button is not None:
        button.set_label(label)
        button.get_style_context().add_class('suggested-action')
    dialog.set_default_response(Gtk.ResponseType.OK)


def _send_desktop_notification(summary, body):
    try:
        connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        parameters = GLib.Variant(
            '(susssasa{sv}i)',
            (_('MiniOS Session Manager'), 0, 'document-save', summary, body, [], {}, 4000))
        connection.call_sync(
            'org.freedesktop.Notifications', '/org/freedesktop/Notifications',
            'org.freedesktop.Notifications', 'Notify', parameters,
            GLib.VariantType.new('(u)'), Gio.DBusCallFlags.NONE, 2000, None)
        return True
    except Exception:
        return False


def _save_phase_text(phase):
    return {
        'prepare': _("Preparing session..."),
        'inventory': _("Scanning session changes..."),
        'capture': _("Collecting session changes..."),
        'compress': _("Compressing session..."),
        'verify': _("Verifying session..."),
        'publish': _("Finishing session save..."),
        'complete': _("Finishing session save..."),
    }.get(phase, _("Saving session..."))


def _strict_json_loads(text):
    def object_from_pairs(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError('duplicate JSON object key: {}'.format(key))
            value[key] = item
        return value

    def reject_constant(value):
        raise ValueError('invalid JSON constant: {}'.format(value))

    return json.loads(
        text, object_pairs_hook=object_from_pairs,
        parse_constant=reject_constant)


class SessionManagerGUI:
    """GUI application for session management"""

    def __init__(self):
        self.cli_command = self._get_minios_session_cli_path()
        self._cli_lock = threading.Lock()
        self._cli_process_lock = threading.Lock()
        self._cli_process = None
        self._closing = False
        self._refresh_generation = 0
        self._status_retry_generation = 0
        self._status_pending = True
        self._snapshot_time = None
        self._sessions_by_id = {}
        self._filesystem_info = {}
        self.luks_available = bool(
            shutil.which('cryptsetup') and shutil.which('losetup') and
            os.path.isfile('/run/initramfs/etc/minios-initramfs-crypt')
        )
        
        self.sessions_status = {
            'success': False, 'found': False, 'writable': False,
            '_query_error': False,
        }
        self.sessions_writable = False
        
        self._load_css()
        
        self.builder = Gtk.Builder()
        self.create_interface()
        self._retry_sessions_directory_status(None)
        GLib.timeout_add_seconds(30, self._periodic_session_refresh)

    def _load_css(self):
        """Load the MiniOS base CSS followed by the first app override found."""
        app_css = None
        for css_path in (
            "/usr/share/minios-session-manager/style.css",
            os.path.join(os.path.dirname(os.path.dirname(__file__)),
                         "share", "styles", "style.css"),
        ):
            if os.path.isfile(css_path):
                app_css = css_path
                break
        apply_minios_css(app_css)

    def _get_minios_session_cli_path(self):
        """Get the path to minios-session CLI tool"""
        script_dir = os.path.dirname(os.path.abspath(__file__))
        if os.path.basename(script_dir) == 'lib':
            # Running from source tree
            cli_path = os.path.join(os.path.dirname(script_dir), 'bin', 'minios-session')
        else:
            # Running from installed location - assume it's in PATH
            cli_path = 'minios-session'
        return cli_path

    def _run_cli_command(self, args, input_data=None):
        """Run CLI command and return result"""
        try:
            cmd = ['pkexec', self.cli_command] + args
            # Long exports, conversions, and imports are not bounded by an arbitrary UI timeout.
            # The lock prevents overlapping privileged operations from racing each other.
            with self._cli_lock:
                with self._cli_process_lock:
                    if self._closing:
                        return False, "", _("Operation cancelled")
                    process = subprocess.Popen(
                        cmd, stdin=subprocess.PIPE if input_data is not None else None,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        universal_newlines=True)
                    self._cli_process = process
                try:
                    output, error = process.communicate(input=input_data)
                    return process.returncode == 0, output, error
                finally:
                    with self._cli_process_lock:
                        if self._cli_process is process:
                            self._cli_process = None
        except Exception as e:
            return False, "", str(e)

    @staticmethod
    def _cli_error_text(error):
        """Decode structured CLI errors before presenting them in the GUI."""
        text = (error or '').strip()
        if not text:
            return 'Unknown error'
        try:
            payload = _strict_json_loads(text)
        except (TypeError, ValueError):
            return text
        if not isinstance(payload, dict):
            return text
        message = payload.get('error') or payload.get('message')
        details = payload.get('details')
        if message and details:
            return '{}\n\n{}'.format(message, details)
        return str(message or details or text)

    def _run_cli_streaming_save(self, session_id, phase_callback):
        """Run Save Now and surface validated phase events while it is active."""
        try:
            cmd = ['pkexec', self.cli_command, 'save', session_id, '--json', '--progress']
            with self._cli_lock:
                with tempfile.TemporaryFile() as error_file:
                    with self._cli_process_lock:
                        if self._closing:
                            return False, "", _("Operation cancelled")
                        process = subprocess.Popen(
                            cmd, stdout=subprocess.PIPE, stderr=error_file,
                            universal_newlines=True)
                        self._cli_process = process
                    final_output = ""
                    try:
                        for line in process.stdout:
                            stripped = line.strip()
                            if not stripped:
                                continue
                            try:
                                event = _strict_json_loads(stripped)
                            except (TypeError, ValueError):
                                continue
                            if event.get('type') == 'phase':
                                phase_callback(event.get('phase'))
                            else:
                                final_output = stripped
                        process.wait()
                        error_file.seek(0)
                        error = error_file.read().decode('utf-8', 'replace')
                        return process.returncode == 0, final_output, error
                    finally:
                        with self._cli_process_lock:
                            if self._cli_process is process:
                                self._cli_process = None
        except Exception as e:
            return False, "", str(e)

    def _prompt_luks_passphrase(self, confirm=False):
        """Return a passphrase pipe payload without placing it in arguments."""
        dialog = Gtk.Dialog(title=_("LUKS Passphrase"), parent=self.window)
        dialog.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                           Gtk.STOCK_OK, Gtk.ResponseType.OK)
        _style_dialog_affirmative(dialog, _('Continue'))
        content = dialog.get_content_area()
        first = Gtk.Entry()
        first.set_visibility(False)
        content.pack_start(Gtk.Label(label=_("Passphrase:")), False, False, 6)
        content.pack_start(first, False, False, 6)
        second = None
        if confirm:
            second = Gtk.Entry()
            second.set_visibility(False)
            content.pack_start(Gtk.Label(label=_("Confirm passphrase:")), False, False, 6)
            content.pack_start(second, False, False, 6)
        dialog.show_all()
        accepted = dialog.run() == Gtk.ResponseType.OK
        password = first.get_text()
        confirmation = second.get_text() if second else password
        dialog.destroy()
        if not accepted:
            return None
        if not password or password != confirmation:
            self._show_error(_("LUKS passphrases do not match or are empty."))
            return None
        payload = password + '\n'
        return payload + password + '\n' if confirm else payload

    def _get_session_mode(self, session_id):
        return self._sessions_by_id.get(session_id, {}).get('mode')

    def _fat_size_limit(self):
        return self._filesystem_info.get('limitations', {}).get('max_file_size')

    def _on_window_destroy(self, _window):
        """Terminate the active privileged child before the GUI exits."""
        with self._cli_process_lock:
            self._closing = True
            process = self._cli_process
        try:
            if process and process.poll() is None:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
        finally:
            Gtk.main_quit()

    def _check_sessions_directory_status(self):
        """Check sessions directory status using CLI"""
        try:
            success, output, error = self._run_cli_command(['status', '--json'])
            if success and output.strip():
                status = _strict_json_loads(output.strip())
                if not isinstance(status, dict):
                    raise ValueError('CLI response is not an object')
                for field in ('success', 'found', 'writable'):
                    if not isinstance(status.get(field), bool):
                        raise ValueError('CLI response field {} is not boolean'.format(field))
                sessions_dir = status.get('sessions_dir')
                if sessions_dir is not None and not isinstance(sessions_dir, str):
                    raise ValueError('CLI response sessions_dir is invalid')
                error_text = status.get('error')
                if not status['success']:
                    if (status['found'] or status['writable'] or
                            sessions_dir is not None or
                            not isinstance(error_text, str) or not error_text):
                        raise ValueError('CLI response has an invalid unavailable state')
                elif not status['found']:
                    if (status['writable'] or not sessions_dir or
                            not isinstance(error_text, str) or not error_text):
                        raise ValueError('CLI response has an invalid missing state')
                else:
                    filesystem_type = status.get('filesystem_type')
                    if (not sessions_dir or
                            not isinstance(filesystem_type, str) or
                            not filesystem_type or
                            (not status['writable'] and
                             (not isinstance(error_text, str) or not error_text))):
                        raise ValueError('CLI response has an invalid found state')
                    if status['writable'] and error_text is not None:
                        raise ValueError('CLI response has an unexpected error')
                status['_query_error'] = False
                return status
            else:
                return {
                    'success': False,
                    'found': False,
                    'writable': False,
                    '_query_error': True,
                    'error': self._cli_error_text(error)
                }
        except (TypeError, ValueError) as e:
            return {
                'success': False,
                'found': False,
                'writable': False,
                '_query_error': True,
                'error': f'Failed to parse CLI response: {e}'
            }
        except Exception as e:
            return {
                'success': False,
                'found': False,
                'writable': False,
                '_query_error': True,
                'error': str(e)
            }

    def _sessions_status_presentation(self):
        """Return the semantic intent and text for the current status."""
        if getattr(self, '_status_pending', False):
            intent = 'info'
            status_text = _("Checking session status...")
        elif self.sessions_status.get('_query_error', False):
            intent = 'error'
            status_text = _("Error: {}").format(
                self.sessions_status.get('error') or _('Unknown error'))
        elif self.sessions_status.get('found', False) and self.sessions_writable:
            intent = 'success'
            status_text = _("Sessions directory is writable")
        elif self.sessions_status.get('found', False):
            intent = 'error'
            status_text = _("Sessions directory is read-only")
        else:
            # A RAM-only boot without ``perch`` intentionally has no persistent
            # changes directory. Keep the manager usable without presenting this
            # expected state as an application failure.
            intent = 'warning'
            status_text = _("Persistent sessions are unavailable — the system is running without changes storage.")
        return intent, status_text

    def _build_sessions_status_info(self, main_box):
        """Build sessions directory status information panel."""
        intent, status_text = self._sessions_status_presentation()
        banner = StatusBanner(status_text, intent=intent)
        banner.label.set_markup(
            '<b>{}</b>'.format(GLib.markup_escape_text(status_text)))
        self.sessions_status_banner = banner
        self.sessions_status_retry = Gtk.Button(label=_("Retry"))
        self.sessions_status_retry.connect(
            'clicked', self._retry_sessions_directory_status)
        self.sessions_status_retry.set_no_show_all(True)
        self.sessions_status_retry.set_visible(
            not self._status_pending and
            self.sessions_status.get('_query_error', False))
        banner.pack_end(self.sessions_status_retry, False, False, 0)
        main_box.pack_start(banner, False, False, 0)

    def _retry_sessions_directory_status(self, _button):
        """Retry a failed status query without blocking the GTK main loop."""
        self._status_retry_generation += 1
        generation = self._status_retry_generation
        self._status_pending = True
        intent, status_text = self._sessions_status_presentation()
        self.sessions_status_banner.set_intent(intent)
        self.sessions_status_banner.label.set_markup(
            '<b>{}</b>'.format(GLib.markup_escape_text(status_text)))
        self.sessions_status_retry.set_visible(False)
        self.sessions_status_retry.set_sensitive(False)

        def check_status():
            status = self._check_sessions_directory_status()
            GLib.idle_add(
                self._finish_sessions_directory_status_retry,
                generation, status)

        thread = threading.Thread(target=check_status)
        thread.daemon = True
        thread.start()

    def _finish_sessions_directory_status_retry(self, generation, status):
        """Apply a status retry result on the GTK main thread."""
        if generation != self._status_retry_generation or self._closing:
            return False
        self._status_pending = False
        self.sessions_status = status
        self.sessions_writable = self.sessions_status.get('writable', False)
        intent, status_text = self._sessions_status_presentation()
        self.sessions_status_banner.set_intent(intent)
        self.sessions_status_banner.label.set_markup(
            '<b>{}</b>'.format(GLib.markup_escape_text(status_text)))
        query_error = self.sessions_status.get('_query_error', False)
        self.sessions_status_retry.set_visible(query_error)
        self.sessions_status_retry.set_sensitive(True)
        self.create_btn.set_sensitive(
            self.sessions_writable and bool(self._filesystem_info))
        for button in (self.import_btn, self.cleanup_btn):
            button.set_sensitive(self.sessions_writable)
        self.refresh_session_list()
        return False

    def _periodic_session_refresh(self):
        """Refresh external session changes without overlapping an operation."""
        if self._closing:
            return False
        with self._cli_process_lock:
            busy = self._cli_process is not None
        if (not busy and not self._status_pending and
                not self.sessions_status.get('_query_error', False) and
                self.sessions_status.get('found', False)):
            self.refresh_session_list()
        return True

    def _show_sessions_query_error_state(self, generation):
        """Render an initial status failure without opening a modal dialog."""
        if generation != self._refresh_generation:
            return False
        for row in self.sessions_list.get_children():
            self.sessions_list.remove(row)
        row = Gtk.ListBoxRow()
        row.set_sensitive(False)
        label = Gtk.Label(label=(
            self.sessions_status.get('error') or _('Unknown error')))
        label.set_line_wrap(True)
        label.set_margin_top(20)
        label.set_margin_bottom(20)
        row.add(label)
        self.sessions_list.add(row)
        self.sessions_list.show_all()
        self._show_loading(False)
        return False

    PERSISTENCE_STATE_FILE = "/run/minios-persistence/state"

    def _read_persistence_state(self):
        """Read the persistence guard state file (key=value) from tmpfs."""
        state = {}
        try:
            with open(self.PERSISTENCE_STATE_FILE, encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    line = line.strip()
                    if "=" in line:
                        key, value = line.split("=", 1)
                        state[key] = value
        except OSError:
            pass
        return state

    def _persistence_health_text(self, state):
        """Return (icon, style_class, text) for the persistence health banner."""
        level = state.get("level", "ok")
        degraded = state.get("degraded") == "1"
        pending = state.get("boot_warnings_pending", "0")
        if degraded or level == "emergency":
            return ("dialog-error", "error-banner",
                    _("Persistence is read-only (out of space)"))
        if level in ("advisory", "critical"):
            free = state.get("free_outer_bytes")
            try:
                free_mb = int(free) // (1024 * 1024) if free is not None else None
            except (TypeError, ValueError):
                free_mb = None
            if free_mb is not None:
                return ("dialog-warning", "warning-banner",
                        _("Low space on persistence device ({} MB free)").format(free_mb))
            return ("dialog-warning", "warning-banner",
                    _("Low space on persistence device"))
        try:
            if int(pending) > 0:
                return ("dialog-warning", "warning-banner",
                        _("{} startup warnings - click to review").format(int(pending)))
        except (TypeError, ValueError):
            pass
        return None

    def _build_persistence_health_banner(self, main_box):
        """Add a banner reflecting persistence health, refreshed every few minutes."""
        self._persistence_banner = StatusBanner('', intent='warning')
        # Keep these aliases for existing callers/tests that inspect the parts.
        self._persistence_banner_icon = self._persistence_banner.icon
        self._persistence_banner_label = self._persistence_banner.label
        # Control visibility ourselves so the window's show_all() cannot force
        # the banner visible when persistence is healthy.
        self._persistence_banner.set_no_show_all(True)
        main_box.pack_start(self._persistence_banner, False, False, 0)

        self._refresh_persistence_banner()
        # The guard samples quickly; the GUI only needs to poll every few minutes.
        GLib.timeout_add_seconds(180, self._refresh_persistence_banner)

    def _refresh_persistence_banner(self):
        state = self._read_persistence_state()
        info = self._persistence_health_text(state)
        if info is None:
            self._persistence_banner.hide()
            return True
        icon_name, style_class, text = info
        intent = {
            'warning-banner': 'warning',
            'error-banner': 'error',
            'success-banner': 'success',
            'info-banner': 'info',
        }.get(style_class, 'warning')
        self._persistence_banner.set_intent(intent, icon=icon_name)
        self._persistence_banner_label.set_markup(
            '<b>{}</b>'.format(GLib.markup_escape_text(text)))
        self._persistence_banner.show()
        return True

    def _build_header_bar(self):
        """Build the header bar"""
        self.window.set_titlebar(new_header_bar(_("MiniOS Session Manager")))

    def create_interface(self):
        """Create the main interface"""
        
        self.window = Gtk.Window()
        self.window.set_icon_name("media-floppy")  # match the .desktop Icon
        self.window.set_default_size(600, 500)
        self.window.set_position(Gtk.WindowPosition.CENTER)
        self.window.connect("destroy", self._on_window_destroy)
        
        # Build header bar
        self._build_header_bar()
        
        
        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        main_box.get_style_context().add_class("manager-surface")
        main_box.set_margin_start(10)
        main_box.set_margin_end(10)
        main_box.set_margin_top(10)
        main_box.set_margin_bottom(10)
        self.window.add(main_box)
        
        
        # Sessions directory status
        self._build_sessions_status_info(main_box)

        # Persistence health (low space / degraded / missed startup warnings)
        self._build_persistence_health_banner(main_box)
        
        # Sessions list
        self.sessions_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.sessions_list.get_style_context().add_class('minios-selectable')
        self.sessions_list.connect("row-selected", self._on_session_selected)
        self.sessions_list.connect("button-press-event", self._on_list_button_press)
        self.sessions_list.connect("popup-menu", self._on_list_popup_menu)

        # ScrolledWindow setup
        scrolled = Gtk.ScrolledWindow()
        scrolled.get_style_context().add_class("manager-list-card")
        scrolled.set_min_content_width(400)
        scrolled.set_min_content_height(200)
        scrolled.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scrolled.add(self.sessions_list)

        # Loading overlay components
        self.loading_spinner = Gtk.Spinner()
        self.loading_label = Gtk.Label(label=_("Loading sessions..."))
        self.loading_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.loading_box.pack_start(self.loading_spinner, False, False, 0)
        self.loading_box.pack_start(self.loading_label, False, False, 0)
        self.loading_box.set_halign(Gtk.Align.CENTER)
        self.loading_box.set_valign(Gtk.Align.CENTER)
        self.loading_box.get_style_context().add_class('loading-overlay')

        # Create overlay
        overlay = Gtk.Overlay()
        overlay.add(scrolled)
        overlay.add_overlay(self.loading_box)
        # The initial refresh happens before window.show_all(). Without
        # no-show-all, GTK makes this already-hidden overlay visible again and
        # RAM-only boots remain stuck on "Loading sessions..." forever.
        self.loading_box.set_no_show_all(True)
        self.loading_box.set_visible(False)
        
        main_box.pack_start(overlay, True, True, 0)
        
        # Create context menu
        self._create_context_menu()
        
        # Initialize selection
        self.selected_session_id = None
        
        # Toolbar buttons
        toolbar_box = Gtk.Grid(column_spacing=8)
        toolbar_box.set_column_homogeneous(True)
        toolbar_box.get_style_context().add_class("manager-footer")
        toolbar_box.set_halign(Gtk.Align.END)
        toolbar_box.set_margin_top(15)
        main_box.pack_start(toolbar_box, False, False, 0)
        
        # Create button
        create_btn = Gtk.Button(label=_("Create"))
        create_btn.set_image(
            new_icon("document-new-symbolic", Gtk.IconSize.BUTTON))
        create_btn.get_style_context().add_class('minios-text-button')
        create_btn.connect("clicked", self.on_create_clicked)
        create_btn.get_style_context().add_class('suggested-action')
        # Disable create button if sessions directory is not writable
        create_btn.set_sensitive(self.sessions_writable)
        toolbar_box.attach(create_btn, 2, 0, 1, 1)

        self.create_btn = create_btn  # Store reference for later use

        save_btn = Gtk.Button(label=_("Save Now"))
        save_btn.set_image(new_icon("document-save-symbolic", Gtk.IconSize.BUTTON))
        save_btn.get_style_context().add_class('minios-text-button')
        save_btn.connect("clicked", self.on_save_clicked)
        save_btn.set_sensitive(False)
        toolbar_box.attach(save_btn, 3, 0, 1, 1)
        self.save_btn = save_btn

        # Import button
        import_btn = Gtk.Button(label=_("Import"))
        import_btn.set_image(
            new_icon("document-open-symbolic", Gtk.IconSize.BUTTON))
        import_btn.get_style_context().add_class('minios-text-button')
        import_btn.connect("clicked", self.on_import_clicked)
        # Disable import button if sessions directory is not writable
        import_btn.set_sensitive(self.sessions_writable)
        toolbar_box.attach(import_btn, 1, 0, 1, 1)

        self.import_btn = import_btn  # Store reference for later use

        # Cleanup button
        cleanup_btn = Gtk.Button(label=_("Cleanup"))
        cleanup_btn.set_image(
            new_icon("user-trash-symbolic", Gtk.IconSize.BUTTON))
        cleanup_btn.get_style_context().add_class('minios-text-button')
        cleanup_btn.connect("clicked", self.on_cleanup_clicked)
        cleanup_btn.get_style_context().add_class('destructive-action')
        # Disable cleanup button if sessions directory is not writable
        cleanup_btn.set_sensitive(self.sessions_writable)
        toolbar_box.attach(cleanup_btn, 0, 0, 1, 1)
        
        self.cleanup_btn = cleanup_btn  # Store reference for later use

    def refresh_session_list(self):
        """Refresh the session list from CLI"""
        self._refresh_generation += 1
        generation = self._refresh_generation
        if self.sessions_status.get('_query_error', False):
            self._show_sessions_query_error_state(generation)
            return
        if not self.sessions_status.get('found', False):
            # No persistence directory is expected for RAM-only boots without
            # ``perch``. Render the normal empty state instead of invoking list,
            # active, and running commands that necessarily fail.
            self._process_session_data(
                generation, True, '[]', '', None, None)
            return

        def fetch_data():
            """Fetch data in background thread"""
            try:
                # Get session list and active/running sessions separately for accurate status
                list_success, list_output, list_error = self._run_cli_command(['list', '--json'])
                active_success, active_output, active_error = self._run_cli_command(['active', '--json'])
                running_success, running_output, running_error = self._run_cli_command(['running', '--json'])
                info_success, info_output, _info_error = self._run_cli_command(['info', '--json'])

                if not active_success or not running_success:
                    detail = active_error if not active_success else running_error
                    GLib.idle_add(
                        self._process_session_fetch_error, generation,
                        _("Error fetching session data: {}").format(
                            self._cli_error_text(detail)))
                    return

                # Parse active and running session IDs
                active_session_id = None
                running_session_id = None

                if active_success and active_output.strip():
                    active_data = _strict_json_loads(active_output.strip())
                    if active_data is not None and not isinstance(active_data, dict):
                        raise ValueError('active response is not an object or null')
                    if active_data is not None:
                        active_session_id = active_data.get('id')
                        if not isinstance(active_session_id, str) or not active_session_id:
                            raise ValueError('active response has an invalid id')
                else:
                    raise ValueError('active response is empty')

                if running_success and running_output.strip():
                    running_data = _strict_json_loads(running_output.strip())
                    if running_data is not None and not isinstance(running_data, dict):
                        raise ValueError('running response is not an object or null')
                    if running_data is not None:
                        running_session_id = running_data.get('id')
                        if not isinstance(running_session_id, str) or not running_session_id:
                            raise ValueError('running response has an invalid id')
                else:
                    raise ValueError('running response is empty')

                if not info_success:
                    raise ValueError(
                        self._cli_error_text(_info_error) or
                        'filesystem information is unavailable')
                if not info_output.strip():
                    raise ValueError('filesystem information response is empty')
                filesystem_info = _strict_json_loads(info_output.strip())
                if not self._valid_filesystem_info(filesystem_info):
                    raise ValueError('filesystem information response is invalid')

                # Return results to main thread
                GLib.idle_add(
                    self._process_session_data, generation, list_success,
                    list_output, list_error, active_session_id,
                    running_session_id, filesystem_info)
            except Exception as e:
                GLib.idle_add(
                    self._process_session_fetch_error, generation,
                    _("Error fetching session data: {}").format(str(e)))
        
        # Show loading indicator
        self._show_loading(True)
        
        # Run fetch in thread to avoid blocking UI
        thread = threading.Thread(target=fetch_data)
        thread.daemon = True
        thread.start()

    def _process_session_fetch_error(self, generation, message):
        """Show only errors from the current refresh generation."""
        if generation != self._refresh_generation:
            return False
        self._filesystem_info = {}
        if hasattr(self, 'create_btn'):
            self.create_btn.set_sensitive(False)
        self._show_error(message)
        self._show_loading(False)
        return False

    @staticmethod
    def _valid_filesystem_info(info):
        if not isinstance(info, dict):
            return False
        filesystem = info.get('filesystem')
        compatible_modes = info.get('compatible_modes')
        limitations = info.get('limitations')
        if (not isinstance(filesystem, dict) or
                not isinstance(filesystem.get('type'), str) or
                not filesystem['type'] or
                not isinstance(compatible_modes, list) or
                any(not isinstance(mode, str) or not mode
                    for mode in compatible_modes) or
                not isinstance(limitations, dict)):
            return False
        max_file_size = limitations.get('max_file_size')
        if (max_file_size is not None and
                (not isinstance(max_file_size, int) or
                 isinstance(max_file_size, bool) or max_file_size <= 0)):
            return False
        return True

    @staticmethod
    def _valid_session_record(session):
        if not isinstance(session, dict):
            return False
        for field in ('id', 'mode', 'version', 'edition', 'union',
                      'size_formatted', 'path'):
            if not isinstance(session.get(field), str) or not session[field]:
                return False
        size = session.get('size')
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            return False
        if session.get('modified') is not None and not isinstance(
                session.get('modified'), str):
            return False
        for field in ('is_default', 'is_running'):
            if not isinstance(session.get(field), bool):
                return False
        return True

    def _process_session_data(self, generation, list_success, list_output,
                              list_error, active_session_id,
                              running_session_id, filesystem_info=None):
        """Process session data in main thread"""
        if generation != self._refresh_generation:
            return
        try:
            # Check for list errors
            if not list_success:
                self._show_error(_("Failed to get session list: {}").format(
                    self._cli_error_text(list_error)))
                return

            stripped_output = list_output.strip()
            try:
                sessions = _strict_json_loads(stripped_output)
            except (TypeError, ValueError) as error:
                self._show_error(
                    _("Failed to parse session list JSON: {}").format(str(error)))
                return
            if (not isinstance(sessions, list) or
                    any(not self._valid_session_record(session)
                        for session in sessions)):
                self._show_error(_("Failed to parse session list JSON: {}").format(
                    'response does not match the session list schema'))
                return

            self._sessions_by_id = {
                session['id']: session for session in sessions
            }
            self._filesystem_info = filesystem_info or {}
            self._snapshot_time = time.monotonic()
            self.create_btn.set_sensitive(
                self.sessions_writable and bool(self._filesystem_info))

            # Keep the previous rows visible until a complete response is valid.
            for row in self.sessions_list.get_children():
                self.sessions_list.remove(row)

            # Parse JSON output
            # JSON format - parse sessions directly
            sessions_found = len(sessions) > 0

            for session in sessions:
                session_id = session.get('id', 'unknown')
                # Determine status from separate commands
                is_active = (session_id == active_session_id)
                is_running = (session_id == running_session_id)
                mode = session.get('mode', 'unknown')
                version = session.get('version', 'unknown')
                edition = session.get('edition', 'unknown')
                union = session.get('union', 'unknown')
                size = session.get('size_formatted', 'unknown')

                # For dynfilefs, add total size if available
                if mode == 'dynfilefs' and 'total_size_formatted' in session:
                    total_size = session.get('total_size_formatted', '')
                    if total_size:
                        size = f"{size} / {total_size}"

                modified_str = session.get('modified') or 'unknown'

                # Format modified date
                try:
                    if modified_str != 'unknown':
                        from datetime import datetime
                        # Python 3.6 compatible ISO format parsing
                        # Extract datetime part (first 19 chars: '2023-01-15T12:30:45')
                        dt_part = modified_str[:19]
                        modified_dt = datetime.strptime(dt_part, '%Y-%m-%dT%H:%M:%S')
                        modified = modified_dt.strftime('%Y-%m-%d %H:%M:%S')
                    else:
                        modified = 'unknown'
                except Exception:
                    modified = modified_str

                # Create session row
                self._create_session_row(
                    session_id, is_active, is_running, mode, version,
                    edition, union, size, modified)
            
            if not sessions_found:
                # Show "no sessions" message
                no_sessions_row = Gtk.ListBoxRow()
                no_sessions_row.set_sensitive(False)
                no_sessions_label = Gtk.Label(label=_("No sessions found"))
                no_sessions_label.set_margin_top(20)
                no_sessions_label.set_margin_bottom(20)
                no_sessions_row.add(no_sessions_label)
                self.sessions_list.add(no_sessions_row)
            
            self.sessions_list.show_all()
        finally:
            # Hide loading indicator
            self._show_loading(False)

    def _create_session_row(self, session_id, is_active, is_running, mode, version, edition, union, size, modified):
        """Create a session row"""
        row = Gtk.ListBoxRow()
        
        # Add CSS classes based on session status
        if is_active:
            row.get_style_context().add_class('row-status-active')
        elif is_running:
            row.get_style_context().add_class('row-status-running')
        else:
            row.get_style_context().add_class('row-status-available')
        
        main_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=15)
        main_box.get_style_context().add_class('manager-state-row-content')
        
        # Session icon
        icon_name = 'media-floppy'
        img = new_icon(icon_name, Gtk.IconSize.DND)
        main_box.pack_start(img, False, False, 0)
        
        # Session info box
        info_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        info_box.set_hexpand(True)
        
        # Main session name - clean without (CURRENT)
        session_label = Gtk.Label()
        session_text = _('Session')
        session_title = f"{session_text} #{session_id}"
        session_label.set_markup(f'<b><span size="large">{GLib.markup_escape_text(session_title)}</span></b>')
        session_label.set_halign(Gtk.Align.START)
        session_label.set_ellipsize(Pango.EllipsizeMode.END)
        info_box.pack_start(session_label, False, False, 0)
        
        # Create a grid for better information layout
        details_grid = Gtk.Grid()
        details_grid.set_column_spacing(20)
        details_grid.set_row_spacing(3)
        
        # Row 1: Mode and Version
        mode_label = Gtk.Label()
        mode_text = _("Mode:")
        mode_label.set_markup(f'<span size="small"><b>{mode_text}</b> {GLib.markup_escape_text(mode)}</span>')
        mode_label.set_halign(Gtk.Align.START)
        details_grid.attach(mode_label, 0, 0, 1, 1)
        
        version_label = Gtk.Label()
        version_text = _("Version:")
        version_label.set_markup(f'<span size="small"><b>{version_text}</b> {GLib.markup_escape_text(version)}</span>')
        version_label.set_halign(Gtk.Align.START)
        details_grid.attach(version_label, 1, 0, 1, 1)
        
        # Row 2: Edition and Union
        edition_label = Gtk.Label()
        edition_text = _("Edition:")
        edition_label.set_markup(f'<span size="small"><b>{edition_text}</b> {GLib.markup_escape_text(edition)}</span>')
        edition_label.set_halign(Gtk.Align.START)
        details_grid.attach(edition_label, 0, 1, 1, 1)
        
        union_label = Gtk.Label()
        union_text = _("Union FS:")
        union_label.set_markup(f'<span size="small"><b>{union_text}</b> {GLib.markup_escape_text(union)}</span>')
        union_label.set_halign(Gtk.Align.START)
        details_grid.attach(union_label, 1, 1, 1, 1)
        
        # Row 3: Size and Modified
        size_label = Gtk.Label()
        size_text = _("Size:")
        size_label.set_markup(f'<span size="small"><b>{size_text}</b> {GLib.markup_escape_text(size)}</span>')
        size_label.set_halign(Gtk.Align.START)
        details_grid.attach(size_label, 0, 2, 1, 1)
        
        modified_label = Gtk.Label()
        modified_text = _("Modified:")
        modified_label.set_markup(f'<span size="small"><b>{modified_text}</b> {GLib.markup_escape_text(modified)}</span>')
        modified_label.set_halign(Gtk.Align.START)
        details_grid.attach(modified_label, 1, 2, 1, 1)
        
        info_box.pack_start(details_grid, False, False, 0)
        main_box.pack_start(info_box, True, True, 0)
        
        # Status badges on the right - in horizontal line
        status_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
        status_box.set_valign(Gtk.Align.CENTER)
        status_box.set_halign(Gtk.Align.END)
        
        # Primary status badge
        status_label = Gtk.Label()
        status_label.get_style_context().add_class('badge')
        if is_active:
            status_text = _('ACTIVE')
            status_label.get_style_context().add_class('badge-success')
        else:
            status_text = _('AVAILABLE')
        
        status_label.set_markup(f'<span size="small" weight="bold">{GLib.markup_escape_text(status_text)}</span>')
        status_label.set_halign(Gtk.Align.CENTER)
        status_box.pack_start(status_label, False, False, 0)
        
        # Running badge (secondary) - in same line
        if is_running:
            running_label = Gtk.Label()
            running_text = _('RUNNING')
            running_label.get_style_context().add_class('badge')
            running_label.get_style_context().add_class('badge-warning')
            running_label.set_markup(f'<span size="small" weight="bold">{GLib.markup_escape_text(running_text)}</span>')
            running_label.set_halign(Gtk.Align.CENTER)
            status_box.pack_start(running_label, False, False, 0)
        
        main_box.pack_start(status_box, False, False, 0)
        
        row.add(main_box)
        row.session_id = session_id
        row.is_active = is_active
        row.is_running = is_running
        row.mode = mode
        
        self.sessions_list.add(row)

    def _on_session_selected(self, list_box, row):
        """Handle session selection"""
        if row:
            self.selected_session_id = row.session_id
        else:
            self.selected_session_id = None
        if hasattr(self, 'save_btn'):
            self.save_btn.set_sensitive(bool(
                row and self.sessions_writable and
                getattr(row, 'mode', 'unknown') == 'squashfs' and
                getattr(row, 'is_running', False)))

    def _create_context_menu(self):
        """Create context menu for session items"""
        self.context_menu = Gtk.Menu()
        self.context_menu.get_style_context().add_class('session-context-menu')

        # Activate menu item
        activate_item = Gtk.MenuItem.new_with_mnemonic(_("_Activate Session"))
        activate_item.get_style_context().add_class('context-menu-activate')
        activate_item.connect("activate", self._on_context_activate)
        self.context_menu.append(activate_item)

        save_settings_item = Gtk.MenuItem.new_with_mnemonic(_("Save _Settings..."))
        save_settings_item.connect("activate", self._on_context_save_settings)
        self.context_menu.append(save_settings_item)

        # Resize menu item
        resize_item = Gtk.MenuItem.new_with_mnemonic(_("_Resize Session"))
        resize_item.get_style_context().add_class('context-menu-resize')
        resize_item.connect("activate", self._on_context_resize)
        self.context_menu.append(resize_item)

        # Separator
        separator1 = Gtk.SeparatorMenuItem()
        self.context_menu.append(separator1)

        # Export menu item
        export_item = Gtk.MenuItem.new_with_mnemonic(_("_Export Session"))
        export_item.get_style_context().add_class('context-menu-export')
        export_item.connect("activate", self._on_context_export)
        self.context_menu.append(export_item)

        # Copy menu item
        copy_item = Gtk.MenuItem.new_with_mnemonic(_("_Copy Session"))
        copy_item.get_style_context().add_class('context-menu-copy')
        copy_item.connect("activate", self._on_context_copy)
        self.context_menu.append(copy_item)

        # Convert menu item
        convert_item = Gtk.MenuItem.new_with_mnemonic(_("Con_vert Session"))
        convert_item.get_style_context().add_class('context-menu-convert')
        convert_item.connect("activate", self._on_context_convert)
        self.context_menu.append(convert_item)

        # Separator
        separator2 = Gtk.SeparatorMenuItem()
        self.context_menu.append(separator2)

        # Open folder menu item
        open_folder_item = Gtk.MenuItem.new_with_mnemonic(_("_Open Folder"))
        open_folder_item.get_style_context().add_class('context-menu-open-folder')
        open_folder_item.connect("activate", self._on_context_open_folder)
        self.context_menu.append(open_folder_item)

        # Separator
        separator3 = Gtk.SeparatorMenuItem()
        self.context_menu.append(separator3)

        # Delete menu item
        delete_item = Gtk.MenuItem.new_with_mnemonic(_("_Delete Session"))
        delete_item.get_style_context().add_class('context-menu-delete')
        delete_item.connect("activate", self._on_context_delete)
        self.context_menu.append(delete_item)

        self.context_menu.show_all()

    def _on_list_button_press(self, widget, event):
        """Handle button press on list"""
        if event.button == 3:  # Right click
            # Get the row under cursor
            row = self.sessions_list.get_row_at_y(int(event.y))
            if row:
                # Select the row
                self.sessions_list.select_row(row)
                self.selected_session_id = row.session_id
                self._prepare_context_menu(row)
                self.context_menu.popup_at_pointer(event)
                return True
        return False

    def _on_list_popup_menu(self, _widget):
        row = self.sessions_list.get_selected_row()
        if row is None:
            return False
        self._prepare_context_menu(row)
        self.context_menu.popup_at_widget(
            self.sessions_list, Gdk.Gravity.SOUTH_WEST,
            Gdk.Gravity.NORTH_WEST, None)
        return True

    def _prepare_context_menu(self, row):
        children = self.context_menu.get_children()
        activate_item, save_settings_item, resize_item = children[0:3]
        export_item, copy_item, convert_item = children[4:7]
        delete_item = children[10]
        resize_available = getattr(row, 'mode', 'unknown') in (
            'dynfilefs', 'raw', 'luks')
        supported_operations = getattr(row, 'mode', 'unknown') != 'squashfs'
        active = getattr(row, 'is_active', False)
        running = getattr(row, 'is_running', False)

        activate_item.set_sensitive(
            self.sessions_writable and not active)
        save_settings_item.set_sensitive(
            self.sessions_writable and getattr(row, 'mode', 'unknown') == 'squashfs')
        resize_item.set_sensitive(
            self.sessions_writable and not running and resize_available)
        export_item.set_sensitive(not running and supported_operations)
        copy_item.set_sensitive(
            self.sessions_writable and not running and supported_operations)
        convert_item.set_sensitive(
            self.sessions_writable and not running and supported_operations)
        delete_item.set_sensitive(
            self.sessions_writable and not active and
            (not running or getattr(row, 'mode', 'unknown') == 'squashfs'))

    def _on_context_activate(self, menu_item):
        """Handle activate from context menu"""
        if self.selected_session_id:
            self.on_activate_clicked(None)

    def _on_context_delete(self, menu_item):
        """Handle delete from context menu"""
        if self.selected_session_id:
            self.on_delete_clicked(None)

    def _on_context_save_settings(self, menu_item):
        """Configure automatic saving for a SquashFS session."""
        if self.selected_session_id:
            self._show_squashfs_settings_dialog(self.selected_session_id)

    def _on_context_resize(self, menu_item):
        """Handle resize from context menu"""
        if self.selected_session_id:
            self._show_resize_dialog(self.selected_session_id)

    def _on_context_export(self, menu_item):
        """Handle export from context menu"""
        if self.selected_session_id:
            self._show_export_dialog(self.selected_session_id)

    def _on_context_copy(self, menu_item):
        """Handle copy from context menu"""
        if self.selected_session_id:
            self._show_copy_dialog(self.selected_session_id)

    def _on_context_convert(self, menu_item):
        """Handle convert from context menu"""
        if self.selected_session_id:
            self._show_convert_dialog(self.selected_session_id)

    def _on_context_open_folder(self, menu_item):
        """Handle open folder from context menu"""
        if self.selected_session_id:
            import subprocess
            session = self._sessions_by_id.get(self.selected_session_id, {})
            session_path = session.get('path')
            try:
                if session_path and os.path.exists(session_path):
                    subprocess.Popen(['xdg-open', session_path])
                else:
                    self._show_error(_("Session folder not found"))
            except Exception as e:
                self._show_error(_("Failed to open folder: {}").format(str(e)))


    def on_create_clicked(self, button):
        """Handle create session button click"""
        # Check if sessions directory is writable
        if not self.sessions_writable:
            self._show_error(_("Sessions directory is not writable. Cannot create new sessions."))
            return
        
        # Filesystem information is refreshed together with the session list.
        fs_info = self._filesystem_info
        compatible_modes = ['native', 'dynfilefs', 'raw']  # Default
        filesystem_type = "unknown"
        limitations = {}

        if fs_info:
            filesystem_type = fs_info.get('filesystem', {}).get('type', 'unknown')
            compatible_modes = fs_info.get(
                'compatible_modes', ['native', 'dynfilefs', 'raw'])
            limitations = fs_info.get('limitations', {})
        
        # Create session mode selection dialog
        dialog = Gtk.Dialog(
            title=_("Create New Session"),
            parent=self.window
        )
        dialog.add_buttons(
            Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
            Gtk.STOCK_OK, Gtk.ResponseType.OK
        )
        _style_dialog_affirmative(dialog, _('Create'))
        
        content_area = dialog.get_content_area()
        content_area.set_spacing(10)
        content_area.set_margin_start(10)
        content_area.set_margin_end(10)
        content_area.set_margin_top(10)
        content_area.set_margin_bottom(10)
        
        # Show filesystem info
        fs_info_label = Gtk.Label()
        detected_text = _('Detected filesystem:')
        fs_info_label.set_markup(f"<b>{detected_text} {filesystem_type}</b>")
        content_area.pack_start(fs_info_label, False, False, 0)
        
        label = Gtk.Label(label=_("Select session mode:"))
        content_area.pack_start(label, False, False, 0)
        
        # Radio buttons for mode selection (only for compatible modes)
        radio_buttons = {}
        first_radio = None
        
        if 'native' in compatible_modes:
            native_radio = Gtk.RadioButton.new_with_label_from_widget(None, _("Native Mode"))
            native_radio.set_tooltip_text(_("Direct storage on POSIX filesystems"))
            content_area.pack_start(native_radio, False, False, 0)
            radio_buttons['native'] = native_radio
            if first_radio is None:
                first_radio = native_radio
        else:
            # Show disabled native mode with explanation
            native_radio = Gtk.RadioButton.new_with_label_from_widget(None, _("Native Mode (not compatible)"))
            native_radio.set_tooltip_text(_("Not available: requires POSIX filesystem"))
            native_radio.set_sensitive(False)
            content_area.pack_start(native_radio, False, False, 0)
        
        if 'squashfs' in compatible_modes:
            base_radio = first_radio if first_radio else None
            squashfs_radio = Gtk.RadioButton.new_with_label_from_widget(base_radio, _("SquashFS Mode"))
            squashfs_radio.set_tooltip_text(_("Compressed snapshot of current live changes"))
            content_area.pack_start(squashfs_radio, False, False, 0)
            radio_buttons['squashfs'] = squashfs_radio
            if first_radio is None:
                first_radio = squashfs_radio

        if 'dynfilefs' in compatible_modes:
            base_radio = first_radio if first_radio else None
            dynfilefs_radio = Gtk.RadioButton.new_with_label_from_widget(base_radio, _("DynFileFS Mode"))
            dynfilefs_radio.set_tooltip_text(_("Dynamic files"))
            content_area.pack_start(dynfilefs_radio, False, False, 0)
            radio_buttons['dynfilefs'] = dynfilefs_radio
            if first_radio is None:
                first_radio = dynfilefs_radio
        
        if 'raw' in compatible_modes:
            base_radio = first_radio if first_radio else None
            raw_radio = Gtk.RadioButton.new_with_label_from_widget(base_radio, _("Raw Mode"))
            raw_radio.set_tooltip_text(_("Static image files"))
            content_area.pack_start(raw_radio, False, False, 0)
            radio_buttons['raw'] = raw_radio
            if first_radio is None:
                first_radio = raw_radio

        if 'luks' in compatible_modes:
            base_radio = first_radio if first_radio else None
            luks_radio = Gtk.RadioButton.new_with_label_from_widget(base_radio, _("LUKS Mode"))
            luks_radio.set_tooltip_text(_("Encrypted LUKS2/ext4 container"))
            content_area.pack_start(luks_radio, False, False, 0)
            radio_buttons['luks'] = luks_radio
            if first_radio is None:
                first_radio = luks_radio

        # SquashFS save policy and optional periodic saving.
        squashfs_options = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        content_area.pack_start(squashfs_options, False, False, 0)
        shutdown_check = Gtk.CheckButton(
            label=_("Save automatically at shutdown (recommended)"))
        shutdown_check.set_active(True)
        squashfs_options.pack_start(shutdown_check, False, False, 0)

        policy_description = Gtk.Label()
        policy_description.set_halign(Gtk.Align.START)
        policy_description.set_line_wrap(True)
        squashfs_options.pack_start(policy_description, False, False, 0)

        autosave_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        squashfs_options.pack_start(autosave_box, False, False, 0)
        autosave_box.pack_start(Gtk.Label(label=_("Periodic save:")), False, False, 0)
        autosave_combo = Gtk.ComboBoxText()
        for value, label_text in (
                ('0', _("Off")), ('30', _("Every 30 minutes")),
                ('60', _("Every 1 hour")), ('120', _("Every 2 hours")),
                ('240', _("Every 4 hours")), ('480', _("Every 8 hours"))):
            autosave_combo.append(value, label_text)
        autosave_combo.set_active_id('0')
        autosave_box.pack_start(autosave_combo, False, False, 0)

        autosave_warning = Gtk.Label(label=_(
            "Periodic saving increases CPU usage and writes to storage. "
            "An interval of 1 hour or longer is recommended."))
        autosave_warning.set_halign(Gtk.Align.START)
        autosave_warning.set_line_wrap(True)
        autosave_warning.get_style_context().add_class('warning-label')
        autosave_warning.set_no_show_all(True)
        squashfs_options.pack_start(autosave_warning, False, False, 0)

        # Size selection for container modes.
        size_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        content_area.pack_start(size_box, False, False, 0)
        
        size_label = Gtk.Label(label=_("Size (MB):"))
        size_box.pack_start(size_label, False, False, 0)
        
        adjustment = Gtk.Adjustment(value=4000, lower=100, upper=1000000, step_increment=100)
        size_spinbutton = Gtk.SpinButton()
        size_spinbutton.set_adjustment(adjustment)
        size_spinbutton.set_value(4000)
        size_box.pack_start(size_spinbutton, False, False, 0)
        
        size_info_label = Gtk.Label(label=_("(Only used for container modes)"))
        size_info_label.set_sensitive(False)
        size_box.pack_start(size_info_label, False, False, 0)
        
        # Enable/disable size controls based on mode selection
        def on_mode_changed(radio):
            # Check which radio buttons exist and are active
            is_dynfilefs_active = 'dynfilefs' in radio_buttons and radio_buttons['dynfilefs'].get_active()
            is_raw_active = 'raw' in radio_buttons and radio_buttons['raw'].get_active()
            is_luks_active = 'luks' in radio_buttons and radio_buttons['luks'].get_active()
            is_squashfs_active = 'squashfs' in radio_buttons and radio_buttons['squashfs'].get_active()
            is_sized_mode = is_dynfilefs_active or is_raw_active or is_luks_active

            squashfs_options.set_sensitive(is_squashfs_active)
            policy_description.set_text(
                _("You can also save manually at any time using Save Now.")
                if shutdown_check.get_active() else
                _("Changes since the last save are discarded when the computer is turned off."))
            if is_squashfs_active and (autosave_combo.get_active_id() or '0') != '0':
                autosave_warning.show()
            else:
                autosave_warning.hide()
            size_label.set_sensitive(is_sized_mode)
            size_spinbutton.set_sensitive(is_sized_mode)
            size_info_label.set_sensitive(not is_sized_mode)
            
            # Raw and LUKS are single files and therefore share FAT32's cap.
            if (is_raw_active or is_luks_active) and 'max_file_size' in limitations:
                max_size = limitations['max_file_size']
                current_size = int(size_spinbutton.get_value())
                if current_size > max_size:
                    size_spinbutton.set_value(max_size)
                adjustment.set_upper(4000)
                size_info_label.set_text(_("(Maximum {}MB on FAT32)").format(max_size))
                size_info_label.set_sensitive(True)
            else:
                adjustment.set_upper(1000000)
                size_info_label.set_text(_("(Only used for container modes)"))
        
        # Connect signals only for existing radio buttons
        for mode, radio in radio_buttons.items():
            radio.connect("toggled", on_mode_changed)
        shutdown_check.connect("toggled", on_mode_changed)
        autosave_combo.connect("changed", on_mode_changed)
        
        # Initialize sensitivity
        on_mode_changed(None)
        
        dialog.show_all()
        
        response = dialog.run()
        
        if response == Gtk.ResponseType.OK:
            # Determine selected mode from radio buttons
            mode = "native"  # default
            for mode_name, radio in radio_buttons.items():
                if radio.get_active():
                    mode = mode_name
                    break
            
            # Get size/save settings if needed.
            size_mb = int(size_spinbutton.get_value())
            squashfs_policy = 'shutdown' if shutdown_check.get_active() else 'manual'
            squashfs_autosave = int(autosave_combo.get_active_id() or '0')

            dialog.destroy()
            
            # Create session in background thread
            def create_session_bg():
                try:
                    if mode in ["dynfilefs", "raw", "luks"]:
                        command = ['create', mode, str(size_mb), '--json']
                        if mode == 'luks':
                            command.append('--password-stdin')
                        success, output, error = self._run_cli_command(command, password_input)
                    elif mode == 'squashfs':
                        success, output, error = self._run_cli_command([
                            'create', mode, '--policy', squashfs_policy,
                            '--autosave', str(squashfs_autosave), '--json'])
                    else:
                        success, output, error = self._run_cli_command(['create', mode, '--json'])
                    
                    # Update UI in main thread
                    GLib.idle_add(self._on_session_creation_complete, success, output, error, None)
                except Exception as e:
                    GLib.idle_add(self._on_session_creation_complete, False, "", str(e), None)
            
            password_input = self._prompt_luks_passphrase(confirm=True) if mode == 'luks' else None
            if mode == 'luks' and password_input is None:
                return
            self._show_loading(True, _("Creating new session, please wait..."))
            thread = threading.Thread(target=create_session_bg)
            thread.daemon = True
            thread.start()
        else:
            dialog.destroy()

    def on_save_clicked(self, button):
        """Save the selected running SquashFS session."""
        row = self.sessions_list.get_selected_row()
        if (not row or not self.sessions_writable or
                getattr(row, 'mode', 'unknown') != 'squashfs' or
                not getattr(row, 'is_running', False)):
            self._show_error(_("Save Now is available only for the running SquashFS session."))
            return
        session_id = row.session_id
        state = {'done': False, 'dialog': None, 'shown': False, 'phase': 'prepare'}

        def update_phase(phase):
            state['phase'] = phase
            if state['dialog'] is not None:
                state['dialog'].message_label.set_text(_save_phase_text(phase))
            return False

        def show_progress_if_needed():
            if state['done']:
                return False
            dialog = self._create_progress_dialog(
                _("Saving Session"), _save_phase_text(state['phase']))
            state['dialog'] = dialog
            state['shown'] = True
            dialog.show_all()
            return False

        def finish_save(success, output, error):
            state['done'] = True
            if state['dialog'] is not None:
                state['dialog'].destroy()
                state['dialog'] = None
            detail = error.strip()
            try:
                result = _strict_json_loads(output) if output else {}
                detail = result.get('message') or detail
            except (TypeError, ValueError):
                pass
            if success:
                self.refresh_session_list()
                if not state['shown']:
                    _send_desktop_notification(
                        _("Session saved"),
                        _("SquashFS session #{} was saved successfully.").format(session_id))
            else:
                message = _("Failed to save session")
                if detail:
                    message = "{}: {}".format(message, detail)
                self._show_error(message)
            return False

        def save_session_bg():
            success, output, error = self._run_cli_streaming_save(
                session_id,
                lambda phase: GLib.idle_add(update_phase, phase))
            GLib.idle_add(finish_save, success, output, error)

        GLib.timeout_add(500, show_progress_if_needed)
        thread = threading.Thread(target=save_session_bg)
        thread.daemon = True
        thread.start()

    def on_activate_clicked(self, button):
        """Handle activate session action"""
        # Check if sessions directory is writable
        if not self.sessions_writable:
            self._show_error(_("Sessions directory is not writable. Cannot activate sessions."))
            return
        
        session_id = self.selected_session_id
        
        # Show loading overlay
        self._show_loading(True, _("Activating session, please wait..."))
        
        # Activate session in background thread
        def activate_session_bg():
            try:
                success, output, error = self._run_cli_command(['activate', session_id, '--json'])
                GLib.idle_add(self._on_session_operation_complete, success, output, error, None, _("Session activated successfully"), _("Failed to activate session"))
            except Exception as e:
                GLib.idle_add(self._on_session_operation_complete, False, "", str(e), None, "", _("Failed to activate session"))
        
        thread = threading.Thread(target=activate_session_bg)
        thread.daemon = True
        thread.start()

    def on_delete_clicked(self, button):
        """Handle delete session action"""
        if not self.sessions_writable:
            self._show_error(_("Sessions directory is not writable. Cannot delete sessions."))
            return

        row = self.sessions_list.get_selected_row()
        if row is None:
            return
        session_id = row.session_id
        handoff = (getattr(row, 'mode', 'unknown') == 'squashfs' and
                   getattr(row, 'is_running', False))
        target = None
        if handoff:
            target = next((candidate for candidate in self.sessions_list.get_children()
                           if getattr(candidate, 'is_active', False) and
                           candidate.session_id != session_id), None)
            if target is None or getattr(target, 'mode', 'unknown') != 'squashfs':
                self._show_error(_(
                    "Create and activate another SquashFS session before deleting "
                    "the running SquashFS session."))
                return
            title = _("Delete running SquashFS session?")
            message = _(
                "The current system is already running from RAM. Session #{} will "
                "become the save target for this boot. Future manual and automatic "
                "saves will be written to it. Session #{} will then be permanently "
                "deleted.").format(target.session_id, session_id)
            confirm_label = _("Delete and Continue")
        else:
            title = _("Delete this session?")
            message = _("Session #{} and all of its data will be permanently "
                        "deleted. This action cannot be undone.").format(session_id)
            confirm_label = _("Delete Session")

        if not ask_confirmation(
                self.window, title, message, destructive=True,
                confirm_label=confirm_label, cancel_label=_("Keep Session")):
            return

        self._show_loading(
            True, _("Switching save target and deleting session...") if handoff else
            _("Deleting session, please wait..."))

        def delete_session_bg():
            try:
                command = ['delete', session_id]
                if handoff:
                    command.append('--handoff')
                command.append('--json')
                success, output, error = self._run_cli_command(command)
                GLib.idle_add(
                    self._on_session_operation_complete, success, output, error, None,
                    _("Session deleted successfully"), _("Failed to delete session"))
            except Exception as e:
                GLib.idle_add(
                    self._on_session_operation_complete, False, "", str(e), None,
                    "", _("Failed to delete session"))

        thread = threading.Thread(target=delete_session_bg)
        thread.daemon = True
        thread.start()

    def on_cleanup_clicked(self, button):
        """Handle cleanup button click"""
        # Check if sessions directory is writable
        if not self.sessions_writable:
            self._show_error(_("Sessions directory is not writable. Cannot cleanup sessions."))
            return
        
        # Ask for days threshold
        dialog = Gtk.Dialog(
            title=_("Cleanup Old Sessions"),
            parent=self.window
        )
        dialog.add_buttons(
            Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
            Gtk.STOCK_OK, Gtk.ResponseType.OK
        )
        _style_dialog_affirmative(dialog, _('Continue'))
        
        content_area = dialog.get_content_area()
        content_area.set_spacing(10)
        content_area.set_margin_start(10)
        content_area.set_margin_end(10)
        content_area.set_margin_top(10)
        content_area.set_margin_bottom(10)
        
        label = Gtk.Label(label=_("Delete sessions older than how many days?"))
        content_area.pack_start(label, False, False, 0)
        
        adjustment = Gtk.Adjustment(value=30, lower=1, upper=365, step_increment=1)
        spinbutton = Gtk.SpinButton()
        spinbutton.set_adjustment(adjustment)
        spinbutton.set_value(30)
        content_area.pack_start(spinbutton, False, False, 0)
        
        dialog.show_all()
        
        response = dialog.run()
        
        if response == Gtk.ResponseType.OK:
            days = int(spinbutton.get_value())
            dialog.destroy()
            
            # Confirm cleanup
            if ask_confirmation(
                    self.window,
                    _("Delete old sessions?"),
                    _("All sessions older than {} days will be permanently "
                      "deleted.").format(days),
                    destructive=True,
                    confirm_label=_("Delete Old Sessions")):
                # Show loading overlay
                self._show_loading(True, _("Cleaning up old sessions, please wait..."))
                
                # Run cleanup in background thread
                def cleanup_sessions_bg():
                    try:
                        success, output, error = self._run_cli_command(['cleanup', '--days', str(days), '--json'])
                        GLib.idle_add(self._on_session_operation_complete, success, output, error, None, _("Cleanup completed successfully"), _("Cleanup failed"))
                    except Exception as e:
                        GLib.idle_add(self._on_session_operation_complete, False, "", str(e), None, "", _("Cleanup failed"))
                
                thread = threading.Thread(target=cleanup_sessions_bg)
                thread.daemon = True
                thread.start()
        else:
            dialog.destroy()


    def _show_error(self, message):
        """Show a standard error attached to the main window."""
        show_error_dialog(
            self.window, _("Something went wrong"), str(message))

    def _show_info(self, message):
        """Show a standard informational message attached to the main window."""
        show_info_dialog(self.window, _("Completed"), str(message))


    def _create_progress_dialog(self, title, message):
        """Create a progress dialog with spinner"""
        progress_dialog = Gtk.Dialog(
            title=title,
            parent=self.window,
            modal=True,
            destroy_with_parent=True
        )
        progress_dialog.set_deletable(False)
        progress_dialog.set_resizable(False)
        progress_dialog.set_default_size(400, 150)
        
        content_area = progress_dialog.get_content_area()
        content_area.set_spacing(15)
        content_area.set_margin_start(20)
        content_area.set_margin_end(20)
        content_area.set_margin_top(20)
        content_area.set_margin_bottom(20)
        
        # Progress box with spinner and text
        progress_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=15)
        progress_box.set_halign(Gtk.Align.CENTER)
        
        # Spinner
        spinner = Gtk.Spinner()
        spinner.set_size_request(32, 32)
        spinner.start()
        progress_box.pack_start(spinner, False, False, 0)
        
        # Message label
        message_label = Gtk.Label(label=message)
        message_label.set_halign(Gtk.Align.START)
        progress_box.pack_start(message_label, False, False, 0)
        
        content_area.pack_start(progress_box, True, True, 0)
        progress_dialog.message_label = message_label
        
        return progress_dialog

    def _on_session_creation_complete(self, success, output, error, progress_dialog):
        """Handle session creation completion"""
        # Hide loading overlay if no progress_dialog (using overlay)
        if progress_dialog is None:
            self._show_loading(False)
        else:
            progress_dialog.destroy()
        
        if success:
            self.refresh_session_list()
        else:
            self._show_error(_("Failed to create session: {}").format(error))

    def _on_session_operation_complete(self, success, output, error, progress_dialog, success_prefix, error_prefix):
        """Handle generic session operation completion"""
        # Hide loading overlay if no progress_dialog (using overlay)
        if progress_dialog is None:
            self._show_loading(False)
        else:
            progress_dialog.destroy()
        
        if success:
            # Skip showing success info dialog, just refresh the list
            self.refresh_session_list()
        else:
            detail = error.strip() if error else ''
            if output:
                try:
                    result = _strict_json_loads(output)
                    detail = result.get('message') or result.get('error') or detail
                except (TypeError, ValueError):
                    pass
            error_message = "{}: {}".format(error_prefix, detail) if detail else error_prefix
            self._show_error(error_message)

    def _show_loading(self, show, text=None):
        """Show or hide loading indicator"""
        if show:
            if text:
                self.loading_label.set_text(text)
            # Ensure CSS class is applied every time we show the loading overlay
            self.loading_box.get_style_context().add_class('loading-overlay')
            self.loading_box.set_visible(True)
            self.loading_spinner.start()
        else:
            self.loading_box.set_visible(False)
            self.loading_spinner.stop()
            # Reset to default text
            self.loading_label.set_text(_("Loading sessions..."))

    def _show_squashfs_settings_dialog(self, session_id):
        """Show user-facing automatic-save settings for a SquashFS session."""
        session = self._sessions_by_id.get(session_id)
        if session is None:
            self._show_error(_("Session not found"))
            return
        if session.get('mode') != 'squashfs':
            self._show_error(_("Save settings are available only for SquashFS sessions."))
            return

        dialog = Gtk.Dialog(title=_("SquashFS Save Settings"), parent=self.window)
        dialog.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                           Gtk.STOCK_OK, Gtk.ResponseType.OK)
        _style_dialog_affirmative(dialog, _("Apply"))
        content = dialog.get_content_area()
        content.set_spacing(8)
        content.set_margin_start(12)
        content.set_margin_end(12)
        content.set_margin_top(12)
        content.set_margin_bottom(12)

        shutdown_check = Gtk.CheckButton(
            label=_("Save automatically at shutdown (recommended)"))
        shutdown_check.set_active(session.get('policy', 'manual') == 'shutdown')
        content.pack_start(shutdown_check, False, False, 0)
        description = Gtk.Label()
        description.set_halign(Gtk.Align.START)
        description.set_line_wrap(True)
        content.pack_start(description, False, False, 0)

        autosave_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        autosave_box.pack_start(Gtk.Label(label=_("Periodic save:")), False, False, 0)
        autosave_combo = Gtk.ComboBoxText()
        for value, label_text in (
                ('0', _("Off")), ('30', _("Every 30 minutes")),
                ('60', _("Every 1 hour")), ('120', _("Every 2 hours")),
                ('240', _("Every 4 hours")), ('480', _("Every 8 hours"))):
            autosave_combo.append(value, label_text)
        autosave_combo.set_active_id(str(session.get('autosave') or 0))
        autosave_box.pack_start(autosave_combo, False, False, 0)
        content.pack_start(autosave_box, False, False, 0)

        warning = Gtk.Label(label=_(
            "Periodic saving increases CPU usage and writes to storage. "
            "An interval of 1 hour or longer is recommended."))
        warning.set_halign(Gtk.Align.START)
        warning.set_line_wrap(True)
        warning.get_style_context().add_class('warning-label')
        warning.set_no_show_all(True)
        content.pack_start(warning, False, False, 0)

        def update_settings_text(_widget=None):
            description.set_text(
                _("You can also save manually at any time using Save Now.")
                if shutdown_check.get_active() else
                _("Changes since the last save are discarded when the computer is turned off."))
            if (autosave_combo.get_active_id() or '0') != '0':
                warning.show()
            else:
                warning.hide()

        shutdown_check.connect('toggled', update_settings_text)
        autosave_combo.connect('changed', update_settings_text)
        dialog.show_all()
        update_settings_text()
        response = dialog.run()
        if response != Gtk.ResponseType.OK:
            dialog.destroy()
            return
        shutdown = 'on' if shutdown_check.get_active() else 'off'
        autosave = autosave_combo.get_active_id() or '0'
        dialog.destroy()

        self._show_loading(True, _("Updating save settings..."))

        def update_bg():
            ok, out, err = self._run_cli_command([
                'settings', session_id, '--shutdown', shutdown,
                '--autosave', autosave, '--json'])
            GLib.idle_add(
                self._on_session_operation_complete, ok, out, err, None,
                _("Save settings updated"), _("Failed to update save settings"))

        thread = threading.Thread(target=update_bg)
        thread.daemon = True
        thread.start()

    def _show_resize_dialog(self, session_id):
        """Show resize dialog for a session"""
        session_info = self._sessions_by_id.get(session_id)
        if session_info is None:
            self._show_error(_("Session not found"))
            return

        try:
            session_mode = session_info.get('mode', 'unknown')
            if session_mode not in ['dynfilefs', 'raw', 'luks']:
                self._show_error(_("Resize is only supported for dynfilefs, raw, and LUKS mode sessions"))
                return
            
            # Check if session is running
            is_running = session_info.get('is_running', False)
            if is_running:
                self._show_error(_("Cannot resize session while it is running. Resize operation is not allowed for the currently active session."))
                return
            
            # Get current session size in MB
            current_size_mb = 100  # Default minimum
            
            if session_mode == 'dynfilefs':
                # For dynfilefs, use total_size (allocated size in bytes)
                if 'total_size' in session_info:
                    current_size_mb = session_info['total_size'] // (1024 * 1024)
            elif session_mode in ('raw', 'luks'):
                # For raw sessions, the 'size' field is the total allocated size in bytes
                if 'size' in session_info:
                    current_size_mb = session_info['size'] // (1024 * 1024)
            
            # Ensure we have a valid minimum size
            current_size_mb = max(100, int(current_size_mb))
            
        except (json.JSONDecodeError, KeyError):
            self._show_error(_("Failed to parse session information"))
            return
        
        # Create resize dialog
        dialog = Gtk.Dialog(
            title=_("Resize Session {}").format(session_id),
            parent=self.window
        )
        dialog.add_buttons(
            Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
            Gtk.STOCK_OK, Gtk.ResponseType.OK
        )
        _style_dialog_affirmative(dialog, _('Resize'))
        
        content_area = dialog.get_content_area()
        content_area.set_spacing(10)
        content_area.set_margin_start(10)
        content_area.set_margin_end(10)
        content_area.set_margin_top(10)
        content_area.set_margin_bottom(10)
        
        # Session info
        info_label = Gtk.Label()
        info_label.set_markup(f"<b>{_('Session:')} {session_id} ({session_mode})</b>")
        content_area.pack_start(info_label, False, False, 0)
        
        # Size input
        size_label = Gtk.Label(label=_("New size (MB):"))
        content_area.pack_start(size_label, False, False, 0)
        
        size_spin = Gtk.SpinButton()
        size_spin.set_range(current_size_mb, self._fat_size_limit() or 1000000)
        size_spin.set_increments(100, 1000)
        size_spin.set_value(current_size_mb)  # Set to current size
        content_area.pack_start(size_spin, False, False, 0)
        
        dialog.show_all()
        
        response = dialog.run()
        if response == Gtk.ResponseType.OK:
            new_size = int(size_spin.get_value())
            dialog.destroy()
            password_input = self._prompt_luks_passphrase() if session_mode == 'luks' else None
            if session_mode == 'luks' and password_input is None:
                return
            
            # Show loading overlay
            self._show_loading(True, _("Resizing session, please wait..."))
            
            # Perform resize in background
            def resize_session_bg():
                try:
                    args = ['resize', session_id, str(new_size), '--json']
                    if password_input is not None:
                        args.append('--password-stdin')
                    success, output, error = self._run_cli_command(args, password_input)
                    GLib.idle_add(self._on_resize_complete, success, output, error)
                except Exception as e:
                    GLib.idle_add(self._on_resize_complete, False, "", str(e))
            
            thread = threading.Thread(target=resize_session_bg)
            thread.daemon = True
            thread.start()
        else:
            dialog.destroy()

    def _on_resize_complete(self, success, output, error):
        """Handle resize completion"""
        # Hide loading overlay
        self._show_loading(False)

        if success:
            # Just refresh the session list, similar to create/delete operations
            self.refresh_session_list()
        else:
            try:
                if output:
                    result = _strict_json_loads(output)
                    message = result.get('message', error or _('Resize failed'))
                else:
                    message = error or _('Resize failed')
            except (TypeError, ValueError):
                message = error or _('Resize failed')
            self._show_error(message)

    def _show_export_dialog(self, session_id):
        """Show export dialog for a session"""
        # Create file chooser dialog
        dialog = Gtk.FileChooserDialog(
            title=_("Export Session {}").format(session_id),
            parent=self.window,
            action=Gtk.FileChooserAction.SAVE
        )
        dialog.add_buttons(
            Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
            Gtk.STOCK_SAVE, Gtk.ResponseType.OK
        )
        _style_dialog_affirmative(dialog, _('Export'))

        # Set default filename
        dialog.set_current_name(f"session_{session_id}.tar.zst")

        # Add file filter for tar.zst
        filter_tar = Gtk.FileFilter()
        filter_tar.set_name(_("TAR.ZSTD archives (*.tar.zst)"))
        filter_tar.add_pattern("*.tar.zst")
        dialog.add_filter(filter_tar)

        filter_all = Gtk.FileFilter()
        filter_all.set_name(_("All files"))
        filter_all.add_pattern("*")
        dialog.add_filter(filter_all)

        response = dialog.run()
        if response == Gtk.ResponseType.OK:
            output_path = dialog.get_filename()
            dialog.destroy()
            session_mode = self._get_session_mode(session_id)
            password_input = self._prompt_luks_passphrase() if session_mode == 'luks' else None
            if session_mode == 'luks' and password_input is None:
                return

            # Show loading overlay
            self._show_loading(True, _("Exporting session, please wait..."))

            # Perform export in background
            def export_session_bg():
                try:
                    args = ['export', session_id, output_path, '--json']
                    if password_input is not None:
                        args.append('--password-stdin')
                    success, output, error = self._run_cli_command(args, password_input)
                    GLib.idle_add(self._on_export_complete, success, output, error)
                except Exception as e:
                    GLib.idle_add(self._on_export_complete, False, "", str(e))

            thread = threading.Thread(target=export_session_bg)
            thread.daemon = True
            thread.start()
        else:
            dialog.destroy()

    def _on_export_complete(self, success, output, error):
        """Handle export completion"""
        # Hide loading overlay
        self._show_loading(False)

        if success:
            self._show_info(_("Session exported successfully"))
        else:
            try:
                if output:
                    result = _strict_json_loads(output)
                    message = result.get('message', error or _('Export failed'))
                else:
                    message = error or _('Export failed')
            except (TypeError, ValueError):
                message = error or _('Export failed')
            self._show_error(message)

    def on_import_clicked(self, button):
        """Handle import button click"""
        # Check if sessions directory is writable
        if not self.sessions_writable:
            self._show_error(_("Sessions directory is not writable. Cannot import sessions."))
            return

        self._show_import_dialog()

    def _show_import_dialog(self):
        """Show import dialog"""
        # Create file chooser dialog
        dialog = Gtk.FileChooserDialog(
            title=_("Import Session"),
            parent=self.window,
            action=Gtk.FileChooserAction.OPEN
        )
        dialog.add_buttons(
            Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
            Gtk.STOCK_OPEN, Gtk.ResponseType.OK
        )
        _style_dialog_affirmative(dialog, _('Open'))

        # Add file filter for tar.zst
        filter_tar = Gtk.FileFilter()
        filter_tar.set_name(_("TAR.ZSTD archives (*.tar.zst)"))
        filter_tar.add_pattern("*.tar.zst")
        dialog.add_filter(filter_tar)

        filter_all = Gtk.FileFilter()
        filter_all.set_name(_("All files"))
        filter_all.add_pattern("*")
        dialog.add_filter(filter_all)

        response = dialog.run()
        if response == Gtk.ResponseType.OK:
            archive_path = dialog.get_filename()
            dialog.destroy()

            # Show options dialog
            self._show_import_options_dialog(archive_path)
        else:
            dialog.destroy()

    def _show_import_options_dialog(self, archive_path):
        """Show import options dialog"""
        dialog = Gtk.Dialog(
            title=_("Import Options"),
            parent=self.window
        )
        dialog.add_buttons(
            Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
            Gtk.STOCK_OK, Gtk.ResponseType.OK
        )
        _style_dialog_affirmative(dialog, _('Import'))

        content_area = dialog.get_content_area()
        content_area.set_spacing(10)
        content_area.set_margin_start(10)
        content_area.set_margin_end(10)
        content_area.set_margin_top(10)
        content_area.set_margin_bottom(10)

        # File info
        file_label = Gtk.Label()
        file_label.set_markup(f"<b>{_('Archive:')} {os.path.basename(archive_path)}</b>")
        content_area.pack_start(file_label, False, False, 0)

        # Auto-convert option
        auto_convert_check = Gtk.CheckButton(label=_("Auto-convert to compatible mode if needed"))
        auto_convert_check.set_active(True)
        content_area.pack_start(auto_convert_check, False, False, 0)

        # Force mode option
        mode_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        mode_label = Gtk.Label(label=_("Force import to mode:"))
        mode_combo = Gtk.ComboBoxText()
        mode_combo.append("auto", _("Auto (from metadata)"))
        mode_combo.append("native", "Native")
        mode_combo.append("dynfilefs", "DynFileFS")
        mode_combo.append("raw", "Raw")
        if self.luks_available:
            mode_combo.append("luks", "LUKS")
        mode_combo.set_active_id("auto")
        mode_box.pack_start(mode_label, False, False, 0)
        mode_box.pack_start(mode_combo, True, True, 0)
        content_area.pack_start(mode_box, False, False, 0)

        dialog.show_all()

        response = dialog.run()
        if response == Gtk.ResponseType.OK:
            auto_convert = auto_convert_check.get_active()
            force_mode = mode_combo.get_active_id()
            if force_mode == "auto":
                force_mode = None
            dialog.destroy()
            password_input = self._prompt_luks_passphrase(confirm=True) if force_mode == 'luks' else None
            if force_mode == 'luks' and password_input is None:
                return

            # Show loading overlay
            self._show_loading(True, _("Importing session, please wait..."))

            # Perform import in background
            def import_session_bg():
                try:
                    args = ['import', archive_path, '--json']
                    if auto_convert:
                        args.append('--auto-convert')
                    if force_mode:
                        args.extend(['--force-mode', force_mode])
                    if password_input is not None:
                        args.append('--password-stdin')

                    success, output, error = self._run_cli_command(args, password_input)
                    GLib.idle_add(self._on_import_complete, success, output, error)
                except Exception as e:
                    GLib.idle_add(self._on_import_complete, False, "", str(e))

            thread = threading.Thread(target=import_session_bg)
            thread.daemon = True
            thread.start()
        else:
            dialog.destroy()

    def _on_import_complete(self, success, output, error):
        """Handle import completion"""
        # Hide loading overlay
        self._show_loading(False)

        if success:
            self._show_info(_("Session imported successfully"))
            self.refresh_session_list()
        else:
            try:
                if output:
                    result = _strict_json_loads(output)
                    message = result.get('message', error or _('Import failed'))
                else:
                    message = error or _('Import failed')
            except (TypeError, ValueError):
                message = error or _('Import failed')
            self._show_error(message)

    def _show_copy_dialog(self, session_id):
        """Show copy dialog for a session"""
        # Create copy dialog
        dialog = Gtk.Dialog(
            title=_("Copy Session {}").format(session_id),
            parent=self.window
        )
        dialog.add_buttons(
            Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
            Gtk.STOCK_OK, Gtk.ResponseType.OK
        )
        _style_dialog_affirmative(dialog, _('Copy'))

        content_area = dialog.get_content_area()
        content_area.set_spacing(10)
        content_area.set_margin_start(10)
        content_area.set_margin_end(10)
        content_area.set_margin_top(10)
        content_area.set_margin_bottom(10)

        # Session info
        info_label = Gtk.Label()
        info_label.set_markup(f"<b>{_('Copy session:')} {session_id}</b>")
        content_area.pack_start(info_label, False, False, 0)

        # Convert mode option
        convert_check = Gtk.CheckButton(label=_("Convert to different mode"))
        content_area.pack_start(convert_check, False, False, 0)

        # Mode selection
        mode_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        mode_label = Gtk.Label(label=_("Target mode:"))
        mode_combo = Gtk.ComboBoxText()
        mode_combo.append("native", "Native")
        mode_combo.append("dynfilefs", "DynFileFS")
        mode_combo.append("raw", "Raw")
        if self.luks_available:
            mode_combo.append("luks", "LUKS")
        mode_combo.set_active_id("native")
        mode_combo.set_sensitive(False)
        mode_box.pack_start(mode_label, False, False, 0)
        mode_box.pack_start(mode_combo, True, True, 0)
        content_area.pack_start(mode_box, False, False, 0)

        # Size input (for container targets)
        size_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        size_label = Gtk.Label(label=_("Size (MB):"))
        size_spin = Gtk.SpinButton()
        size_spin.set_range(100, self._fat_size_limit() or 1000000)
        size_spin.set_increments(100, 1000)
        size_spin.set_value(4000)
        size_spin.set_sensitive(False)
        size_box.pack_start(size_label, False, False, 0)
        size_box.pack_start(size_spin, True, True, 0)
        content_area.pack_start(size_box, False, False, 0)

        def on_convert_toggled(widget):
            mode_combo.set_sensitive(widget.get_active())
            mode = mode_combo.get_active_id()
            size_spin.set_sensitive(widget.get_active() and mode in ['dynfilefs', 'raw', 'luks'])

        def on_mode_changed(widget):
            mode = widget.get_active_id()
            size_spin.set_sensitive(convert_check.get_active() and mode in ['dynfilefs', 'raw', 'luks'])

        convert_check.connect("toggled", on_convert_toggled)
        mode_combo.connect("changed", on_mode_changed)

        dialog.show_all()

        response = dialog.run()
        if response == Gtk.ResponseType.OK:
            convert = convert_check.get_active()
            target_mode = mode_combo.get_active_id() if convert else None
            size_mb = int(size_spin.get_value()) if convert and target_mode in ['dynfilefs', 'raw', 'luks'] else None
            dialog.destroy()
            source_mode = self._get_session_mode(session_id)
            needs_password = source_mode == 'luks' or target_mode == 'luks'
            password_input = self._prompt_luks_passphrase(confirm=target_mode == 'luks') if needs_password else None
            if needs_password and password_input is None:
                return

            # Show loading overlay
            self._show_loading(True, _("Copying session, please wait..."))

            # Perform copy in background
            def copy_session_bg():
                try:
                    args = ['copy', session_id, '--json']
                    if target_mode:
                        args.extend(['--to-mode', target_mode])
                    if size_mb:
                        args.extend(['--size', str(size_mb)])
                    if password_input is not None:
                        args.append('--password-stdin')

                    success, output, error = self._run_cli_command(args, password_input)
                    GLib.idle_add(self._on_copy_complete, success, output, error)
                except Exception as e:
                    GLib.idle_add(self._on_copy_complete, False, "", str(e))

            thread = threading.Thread(target=copy_session_bg)
            thread.daemon = True
            thread.start()
        else:
            dialog.destroy()

    def _on_copy_complete(self, success, output, error):
        """Handle copy completion"""
        # Hide loading overlay
        self._show_loading(False)

        if success:
            self._show_info(_("Session copied successfully"))
            self.refresh_session_list()
        else:
            try:
                if output:
                    result = _strict_json_loads(output)
                    message = result.get('message', error or _('Copy failed'))
                else:
                    message = error or _('Copy failed')
            except (TypeError, ValueError):
                message = error or _('Copy failed')
            self._show_error(message)

    def _show_convert_dialog(self, session_id):
        """Show convert dialog for a session"""
        session_info = self._sessions_by_id.get(session_id)
        if session_info is None:
            self._show_error(_("Session not found"))
            return

        try:
            current_mode = session_info.get('mode', 'unknown')

        except (json.JSONDecodeError, KeyError):
            self._show_error(_("Failed to parse session information"))
            return

        # Create convert dialog
        dialog = Gtk.Dialog(
            title=_("Convert Session {}").format(session_id),
            parent=self.window
        )
        dialog.add_buttons(
            Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
            Gtk.STOCK_OK, Gtk.ResponseType.OK
        )
        _style_dialog_affirmative(dialog, _('Convert'))

        content_area = dialog.get_content_area()
        content_area.set_spacing(10)
        content_area.set_margin_start(10)
        content_area.set_margin_end(10)
        content_area.set_margin_top(10)
        content_area.set_margin_bottom(10)

        # Session info
        info_label = Gtk.Label()
        info_label.set_markup(f"<b>{_('Convert session:')} {session_id}</b>")
        content_area.pack_start(info_label, False, False, 0)

        current_label = Gtk.Label(label=_("Current mode: {}").format(current_mode))
        content_area.pack_start(current_label, False, False, 0)

        # Target mode selection
        mode_label = Gtk.Label(label=_("Target mode:"))
        content_area.pack_start(mode_label, False, False, 0)

        mode_combo = Gtk.ComboBoxText()
        modes = ['native', 'dynfilefs', 'raw']
        if self.luks_available:
            modes.append('luks')
        for mode in modes:
            if mode != current_mode:
                mode_combo.append(mode, mode.capitalize())
        mode_combo.set_active(0)
        content_area.pack_start(mode_combo, False, False, 0)

        # Size input (for container modes)
        size_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        size_label = Gtk.Label(label=_("Size (MB):"))
        size_spin = Gtk.SpinButton()
        size_spin.set_range(100, self._fat_size_limit() or 1000000)
        size_spin.set_increments(100, 1000)
        # Use current session size as default if available
        default_size = 4000
        if session_info.get('total_size'):
            # dynfilefs: total_size is in bytes, convert to MB
            default_size = int(session_info['total_size'] / (1024 * 1024))
        elif current_mode == 'raw' and session_info.get('size'):
            # raw: size field contains the image file size in bytes
            default_size = int(session_info['size'] / (1024 * 1024))
        elif current_mode == 'native' and session_info.get('size'):
            # native: use actual size + 100 MB
            default_size = int(session_info['size'] / (1024 * 1024)) + 100
        elif session_info.get('total_size_mb'):
            default_size = session_info['total_size_mb']
            if isinstance(default_size, str):
                try:
                    default_size = int(default_size)
                except ValueError:
                    default_size = 4000
        size_spin.set_value(default_size)
        size_box.pack_start(size_label, False, False, 0)
        size_box.pack_start(size_spin, True, True, 0)
        content_area.pack_start(size_box, False, False, 0)

        def on_mode_changed(widget):
            mode = widget.get_active_id()
            size_spin.set_sensitive(mode in ['dynfilefs', 'raw', 'luks'])

        mode_combo.connect("changed", on_mode_changed)
        on_mode_changed(mode_combo)  # Initialize

        dialog.show_all()

        response = dialog.run()
        if response == Gtk.ResponseType.OK:
            target_mode = mode_combo.get_active_id()
            size_mb = int(size_spin.get_value()) if target_mode in ['dynfilefs', 'raw', 'luks'] else None
            dialog.destroy()
            needs_password = current_mode == 'luks' or target_mode == 'luks'
            password_input = self._prompt_luks_passphrase(confirm=target_mode == 'luks') if needs_password else None
            if needs_password and password_input is None:
                return

            # Show loading overlay
            self._show_loading(True, _("Converting session, please wait..."))

            # Perform convert in background
            def convert_session_bg():
                try:
                    args = ['convert', session_id, target_mode, '--json']
                    if size_mb:
                        args.extend(['--size', str(size_mb)])
                    if password_input is not None:
                        args.append('--password-stdin')

                    success, output, error = self._run_cli_command(args, password_input)
                    GLib.idle_add(self._on_convert_complete, success, output, error)
                except Exception as e:
                    GLib.idle_add(self._on_convert_complete, False, "", str(e))

            thread = threading.Thread(target=convert_session_bg)
            thread.daemon = True
            thread.start()
        else:
            dialog.destroy()

    def _on_convert_complete(self, success, output, error):
        """Handle convert completion"""
        # Hide loading overlay
        self._show_loading(False)

        if success:
            self._show_info(_("Session converted successfully"))
            self.refresh_session_list()
        else:
            try:
                if output:
                    result = _strict_json_loads(output)
                    message = result.get('message', error or _('Convert failed'))
                else:
                    message = error or _('Convert failed')
            except (TypeError, ValueError):
                message = error or _('Convert failed')
            self._show_error(message)

    def run(self):
        """Start the application"""
        self.window.show_all()
        try:
            Gtk.main()
        except KeyboardInterrupt:
            pass

def main():
    """Main entry point"""
    app = SessionManagerGUI()
    app.run()

if __name__ == '__main__':
    main()
