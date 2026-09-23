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
from gi.repository import Gdk, Gtk, GLib, Pango
from minios_gui import (BackgroundTask, OperationView, ProgressDialog,
                        StatusBanner, apply_minios_css, ask_confirmation,
                        choose_open_file, choose_save_file, new_header_bar,
                        new_icon, show_error_dialog, show_info_dialog)
from minios_session_ui import save_phase_text, send_desktop_notification

# Internationalization setup
try:
    gettext.bindtextdomain('minios-session-manager', '/usr/share/locale')
    gettext.textdomain('minios-session-manager')
    _ = gettext.gettext
except Exception:
    _ = lambda x: x


DYNBLK_COMPRESSION_CODECS = (
    'none', 'lz4', 'lz4hc', 'lzo', 'lzo-rle', 'zstd', 'deflate', '842',
)


def _mode_display_name(mode):
    return {
        'native': 'Native',
        'squashfs': 'SquashFS',
        'dynfilefs': 'DynFileFS',
        'dynblk': 'DynBlk',
        'vmdk': 'VMDK',
        'raw': 'Raw',
    }.get(mode, mode)


def _style_dialog_affirmative(dialog, label):
    button = dialog.get_widget_for_response(Gtk.ResponseType.OK)
    if button is not None:
        button.set_label(label)
        button.get_style_context().add_class('suggested-action')
    dialog.set_default_response(Gtk.ResponseType.OK)


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


def _privileged_command(command):
    """Use polkit only when the current process is not already privileged."""
    command = list(command)
    if os.geteuid() == 0:
        return command
    return ['pkexec'] + command


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
        self._loading_visible = False
        self._session_mounts = {}
        self._mount_generation = 0
        self._snapshot_time = None
        self._sessions_by_id = {}
        self._filesystem_info = {}
        self.luks_backends = {
            mode: False
            for mode in ('raw', 'dynfilefs', 'dynblk', 'vmdk')
        }
        self.dynblk_compression_codecs = ('none',)
        
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
            cmd = _privileged_command([self.cli_command] + args)
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

    def _start_cli_task(self, args, finished, input_data=None):
        """Run one ordinary CLI request and deliver its TaskOutcome safely."""
        def worker(token):
            token.raise_if_cancelled()
            result = self._run_cli_command(args, input_data)
            token.raise_if_cancelled()
            return result

        def complete(outcome):
            if outcome.succeeded:
                finished(*outcome.value)
            elif not outcome.cancelled:
                finished(False, "", str(outcome.error))
            elif not self._closing:
                finished(False, "", _("Operation cancelled"))

        return BackgroundTask(worker, complete, owner=self.window).start()

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
            cmd = _privileged_command([
                self.cli_command, 'save', session_id, '--json', '--progress'])
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

    def _dynblk_size_limit(self, mode='dynblk'):
        # The backend supplies geometry limits; keep old backend JSON usable.
        value = self._filesystem_info.get('vmdk_max_size_mb' if mode == 'vmdk' else 'dynblk_max_size_mb')
        return value if type(value) is int and value > 0 else 512 * 1024

    def _fat_size_limit(self):
        return self._filesystem_info.get('limitations', {}).get('max_file_size')

    def _on_window_destroy(self, _window):
        """Terminate the active privileged child before the GUI exits."""
        with self._cli_process_lock:
            self._closing = True
            process = self._cli_process
        for mount in list(self._session_mounts.values()):
            try:
                mount['process'].stdin.close()
            except (BrokenPipeError, OSError, ValueError):
                pass
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
        self.sessions_status_banner = banner
        # Keep a healthy status out of the way, including after window.show_all().
        banner.set_no_show_all(True)
        self.sessions_status_retry = Gtk.Button(label=_("Retry"))
        self.sessions_status_retry.connect(
            'clicked', self._retry_sessions_directory_status)
        self.sessions_status_retry.set_no_show_all(True)
        self.sessions_status_retry.set_visible(
            not self._status_pending and
            self.sessions_status.get('_query_error', False))
        banner.pack_end(self.sessions_status_retry, False, False, 0)
        main_box.pack_start(banner, False, False, 0)
        self._update_sessions_status_banner()

    def _update_sessions_status_banner(self):
        """Show only actionable session-directory status information."""
        intent, status_text = self._sessions_status_presentation()
        self.sessions_status_banner.set_intent(intent)
        self.sessions_status_banner.label.set_markup(
            '<b>{}</b>'.format(GLib.markup_escape_text(status_text)))
        self.sessions_status_banner.set_visible(intent != 'success')

    def _retry_sessions_directory_status(self, _button):
        """Retry a failed status query without blocking the GTK main loop."""
        self._status_retry_generation += 1
        generation = self._status_retry_generation
        self._status_pending = True
        self._update_sessions_status_banner()
        self.sessions_status_retry.set_visible(False)
        self.sessions_status_retry.set_sensitive(False)

        def check_status(token):
            token.raise_if_cancelled()
            return self._check_sessions_directory_status()

        def finish_status(outcome):
            if outcome.succeeded:
                self._finish_sessions_directory_status_retry(
                    generation, outcome.value)
            elif not outcome.cancelled:
                self._finish_sessions_directory_status_retry(generation, {
                    'success': False, 'found': False, 'writable': False,
                    '_query_error': True, 'error': str(outcome.error),
                })

        BackgroundTask(
            check_status, finish_status, owner=self.window).start()

    def _finish_sessions_directory_status_retry(self, generation, status):
        """Apply a status retry result on the GTK main thread."""
        if generation != self._status_retry_generation or self._closing:
            return False
        self._status_pending = False
        self.sessions_status = status
        self.sessions_writable = self.sessions_status.get('writable', False)
        self._update_sessions_status_banner()
        query_error = self.sessions_status.get('_query_error', False)
        self.sessions_status_retry.set_visible(query_error)
        self.sessions_status_retry.set_sensitive(True)
        self._update_footer_sensitivity()
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
        self.window.set_default_size(680, 500)
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
        self.loading_box = OperationView(
            status=_("Loading sessions..."), cancellable=False)
        self.loading_spinner = self.loading_box.spinner
        self.loading_label = self.loading_box.status_label
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
        toolbar_box.set_halign(Gtk.Align.CENTER)
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

        def fetch_data(token):
            """Fetch and validate one complete list snapshot."""
            list_success, list_output, list_error = self._run_cli_command(['list', '--json'])
            active_success, active_output, active_error = self._run_cli_command(['active', '--json'])
            running_success, running_output, running_error = self._run_cli_command(['running', '--json'])
            info_success, info_output, info_error = self._run_cli_command(['info', '--json'])
            token.raise_if_cancelled()

            if not active_success or not running_success:
                detail = active_error if not active_success else running_error
                raise ValueError(self._cli_error_text(detail))

            active_session_id = None
            if active_output.strip():
                active_data = _strict_json_loads(active_output.strip())
                if active_data is not None and not isinstance(active_data, dict):
                    raise ValueError('active response is not an object or null')
                if active_data is not None:
                    active_session_id = active_data.get('id')
                    if not isinstance(active_session_id, str) or not active_session_id:
                        raise ValueError('active response has an invalid id')
            else:
                raise ValueError('active response is empty')

            running_session_id = None
            if running_output.strip():
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
                    self._cli_error_text(info_error) or
                    'filesystem information is unavailable')
            if not info_output.strip():
                raise ValueError('filesystem information response is empty')
            filesystem_info = _strict_json_loads(info_output.strip())
            if not self._valid_filesystem_info(filesystem_info):
                raise ValueError('filesystem information response is invalid')
            return (list_success, list_output, list_error, active_session_id,
                    running_session_id, filesystem_info)

        def finish_fetch(outcome):
            if outcome.succeeded:
                self._process_session_data(generation, *outcome.value)
            elif not outcome.cancelled:
                self._process_session_fetch_error(
                    generation, _("Error fetching session data: {}").format(
                        str(outcome.error)))
        
        # Show loading indicator
        self._show_loading(True)
        
        BackgroundTask(fetch_data, finish_fetch, owner=self.window).start()

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
        encryptions = info.get('compatible_encryptions')
        codecs = info.get('dynblk_compression_codecs', ['none'])
        if encryptions is None:
            encryptions = {mode: ['none'] for mode in compatible_modes or []}
        if (not isinstance(filesystem, dict) or
                not isinstance(filesystem.get('type'), str) or
                not filesystem['type'] or
                not isinstance(compatible_modes, list) or
                any(not isinstance(mode, str) or not mode
                    for mode in compatible_modes) or
                not isinstance(limitations, dict) or
                not isinstance(encryptions, dict) or
                not isinstance(codecs, list) or not codecs or codecs[0] != 'none' or
                any(codec not in DYNBLK_COMPRESSION_CODECS for codec in codecs)):
            return False
        for mode in compatible_modes:
            values = encryptions.get(mode)
            if (not isinstance(values, list) or not values or
                    values[0] != 'none' or
                    any(value not in ('none', 'luks') for value in values)):
                return False
        for key in ('dynblk_max_size_mb', 'vmdk_max_size_mb'):
            limit = info.get(key)
            if limit is not None and (type(limit) is not int or
                    not 0 < limit <= ((1 << 63) - 1) // (1 << 20)):
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
        if session.get('encryption', 'none') not in ('none', 'luks'):
            return False
        if not isinstance(session.get('configuration_supported', True), bool):
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
            available_encryptions = self._filesystem_info.get(
                'compatible_encryptions', {})
            self.luks_backends = {
                mode: 'luks' in available_encryptions.get(mode, [])
                for mode in ('raw', 'dynfilefs', 'dynblk', 'vmdk')
            }
            self.dynblk_compression_codecs = tuple(
                self._filesystem_info.get('dynblk_compression_codecs') or ['none'])
            self._snapshot_time = time.monotonic()
            self._update_footer_sensitivity()

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
                encryption = session.get('encryption', 'none')
                configuration_supported = session.get(
                    'configuration_supported', True)
                version = session.get('version', 'unknown')
                edition = session.get('edition', 'unknown')
                union = session.get('union', 'unknown')
                size = session.get('size_formatted', 'unknown')

                # Thin session rows show allocated use alongside their limit.
                if (mode in ('dynfilefs', 'dynblk', 'vmdk') and
                        'total_size_formatted' in session):
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
                    edition, union, size, modified, encryption,
                    configuration_supported)
            
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

    def _create_session_row(self, session_id, is_active, is_running, mode,
                            version, edition, union, size, modified,
                            encryption='none', configuration_supported=True):
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
        mode_display = _mode_display_name(mode) if encryption == 'none' else '{} + LUKS2'.format(_mode_display_name(mode))
        mode_label.set_markup(f'<span size="small"><b>{mode_text}</b> {GLib.markup_escape_text(mode_display)}</span>')
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
        row.encryption = encryption
        row.configuration_supported = configuration_supported
        
        self.sessions_list.add(row)

    def _on_session_selected(self, list_box, row):
        """Handle session selection"""
        if row:
            self.selected_session_id = row.session_id
        else:
            self.selected_session_id = None

    def _create_context_menu(self):
        """Create context menu for session items"""
        self.context_menu = Gtk.Menu()
        self.context_menu.get_style_context().add_class('session-context-menu')

        # Activate menu item
        activate_item = Gtk.MenuItem.new_with_mnemonic(_("_Activate Session"))
        activate_item.get_style_context().add_class('context-menu-activate')
        activate_item.connect("activate", self._on_context_activate)
        self.context_menu.append(activate_item)

        save_now_item = Gtk.MenuItem.new_with_mnemonic(_("_Save Now"))
        save_now_item.connect("activate", self._on_context_save_now)
        self.context_menu.append(save_now_item)

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

        clone_item = Gtk.MenuItem.new_with_mnemonic(_("C_lone Session"))
        clone_item.get_style_context().add_class('context-menu-clone')
        clone_item.connect("activate", self._on_context_clone)
        self.context_menu.append(clone_item)

        # Convert menu item
        convert_item = Gtk.MenuItem.new_with_mnemonic(_("Con_vert Session"))
        convert_item.get_style_context().add_class('context-menu-convert')
        convert_item.connect("activate", self._on_context_convert)
        self.context_menu.append(convert_item)

        # Separator
        separator2 = Gtk.SeparatorMenuItem()
        self.context_menu.append(separator2)

        self.mount_item = Gtk.MenuItem.new_with_mnemonic(_("_Mount Session"))
        self.mount_item.connect("activate", self._on_context_mount)
        self.context_menu.append(self.mount_item)

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

        self.reclaim_item = Gtk.MenuItem.new_with_mnemonic(_("_Free Space..."))
        self.reclaim_item.connect("activate", self._on_context_reclaim)
        self.context_menu.append(self.reclaim_item)
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
        activate_item, save_now_item, save_settings_item, resize_item = children[0:4]
        export_item, copy_item, clone_item, convert_item = children[5:9]
        delete_item = children[13]
        mode = getattr(row, 'mode', 'unknown')
        session_id = getattr(row, 'session_id', None)
        mounted = session_id in self._session_mounts
        mount_in_progress = bool(self._session_mounts)
        mountable = (mode in ('dynfilefs', 'dynblk', 'vmdk', 'raw') and
                     getattr(row, 'configuration_supported', True))
        self.mount_item.set_visible(mountable)
        self.mount_item.set_label(
            _("_Unmount Session") if mounted else _("_Mount Session"))
        self.mount_item.set_sensitive(
            mounted or (mountable and self.sessions_writable and
                        not getattr(row, 'is_active', False) and
                        not getattr(row, 'is_running', False) and
                        not mount_in_progress))
        self.reclaim_item.set_visible(mode in ('dynfilefs', 'dynblk', 'vmdk'))
        self.reclaim_item.set_sensitive(not mount_in_progress and
                                        self.sessions_writable and
                                        getattr(row, 'configuration_supported', True))
        is_squashfs = mode == 'squashfs'
        resize_available = mode in ('dynfilefs', 'dynblk', 'vmdk', 'raw')
        supported_operations = getattr(row, 'mode', 'unknown') != 'squashfs'
        supported_operations = (
            supported_operations and
            getattr(row, 'configuration_supported', True))
        active = getattr(row, 'is_active', False)
        running = getattr(row, 'is_running', False)

        activate_item.set_sensitive(
            not mount_in_progress and self.sessions_writable and not active and
            getattr(row, 'configuration_supported', True))
        save_now_item.set_visible(is_squashfs)
        save_settings_item.set_visible(is_squashfs)
        save_now_item.set_sensitive(
            not mount_in_progress and self.sessions_writable and
            is_squashfs and running)
        save_settings_item.set_sensitive(
            not mount_in_progress and self.sessions_writable and is_squashfs)
        resize_item.set_sensitive(
            not mount_in_progress and self.sessions_writable and
            not running and resize_available)
        export_item.set_sensitive(
            not mount_in_progress and not running and supported_operations)
        copy_item.set_sensitive(
            not mount_in_progress and self.sessions_writable and
            not running and supported_operations)
        clone_item.set_sensitive(
            not mount_in_progress and self.sessions_writable and
            not running and supported_operations)
        convert_item.set_sensitive(
            not mount_in_progress and self.sessions_writable and
            not running and supported_operations)
        delete_item.set_sensitive(
            not mount_in_progress and self.sessions_writable and not active and
            (not running or getattr(row, 'mode', 'unknown') == 'squashfs'))

    def _on_context_activate(self, menu_item):
        """Handle activate from context menu"""
        if self.selected_session_id:
            self.on_activate_clicked(None)

    def _on_context_delete(self, menu_item):
        """Handle delete from context menu"""
        if self.selected_session_id:
            self.on_delete_clicked(None)

    def _on_context_save_now(self, menu_item):
        """Save the selected running SquashFS session."""
        if self.selected_session_id:
            self.on_save_clicked(None)

    def _on_context_save_settings(self, menu_item):
        """Configure automatic saving for a SquashFS session."""
        if self.selected_session_id:
            self._show_squashfs_settings_dialog(self.selected_session_id)

    def _on_context_mount(self, _menu_item):
        """Mount or unmount the selected detached container session."""
        session_id = self.selected_session_id
        if not session_id:
            return
        if session_id in self._session_mounts:
            self._stop_session_mount(session_id)
            return
        session = self._sessions_by_id.get(session_id, {})
        if (session.get('is_default') or session.get('is_running') or
                session.get('mode') not in ('dynfilefs', 'dynblk', 'vmdk', 'raw')):
            return
        password = None
        args = ['mount', session_id, '--json']
        if session.get('encryption', 'none') == 'luks':
            password = self._prompt_luks_passphrase()
            if password is None:
                return
            args.append('--password-stdin')
        self._start_session_mount(session_id, args, password)

    def _start_session_mount(self, session_id, args, password=None):
        """Start the privileged foreground mount and wait for its ready frame."""
        self._mount_generation += 1
        generation = self._mount_generation
        process = None
        try:
            process = subprocess.Popen(
                _privileged_command([self.cli_command] + args),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, universal_newlines=True, bufsize=1)
            if password is not None:
                process.stdin.write(password)
                process.stdin.flush()
        except Exception as error:
            if process is not None and process.poll() is None:
                process.terminate()
            self._show_error(_("Failed to mount session: {}").format(str(error)))
            return
        state = {
            'process': process, 'generation': generation, 'mount_point': None,
            'stopping': False, 'ready': False, 'message': None,
        }
        self._session_mounts[session_id] = state
        self._show_loading(True, _("Mounting session, please wait..."))

        def monitor_mount(token):
            token.raise_if_cancelled()
            reply = None
            try:
                line = process.stdout.readline()
                reply = _strict_json_loads(line) if line else None
            except (OSError, TypeError, ValueError) as error:
                reply = {'success': False, 'message': str(error)}
            if (not isinstance(reply, dict) or
                    type(reply.get('success')) is not bool):
                reply = {
                    'success': False,
                    'message': _("Invalid response from the session backend."),
                }
            if reply['success']:
                mount_point = reply.get('mount_point')
                if not isinstance(mount_point, str) or not os.path.isabs(mount_point):
                    reply = {
                        'success': False,
                        'message': _("Invalid mount path from the session backend."),
                    }
                else:
                    GLib.idle_add(
                        self._on_session_mount_ready, session_id, generation,
                        mount_point)
            if not reply['success']:
                state['message'] = reply.get('message')
                try:
                    process.stdin.close()
                except (BrokenPipeError, OSError, ValueError):
                    pass
            return_code = process.wait()
            try:
                error_text = process.stderr.read().strip()
            except (OSError, ValueError):
                error_text = ''
            return return_code, error_text

        def finish_mount(outcome):
            if outcome.succeeded:
                return_code, error_text = outcome.value
            else:
                return_code, error_text = 1, str(outcome.error or '')
            self._on_session_mount_exited(
                session_id, generation, return_code, error_text)

        BackgroundTask(
            monitor_mount, finish_mount, owner=self.window).start()

    def _on_session_mount_ready(self, session_id, generation, mount_point):
        state = self._session_mounts.get(session_id)
        if (state is None or state['generation'] != generation or self._closing):
            return False
        state['ready'] = True
        state['mount_point'] = mount_point
        self._show_loading(False)
        try:
            subprocess.Popen(['xdg-open', mount_point])
        except Exception as error:
            self._show_error(_("Failed to open mounted session: {}").format(
                str(error)))
        return False

    def _stop_session_mount(self, session_id):
        state = self._session_mounts.get(session_id)
        if state is None or state['stopping']:
            return
        state['stopping'] = True
        self._show_loading(True, _("Unmounting session, please wait..."))
        try:
            state['process'].stdin.close()
        except (BrokenPipeError, OSError, ValueError):
            pass

    def _on_session_mount_exited(self, session_id, generation, return_code,
                                 error_text):
        state = self._session_mounts.get(session_id)
        if state is None or state['generation'] != generation:
            return False
        self._session_mounts.pop(session_id, None)
        if self._closing:
            return False
        self._show_loading(False)
        self.refresh_session_list()
        message = state.get('message') or error_text
        if return_code != 0:
            self._show_error(message or _("Failed to unmount session."))
        elif state['ready'] and not state['stopping']:
            self._show_error(_("The mounted session was disconnected unexpectedly."))
        return False

    def _on_context_reclaim(self, _menu_item):
        session_id = self.selected_session_id
        session = self._sessions_by_id.get(session_id, {})
        if not self.sessions_writable or session.get('mode') not in ('dynfilefs', 'dynblk', 'vmdk'):
            return
        dialog = Gtk.Dialog(title=_("Free Space — Session {}").format(session_id),
                            transient_for=self.window, modal=True)
        dialog.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                           _("Free Space"), Gtk.ResponseType.OK)
        _style_dialog_affirmative(dialog, _("Free Space"))
        box = dialog.get_content_area()
        box.set_spacing(12)
        box.set_border_width(12)
        note = Gtk.Label(label=_(
            "Return unused container space without conversion. By default, live data is not moved. "
            "On FAT32/exFAT, free space inside the file may remain allocated."))
        note.set_line_wrap(True)
        note.set_max_width_chars(64)
        note.set_xalign(0)
        box.pack_start(note, False, False, 0)
        compact = Gtk.CheckButton(label=_("Also compact by moving live data (additional flash writes)"))
        compact.set_active(False)
        box.pack_start(compact, False, False, 0)
        if session.get('encryption', 'none') != 'none':
            encrypted = Gtk.Label(label=_(
                "Encrypted session: only known free space is reclaimed. Discard through LUKS is not enabled automatically."))
            encrypted.set_line_wrap(True)
            encrypted.set_max_width_chars(64)
            encrypted.set_xalign(0)
            box.pack_start(encrypted, False, False, 0)
        dialog.show_all()
        response = dialog.run()
        move_data = compact.get_active()
        dialog.destroy()
        if response != Gtk.ResponseType.OK:
            return
        args = ['reclaim', session_id, '--json']
        if move_data:
            args.append('--compact')
        self._show_loading(True, _("Reclaiming session space..."))
        self._start_cli_task(args, self._on_reclaim_complete)

    def _on_reclaim_complete(self, success, output, error):
        self._show_loading(False)
        try:
            result = _strict_json_loads(output) if output else {}
            if not isinstance(result, dict) or type(result.get('success')) is not bool:
                raise ValueError('invalid reclaim reply')
            success = success and result['success']
            message = result.get('message') or error or _("Space reclamation finished.")
        except (TypeError, ValueError):
            message = error or _("Invalid response from the session backend.")
            success = False
        self.refresh_session_list()
        if success:
            self._show_info(message)
        else:
            self._show_error(message)

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

    def _on_context_clone(self, menu_item):
        """Create a detached physical clone of the selected session."""
        if not self.selected_session_id:
            return
        self._show_loading(True, _("Cloning session, please wait..."))
        args = ['clone', self.selected_session_id, '--json']
        self._start_cli_task(args, self._on_clone_complete)

    def _on_clone_complete(self, success, output, error):
        self._show_loading(False)
        if success:
            self._show_info(_("Session cloned successfully"))
            self.refresh_session_list()
            return
        try:
            result = _strict_json_loads(output) if output else {}
            message = result.get('message', error or _("Clone failed"))
        except (TypeError, ValueError):
            message = error or _("Clone failed")
        self._show_error(message)

    def _on_context_convert(self, menu_item):
        """Handle convert from context menu"""
        if self.selected_session_id:
            self._show_convert_dialog(self.selected_session_id)

    def _on_context_open_folder(self, menu_item):
        """Handle open folder from context menu"""
        if self.selected_session_id:
            import subprocess
            session = self._sessions_by_id.get(self.selected_session_id, {})
            mount = self._session_mounts.get(self.selected_session_id, {})
            session_path = mount.get('mount_point') or session.get('path')
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
        compatible_modes = ['native', 'dynfilefs', 'raw']  # Capability-neutral fallback
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
        
        if 'dynblk' in compatible_modes:
            base_radio = first_radio if first_radio else None
            dynblk_radio = Gtk.RadioButton.new_with_label_from_widget(base_radio, _("DynBlk Mode"))
            dynblk_radio.set_tooltip_text(_("Native compressed block-device container"))
            content_area.pack_start(dynblk_radio, False, False, 0)
            radio_buttons['dynblk'] = dynblk_radio
            if first_radio is None:
                first_radio = dynblk_radio

        if 'vmdk' in compatible_modes:
            vmdk_radio = Gtk.RadioButton.new_with_label_from_widget(first_radio, _("VMDK Mode"))
            vmdk_radio.set_tooltip_text(_("Split sparse VMDK image on the DynBlk driver; no compression"))
            content_area.pack_start(vmdk_radio, False, False, 0)
            radio_buttons['vmdk'] = vmdk_radio
            if first_radio is None:
                first_radio = vmdk_radio

        if 'raw' in compatible_modes:
            base_radio = first_radio if first_radio else None
            raw_radio = Gtk.RadioButton.new_with_label_from_widget(base_radio, _("Raw Mode"))
            raw_radio.set_tooltip_text(_("Static image files"))
            content_area.pack_start(raw_radio, False, False, 0)
            radio_buttons['raw'] = raw_radio
            if first_radio is None:
                first_radio = raw_radio

        encryption_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        encryption_box.pack_start(Gtk.Label(label=_("Encryption:")), False, False, 0)
        encryption_combo = Gtk.ComboBoxText()
        encryption_combo.append('none', _("None"))
        if any(self.luks_backends.values()):
            encryption_combo.append('luks', _("LUKS2"))
        encryption_combo.set_active_id('none')
        encryption_box.pack_start(encryption_combo, False, False, 0)
        content_area.pack_start(encryption_box, False, False, 0)

        compression_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        compression_box.pack_start(Gtk.Label(label=_("DynBlk compression:")), False, False, 0)
        compression_combo = Gtk.ComboBoxText()
        for codec in self.dynblk_compression_codecs:
            compression_combo.append(codec, codec)
        compression_combo.set_active_id('none')
        compression_box.pack_start(compression_combo, False, False, 0)
        content_area.pack_start(compression_box, False, False, 0)

        # SquashFS save policy and optional periodic saving. Hide this whole
        # section for every other backend instead of presenting disabled controls.
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
        
        size_info_label = Gtk.Label(label=_("Available for container modes"))
        size_info_label.set_halign(Gtk.Align.START)
        size_info_label.get_style_context().add_class('field-description')
        size_box.pack_start(size_info_label, False, False, 0)
        
        # Enable/disable size controls based on mode selection
        def on_mode_changed(radio):
            # Check which radio buttons exist and are active
            is_dynfilefs_active = 'dynfilefs' in radio_buttons and radio_buttons['dynfilefs'].get_active()
            is_dynblk_active = 'dynblk' in radio_buttons and radio_buttons['dynblk'].get_active()
            is_vmdk_active = 'vmdk' in radio_buttons and radio_buttons['vmdk'].get_active()
            is_raw_active = 'raw' in radio_buttons and radio_buttons['raw'].get_active()
            is_squashfs_active = 'squashfs' in radio_buttons and radio_buttons['squashfs'].get_active()
            is_sized_mode = is_dynfilefs_active or is_dynblk_active or is_vmdk_active or is_raw_active
            selected_mode = next((
                name for name, button in radio_buttons.items()
                if button.get_active()), None)
            encryption_supported = self.luks_backends.get(
                selected_mode, False)
            encryption_box.set_sensitive(encryption_supported)
            if not encryption_supported:
                encryption_combo.set_active_id('none')
            compression_enabled = (
                is_dynblk_active and (encryption_combo.get_active_id() or 'none') == 'none')
            compression_box.set_sensitive(compression_enabled)
            if not compression_enabled:
                compression_combo.set_active_id('none')

            if is_squashfs_active:
                squashfs_options.show_all()
            else:
                squashfs_options.hide()
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

            # Keep helper text visually secondary rather than disabling it. A
            # disabled label looks unavailable even when it describes the
            # currently selected container mode.
            if is_raw_active and 'max_file_size' in limitations:
                max_size = limitations['max_file_size']
                current_size = int(size_spinbutton.get_value())
                if current_size > max_size:
                    size_spinbutton.set_value(max_size)
                adjustment.set_upper(max_size)
                size_info_label.set_text(_("Maximum {} MB on FAT32").format(max_size))
            elif is_dynblk_active or is_vmdk_active:
                maximum = self._dynblk_size_limit(selected_mode)
                adjustment.set_upper(maximum)
                if int(size_spinbutton.get_value()) == 4000:
                    size_spinbutton.set_value(16384)
                elif size_spinbutton.get_value() > maximum:
                    size_spinbutton.set_value(maximum)
                size_info_label.set_text(_(
                    "Thin container: default 16 GiB, backend maximum {} MiB").format(maximum))
            elif is_dynfilefs_active:
                adjustment.set_upper(1000000)
                size_info_label.set_text(_(
                    "Thin container: backing storage grows on demand"))
            elif is_raw_active:
                adjustment.set_upper(1000000)
                size_info_label.set_text(_("Fixed-size image"))
            else:
                adjustment.set_upper(1000000)
                size_info_label.set_text(_("Available for container modes"))
        
        # Connect signals only for existing radio buttons
        for mode, radio in radio_buttons.items():
            radio.connect("toggled", on_mode_changed)
        encryption_combo.connect("changed", on_mode_changed)
        shutdown_check.connect("toggled", on_mode_changed)
        autosave_combo.connect("changed", on_mode_changed)
        
        # Show ordinary controls first, then apply mode-specific visibility so
        # non-SquashFS modes never display a disabled automatic-save section.
        dialog.show_all()
        on_mode_changed(None)
        
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
            encryption = encryption_combo.get_active_id() or 'none'
            compression = compression_combo.get_active_id() or 'none'

            dialog.destroy()
            
            password_input = self._prompt_luks_passphrase(confirm=True) if encryption == 'luks' else None
            if encryption == 'luks' and password_input is None:
                return
            if mode in ["dynfilefs", "dynblk", "vmdk", "raw"]:
                command = ['create', mode, str(size_mb), '--json']
                if mode == 'dynblk' and compression != 'none':
                    command.extend(['--compression', compression])
                if encryption == 'luks':
                    command.extend(['--encryption', 'luks', '--password-stdin'])
            elif mode == 'squashfs':
                command = [
                    'create', mode, '--policy', squashfs_policy,
                    '--autosave', str(squashfs_autosave), '--json']
            else:
                command = ['create', mode, '--json']
            self._show_loading(True, _("Creating new session, please wait..."))
            self._start_cli_task(
                command,
                lambda success, output, error:
                self._on_session_creation_complete(
                    success, output, error, None),
                password_input)
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
                state['dialog'].message_label.set_text(save_phase_text(phase))
            return False

        def show_progress_if_needed():
            if state['done']:
                return False
            dialog = self._create_progress_dialog(
                _("Saving Session"), save_phase_text(state['phase']))
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
                    send_desktop_notification(
                        _("Session saved"),
                        _("SquashFS session #{} was saved successfully.").format(session_id),
                        timeout_ms=4000)
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
        
        self._start_cli_task(
            ['activate', session_id, '--json'],
            lambda success, output, error: self._on_session_operation_complete(
                success, output, error, None,
                _("Session activated successfully"),
                _("Failed to activate session")))

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

        command = ['delete', session_id]
        if handoff:
            command.append('--handoff')
        command.append('--json')
        self._start_cli_task(
            command,
            lambda success, output, error: self._on_session_operation_complete(
                success, output, error, None,
                _("Session deleted successfully"),
                _("Failed to delete session")))

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
                
                self._start_cli_task(
                    ['cleanup', '--days', str(days), '--json'],
                    lambda success, output, error:
                    self._on_session_operation_complete(
                        success, output, error, None,
                        _("Cleanup completed successfully"),
                        _("Cleanup failed")))
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
        """Create the shared indeterminate operation dialog."""
        progress_dialog = ProgressDialog(
            parent=self.window, title=title, status=message,
            cancellable=False)
        progress_dialog.set_deletable(False)
        progress_dialog.set_resizable(False)
        progress_dialog.set_default_size(400, 150)
        progress_dialog.operation_view.set_state('running')
        progress_dialog.message_label = progress_dialog.operation_view.status_label
        return progress_dialog

    def _on_session_creation_complete(self, success, output, error, progress_dialog):
        """Handle session creation completion."""
        # Hide loading overlay if no progress_dialog (using overlay)
        if progress_dialog is None:
            self._show_loading(False)
        else:
            progress_dialog.destroy()

        if success:
            self.refresh_session_list()
        else:
            detail = error.strip() if error else ''
            if output:
                try:
                    result = _strict_json_loads(output)
                    if isinstance(result, dict):
                        detail = result.get('message') or result.get('error') or detail
                except (TypeError, ValueError):
                    pass
            message = _("Failed to create session")
            if detail:
                message = "{}: {}".format(message, detail)
            self._show_error(message)

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
            self._loading_visible = True
            self._update_footer_sensitivity()
            if text:
                self.loading_label.set_text(text)
            # Ensure CSS class is applied every time we show the loading overlay
            self.loading_box.get_style_context().add_class('loading-overlay')
            self.loading_box.set_visible(True)
            self.loading_box.set_state('running')
        else:
            self.loading_box.set_visible(False)
            self.loading_box.set_state('idle')
            # Reset to default text
            self.loading_label.set_text(_("Loading sessions..."))
            self._loading_visible = False
            self._update_footer_sensitivity()

    def _update_footer_sensitivity(self):
        """Keep footer actions unavailable for the full loading interval."""
        if not all(hasattr(self, name) for name in (
                'create_btn', 'import_btn', 'cleanup_btn')):
            return
        available = not getattr(self, '_loading_visible', False)
        available = available and not bool(
            getattr(self, '_session_mounts', {}))
        self.create_btn.set_sensitive(
            available and self.sessions_writable and bool(self._filesystem_info))
        self.import_btn.set_sensitive(available and self.sessions_writable)
        self.cleanup_btn.set_sensitive(available and self.sessions_writable)

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

        self._start_cli_task(
            ['settings', session_id, '--shutdown', shutdown,
             '--autosave', autosave, '--json'],
            lambda success, output, error: self._on_session_operation_complete(
                success, output, error, None,
                _("Save settings updated"),
                _("Failed to update save settings")))

    def _show_resize_dialog(self, session_id):
        """Show resize dialog for a session"""
        session_info = self._sessions_by_id.get(session_id)
        if session_info is None:
            self._show_error(_("Session not found"))
            return

        try:
            session_mode = session_info.get('mode', 'unknown')
            session_encryption = session_info.get('encryption', 'none')
            if session_mode not in ['dynfilefs', 'dynblk', 'vmdk', 'raw']:
                self._show_error(_("Resize is only supported for DynFileFS, DynBlk, and Raw sessions"))
                return
            
            # Check if session is running
            is_running = session_info.get('is_running', False)
            if is_running:
                self._show_error(_("Cannot resize session while it is running. Resize operation is not allowed for the currently active session."))
                return
            
            # Get current session size in MB
            current_size_mb = 100  # Default minimum
            
            if session_mode in ('dynfilefs', 'dynblk', 'vmdk'):
                # Thin backends report physical use separately from virtual capacity.
                if 'total_size' in session_info:
                    current_size_mb = session_info['total_size'] // (1024 * 1024)
            elif session_mode == 'raw':
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
        mode_display = _mode_display_name(session_mode) if session_encryption == 'none' else '{} + LUKS2'.format(_mode_display_name(session_mode))
        info_label.set_markup(f"<b>{_('Session:')} {session_id} ({mode_display})</b>")
        content_area.pack_start(info_label, False, False, 0)
        
        # Size input
        size_label = Gtk.Label(label=_("New size (MB):"))
        content_area.pack_start(size_label, False, False, 0)
        
        size_spin = Gtk.SpinButton()
        if session_mode in ('dynblk', 'vmdk'):
            max_resize_mb = self._dynblk_size_limit(session_mode)
        elif session_mode == 'raw':
            max_resize_mb = self._fat_size_limit() or 1000000
        else:
            max_resize_mb = 1000000
        size_spin.set_range(current_size_mb, max_resize_mb)
        size_spin.set_increments(100, 1000)
        size_spin.set_value(current_size_mb)  # Set to current size
        content_area.pack_start(size_spin, False, False, 0)
        
        dialog.show_all()
        
        response = dialog.run()
        if response == Gtk.ResponseType.OK:
            new_size = int(size_spin.get_value())
            dialog.destroy()
            password_input = self._prompt_luks_passphrase() if session_encryption == 'luks' else None
            if session_encryption == 'luks' and password_input is None:
                return
            
            # Show loading overlay
            self._show_loading(True, _("Resizing session, please wait..."))
            
            args = ['resize', session_id, str(new_size), '--json']
            if password_input is not None:
                args.append('--password-stdin')
            self._start_cli_task(args, self._on_resize_complete, password_input)
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
        output_path = choose_save_file(
            self.window, _("Export Session {}").format(session_id),
            filters=((_("TAR.ZSTD archives (*.tar.zst)"), ("*.tar.zst",)),
                     (_("All files"), ("*",))),
            current_name="session_{}.tar.zst".format(session_id),
            accept_label=_("Export"))
        if output_path is None:
            return
        session_encryption = self._sessions_by_id.get(
            session_id, {}).get('encryption', 'none')
        password_input = self._prompt_luks_passphrase() if session_encryption == 'luks' else None
        if session_encryption == 'luks' and password_input is None:
            return

        self._show_loading(True, _("Exporting session, please wait..."))
        args = ['export', session_id, output_path, '--json']
        if password_input is not None:
            args.append('--password-stdin')
        self._start_cli_task(args, self._on_export_complete, password_input)

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
        archive_path = choose_open_file(
            self.window, _("Import Session"),
            filters=((_("TAR.ZSTD archives (*.tar.zst)"), ("*.tar.zst",)),
                     (_("All files"), ("*",))),
            accept_label=_("Open"))
        if archive_path is not None:
            self._show_import_options_dialog(archive_path)

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
        import_modes = [mode for mode in self._filesystem_info.get(
            'compatible_modes', ['native', 'dynfilefs', 'raw'])
                        if mode != 'squashfs']
        for mode in import_modes:
            mode_combo.append(mode, _mode_display_name(mode))
        mode_combo.set_active_id("auto")
        mode_box.pack_start(mode_label, False, False, 0)
        mode_box.pack_start(mode_combo, True, True, 0)
        content_area.pack_start(mode_box, False, False, 0)

        encryption_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        encryption_box.pack_start(Gtk.Label(label=_("Target encryption:")), False, False, 0)
        encryption_combo = Gtk.ComboBoxText()
        encryption_combo.append('none', _("None"))
        if any(self.luks_backends.values()):
            encryption_combo.append('luks', _("LUKS2"))
        encryption_combo.set_active_id('none')
        encryption_box.pack_start(encryption_combo, True, True, 0)
        content_area.pack_start(encryption_box, False, False, 0)

        compression_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        compression_box.pack_start(Gtk.Label(label=_("DynBlk compression:")), False, False, 0)
        compression_combo = Gtk.ComboBoxText()
        for codec in self.dynblk_compression_codecs:
            compression_combo.append(codec, codec)
        compression_combo.set_active_id('none')
        compression_box.pack_start(compression_combo, True, True, 0)
        content_area.pack_start(compression_box, False, False, 0)

        def on_import_mode_changed(_widget):
            mode = mode_combo.get_active_id()
            supported = (any(self.luks_backends.values()) if mode == 'auto'
                         else self.luks_backends.get(mode, False))
            encryption_box.set_sensitive(supported)
            if not supported:
                encryption_combo.set_active_id('none')
            compression_enabled = (
                mode == 'dynblk' and
                (encryption_combo.get_active_id() or 'none') == 'none')
            compression_box.set_sensitive(compression_enabled)
            if not compression_enabled:
                compression_combo.set_active_id('none')

        mode_combo.connect('changed', on_import_mode_changed)
        encryption_combo.connect('changed', on_import_mode_changed)
        on_import_mode_changed(mode_combo)

        dialog.show_all()

        response = dialog.run()
        if response == Gtk.ResponseType.OK:
            auto_convert = auto_convert_check.get_active()
            force_mode = mode_combo.get_active_id()
            if force_mode == "auto":
                force_mode = None
            force_encryption = encryption_combo.get_active_id() or 'none'
            compression = compression_combo.get_active_id() or 'none'
            dialog.destroy()
            password_input = self._prompt_luks_passphrase(confirm=True) if force_encryption == 'luks' else None
            if force_encryption == 'luks' and password_input is None:
                return

            # Show loading overlay
            self._show_loading(True, _("Importing session, please wait..."))

            args = ['import', archive_path, '--json']
            if auto_convert:
                args.append('--auto-convert')
            if force_mode:
                args.extend(['--force-mode', force_mode])
            args.extend(['--force-encryption', force_encryption])
            if compression != 'none':
                args.extend(['--compression', compression])
            if password_input is not None:
                args.append('--password-stdin')
            self._start_cli_task(args, self._on_import_complete, password_input)
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
        source_session = self._sessions_by_id.get(session_id, {})
        source_mode = source_session.get('mode', 'native')
        source_encryption = source_session.get('encryption', 'none')
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
        copy_modes = [mode for mode in self._filesystem_info.get(
            'compatible_modes', ['native', 'dynfilefs', 'raw'])
                      if mode != 'squashfs']
        for mode in copy_modes:
            mode_combo.append(mode, _mode_display_name(mode))
        mode_combo.set_active_id(source_mode if source_mode in copy_modes else copy_modes[0])
        mode_combo.set_sensitive(False)
        mode_box.pack_start(mode_label, False, False, 0)
        mode_box.pack_start(mode_combo, True, True, 0)
        content_area.pack_start(mode_box, False, False, 0)

        encryption_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        encryption_box.pack_start(Gtk.Label(label=_("Target encryption:")), False, False, 0)
        encryption_combo = Gtk.ComboBoxText()
        encryption_combo.append('none', _("None"))
        encryption_combo.append('luks', _("LUKS2"))
        encryption_combo.set_active_id(source_encryption)
        encryption_box.pack_start(encryption_combo, True, True, 0)
        content_area.pack_start(encryption_box, False, False, 0)

        compression_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        compression_box.pack_start(Gtk.Label(label=_("DynBlk compression:")), False, False, 0)
        compression_combo = Gtk.ComboBoxText()
        for codec in self.dynblk_compression_codecs:
            compression_combo.append(codec, codec)
        compression_combo.set_active_id('none')
        compression_box.pack_start(compression_combo, True, True, 0)
        content_area.pack_start(compression_box, False, False, 0)

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

        def refresh_target_controls(_widget=None):
            convert = convert_check.get_active()
            mode_combo.set_sensitive(convert)
            mode = mode_combo.get_active_id() if convert else source_mode
            size_spin.set_sensitive(convert and mode in ['dynfilefs', 'dynblk', 'vmdk', 'raw'])
            encryption_supported = self.luks_backends.get(mode, False)
            encryption_box.set_sensitive(encryption_supported)
            if not encryption_supported:
                encryption_combo.set_active_id('none')
            compression_enabled = (
                mode == 'dynblk' and
                (encryption_combo.get_active_id() or 'none') == 'none')
            compression_box.set_sensitive(compression_enabled)
            if not compression_enabled:
                compression_combo.set_active_id('none')
            if mode in ('dynblk', 'vmdk'):
                upper = self._dynblk_size_limit(mode)
            elif mode == 'raw':
                upper = self._fat_size_limit() or 1000000
            else:
                upper = 1000000
            size_spin.set_range(100, upper)

        convert_check.connect("toggled", refresh_target_controls)
        mode_combo.connect("changed", refresh_target_controls)
        encryption_combo.connect("changed", refresh_target_controls)
        refresh_target_controls()

        dialog.show_all()

        response = dialog.run()
        if response == Gtk.ResponseType.OK:
            convert = convert_check.get_active()
            target_mode = mode_combo.get_active_id() if convert else None
            target_encryption = encryption_combo.get_active_id() or 'none'
            compression = compression_combo.get_active_id() or 'none'
            size_mb = int(size_spin.get_value()) if convert and target_mode in ['dynfilefs', 'dynblk', 'vmdk', 'raw'] else None
            dialog.destroy()
            password_parts = []
            if source_encryption == 'luks':
                source_input = self._prompt_luks_passphrase()
                if source_input is None:
                    return
                password_parts.append(source_input)
            if target_encryption == 'luks':
                target_input = self._prompt_luks_passphrase(confirm=True)
                if target_input is None:
                    return
                password_parts.append(target_input)
            password_input = ''.join(password_parts) or None

            # Show loading overlay
            self._show_loading(True, _("Copying session, please wait..."))

            args = ['copy', session_id, '--json']
            if target_mode:
                args.extend(['--to-mode', target_mode])
            if target_encryption != source_encryption or target_mode:
                args.extend(['--to-encryption', target_encryption])
            if size_mb:
                args.extend(['--size', str(size_mb)])
            if compression != 'none':
                args.extend(['--compression', compression])
            if password_input is not None:
                args.append('--password-stdin')
            self._start_cli_task(args, self._on_copy_complete, password_input)
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
            current_encryption = session_info.get('encryption', 'none')

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

        current_display = (_mode_display_name(current_mode) if current_encryption == 'none' else
                           '{} + LUKS2'.format(_mode_display_name(current_mode)))
        current_label = Gtk.Label(label=_("Current mode: {}").format(current_display))
        content_area.pack_start(current_label, False, False, 0)

        # Target mode selection
        mode_label = Gtk.Label(label=_("Target mode:"))
        content_area.pack_start(mode_label, False, False, 0)

        mode_combo = Gtk.ComboBoxText()
        modes = [mode for mode in self._filesystem_info.get(
            'compatible_modes', ['native', 'dynfilefs', 'raw'])
                 if mode != 'squashfs']
        for mode in modes:
            mode_combo.append(mode, _mode_display_name(mode))
        mode_combo.set_active_id(current_mode if current_mode in modes else modes[0])
        content_area.pack_start(mode_combo, False, False, 0)

        encryption_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        encryption_box.pack_start(Gtk.Label(label=_("Target encryption:")), False, False, 0)
        encryption_combo = Gtk.ComboBoxText()
        encryption_combo.append('none', _("None"))
        encryption_combo.append('luks', _("LUKS2"))
        encryption_combo.set_active_id(current_encryption)
        encryption_box.pack_start(encryption_combo, False, False, 0)
        content_area.pack_start(encryption_box, False, False, 0)

        compression_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        compression_box.pack_start(Gtk.Label(label=_("DynBlk compression:")), False, False, 0)
        compression_combo = Gtk.ComboBoxText()
        for codec in self.dynblk_compression_codecs:
            compression_combo.append(codec, codec)
        compression_combo.set_active_id('none')
        compression_box.pack_start(compression_combo, False, False, 0)
        content_area.pack_start(compression_box, False, False, 0)

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

        def on_mode_changed(_widget=None):
            mode = mode_combo.get_active_id()
            size_spin.set_sensitive(mode in ['dynfilefs', 'dynblk', 'vmdk', 'raw'])
            encryption_supported = self.luks_backends.get(mode, False)
            encryption_box.set_sensitive(encryption_supported)
            if not encryption_supported:
                encryption_combo.set_active_id('none')
            compression_enabled = (
                mode == 'dynblk' and
                (encryption_combo.get_active_id() or 'none') == 'none')
            compression_box.set_sensitive(compression_enabled)
            if not compression_enabled:
                compression_combo.set_active_id('none')
            if mode in ('dynblk', 'vmdk'):
                upper = self._dynblk_size_limit(mode)
            elif mode == 'raw':
                upper = self._fat_size_limit() or 1000000
            else:
                upper = 1000000
            size_spin.set_range(100, upper)

        mode_combo.connect("changed", on_mode_changed)
        encryption_combo.connect("changed", on_mode_changed)
        on_mode_changed()

        dialog.show_all()

        response = dialog.run()
        if response == Gtk.ResponseType.OK:
            target_mode = mode_combo.get_active_id()
            target_encryption = encryption_combo.get_active_id() or 'none'
            compression = compression_combo.get_active_id() or 'none'
            size_mb = int(size_spin.get_value()) if target_mode in ['dynfilefs', 'dynblk', 'vmdk', 'raw'] else None
            dialog.destroy()
            password_parts = []
            if current_encryption == 'luks':
                source_input = self._prompt_luks_passphrase()
                if source_input is None:
                    return
                password_parts.append(source_input)
            if target_encryption == 'luks':
                target_input = self._prompt_luks_passphrase(confirm=True)
                if target_input is None:
                    return
                password_parts.append(target_input)
            password_input = ''.join(password_parts) or None

            # Show loading overlay
            self._show_loading(True, _("Converting session, please wait..."))

            args = ['convert', session_id, target_mode, '--json']
            args.extend(['--to-encryption', target_encryption])
            if size_mb:
                args.extend(['--size', str(size_mb)])
            if compression != 'none':
                args.extend(['--compression', compression])
            if password_input is not None:
                args.append('--password-stdin')
            self._start_cli_task(args, self._on_convert_complete, password_input)
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
