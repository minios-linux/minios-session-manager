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
import subprocess
import threading
import gettext
from datetime import datetime

gi.require_version('Gtk', '3.0')
gi.require_version('Gdk', '3.0')
from gi.repository import Gtk, GLib, Pango, Gdk

# Internationalization setup
try:
    gettext.bindtextdomain('minios-session-manager', '/usr/share/locale')
    gettext.textdomain('minios-session-manager')
    _ = gettext.gettext
except:
    _ = lambda x: x

class SessionManagerGUI:
    """GUI application for session management"""
    
    def __init__(self):
        self.cli_command = self._get_minios_session_cli_path()
        
        # Check sessions directory status
        self.sessions_status = self._check_sessions_directory_status()
        self.sessions_writable = self.sessions_status.get('writable', False)
        
        self._load_css()
        
        self.builder = Gtk.Builder()
        self.create_interface()
        self.refresh_session_list()
    
    def _load_css(self):
        """Load CSS styling for the application"""
        css_paths = [
            "/usr/share/minios-session-manager/style.css",
            os.path.join(os.path.dirname(os.path.dirname(__file__)), "share", "styles", "style.css")
        ]
        
        for css_path in css_paths:
            if os.path.exists(css_path):
                try:
                    provider = Gtk.CssProvider()
                    provider.load_from_path(css_path)
                    # Use Gdk.Screen.get_default() for GTK 3
                    screen = Gdk.Screen.get_default()
                    Gtk.StyleContext.add_provider_for_screen(
                        screen,
                        provider,
                        Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
                    )
                    break
                except Exception as e:
                    print(f"Warning: Failed to load CSS from {css_path}: {e}")
                    continue
    
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
    
    def _run_cli_command(self, args):
        """Run CLI command and return result"""
        try:
            cmd = ['pkexec', self.cli_command] + args
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)  # Increased timeout for pkexec
            return result.returncode == 0, result.stdout, result.stderr
        except subprocess.TimeoutExpired:
            return False, "", _("Command timed out - authentication may have been cancelled")
        except Exception as e:
            return False, "", str(e)
    
    def _check_sessions_directory_status(self):
        """Check sessions directory status using CLI"""
        try:
            success, output, error = self._run_cli_command(['status', '--json'])
            if success and output.strip():
                return json.loads(output.strip())
            else:
                return {
                    'success': False,
                    'found': False,
                    'writable': False,
                    'error': error or 'Unknown error'
                }
        except json.JSONDecodeError as e:
            return {
                'success': False,
                'found': False,
                'writable': False,
                'error': f'Failed to parse CLI response: {e}'
            }
        except Exception as e:
            return {
                'success': False,
                'found': False,
                'writable': False,
                'error': str(e)
            }
    
    def _build_sessions_status_info(self, main_box):
        """Build sessions directory status information panel"""
        sessions_hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        sessions_hbox.set_margin_bottom(12)
        
        if self.sessions_status.get('found', False) and self.sessions_writable:
            status_icon_name = "emblem-default"  # Green checkmark
            status_color = "#2E7D32"  # Green
            status_text = _("Sessions directory is writable")
        else:
            status_icon_name = "dialog-error"  # Error icon
            status_color = "#D32F2F"  # Red
            if self.sessions_status.get('found', False):
                status_text = _("Sessions directory is read-only")
            else:
                status_text = _("Sessions directory not found")
        
        # Status icon
        status_icon = Gtk.Image.new_from_icon_name(status_icon_name, Gtk.IconSize.MENU)
        sessions_hbox.pack_start(status_icon, False, False, 0)
        
        # Status text
        sessions_status_label = Gtk.Label()
        sessions_status_label.set_markup(f'<span color="{status_color}"><b>{status_text}</b></span>')
        sessions_status_label.set_halign(Gtk.Align.START)
        sessions_hbox.pack_start(sessions_status_label, False, False, 0)
        
        main_box.pack_start(sessions_hbox, False, False, 0)
    
    def _build_header_bar(self):
        """Build the header bar"""
        header = Gtk.HeaderBar(show_close_button=True)
        header.props.title = _("MiniOS Session Manager")
        self.window.set_titlebar(header)
    
    def create_interface(self):
        """Create the main interface"""
        
        self.window = Gtk.Window()
        self.window.set_icon_name("preferences-desktop-personal")  # Personal desktop preferences icon
        self.window.set_default_size(600, 500)
        self.window.set_position(Gtk.WindowPosition.CENTER)
        self.window.connect("destroy", Gtk.main_quit)
        
        # Build header bar
        self._build_header_bar()
        
        
        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        main_box.set_margin_start(10)
        main_box.set_margin_end(10)
        main_box.set_margin_top(10)
        main_box.set_margin_bottom(10)
        self.window.add(main_box)
        
        
        # Sessions directory status
        self._build_sessions_status_info(main_box)
        
        # Sessions list
        self.sessions_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.sessions_list.connect("row-selected", self._on_session_selected)
        self.sessions_list.connect("button-press-event", self._on_list_button_press)

        # ScrolledWindow setup
        scrolled = Gtk.ScrolledWindow()
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
        self.loading_box.set_visible(False)
        
        main_box.pack_start(overlay, True, True, 0)
        
        # Create context menu
        self._create_context_menu()
        
        # Initialize selection
        self.selected_session_id = None
        
        # Toolbar buttons
        toolbar_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=15)
        toolbar_box.set_halign(Gtk.Align.CENTER)
        toolbar_box.set_margin_top(15)
        main_box.pack_start(toolbar_box, False, False, 0)
        
        # Create button
        create_btn = Gtk.Button()
        create_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        create_icon = Gtk.Image.new_from_icon_name("document-new", Gtk.IconSize.DND)
        create_label = Gtk.Label(label=_("Create"))
        create_box.pack_start(create_icon, False, False, 0)
        create_box.pack_start(create_label, False, False, 0)
        create_btn.add(create_box)
        create_btn.connect("clicked", self.on_create_clicked)
        create_btn.get_style_context().add_class('suggested-action')
        create_btn.get_style_context().add_class('large-button')
        create_btn.get_style_context().add_class('create-button')
        create_btn.set_size_request(140, -1)
        # Disable create button if sessions directory is not writable
        create_btn.set_sensitive(self.sessions_writable)
        toolbar_box.pack_start(create_btn, False, False, 0)
        
        self.create_btn = create_btn  # Store reference for later use
        
        
        # Cleanup button
        cleanup_btn = Gtk.Button()
        cleanup_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        cleanup_icon = Gtk.Image.new_from_icon_name("user-trash", Gtk.IconSize.DND)
        cleanup_label = Gtk.Label(label=_("Cleanup"))
        cleanup_box.pack_start(cleanup_icon, False, False, 0)
        cleanup_box.pack_start(cleanup_label, False, False, 0)
        cleanup_btn.add(cleanup_box)
        cleanup_btn.connect("clicked", self.on_cleanup_clicked)
        cleanup_btn.get_style_context().add_class('large-button')
        cleanup_btn.get_style_context().add_class('cleanup-button')
        cleanup_btn.set_size_request(140, -1)
        # Disable cleanup button if sessions directory is not writable
        cleanup_btn.set_sensitive(self.sessions_writable)
        toolbar_box.pack_start(cleanup_btn, False, False, 0)
        
        self.cleanup_btn = cleanup_btn  # Store reference for later use
    
    def refresh_session_list(self):
        """Refresh the session list from CLI"""
        def fetch_data():
            """Fetch data in background thread"""
            try:
                # Get session list and active/running sessions separately for accurate status
                list_success, list_output, list_error = self._run_cli_command(['list', '--json'])
                active_success, active_output, active_error = self._run_cli_command(['active', '--json'])
                running_success, running_output, running_error = self._run_cli_command(['running', '--json'])
                
                # Parse active and running session IDs
                active_session_id = None
                running_session_id = None
                
                if active_success and active_output.strip():
                    try:
                        active_data = json.loads(active_output.strip())
                        active_session_id = active_data.get('id')
                    except:
                        pass
                
                if running_success and running_output.strip():
                    try:
                        running_data = json.loads(running_output.strip())
                        running_session_id = running_data.get('id')
                    except:
                        pass
                
                # Return results to main thread
                GLib.idle_add(self._process_session_data, list_success, list_output, list_error, active_session_id, running_session_id)
            except Exception as e:
                GLib.idle_add(self._show_error, _("Error fetching session data: {}").format(str(e)))
                GLib.idle_add(self._show_loading, False)
        
        # Show loading indicator
        self._show_loading(True)
        
        # Run fetch in thread to avoid blocking UI
        thread = threading.Thread(target=fetch_data)
        thread.daemon = True
        thread.start()
    
    def _process_session_data(self, list_success, list_output, list_error, active_session_id, running_session_id):
        """Process session data in main thread"""
        try:
            # Clear existing session rows
            for row in self.sessions_list.get_children():
                self.sessions_list.remove(row)
            
            # Check for list errors
            if not list_success:
                self._show_error(_("Failed to get session list: {}").format(list_error))
                return
            
            # Parse JSON output
            if list_output.strip().startswith('['):
                # JSON format - parse sessions directly
                try:
                    sessions = json.loads(list_output.strip())
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
                        
                        modified_str = session.get('modified', 'unknown')
                        
                        # Format modified date
                        try:
                            if modified_str != 'unknown':
                                from datetime import datetime
                                modified_dt = datetime.fromisoformat(modified_str.replace('Z', '+00:00'))
                                modified = modified_dt.strftime('%Y-%m-%d %H:%M:%S')
                            else:
                                modified = 'unknown'
                        except:
                            modified = modified_str
                        
                        # Create session row
                        self._create_session_row(session_id, is_active, is_running, mode, version, edition, union, size, modified)
                
                except json.JSONDecodeError as e:
                    self._show_error(_("Failed to parse session list JSON: {}").format(str(e)))
                    return
            else:
                # Fallback to text parsing (backward compatibility)
                lines = list_output.split('\n')
                sessions_found = False
                
                for i, line in enumerate(lines):
                    if line.startswith('Session #'):
                        sessions_found = True
                        # Extract session info
                        session_id = line.split('#')[1].split()[0]
                        # Determine status from separate commands
                        is_active = (session_id == active_session_id)
                        is_running = (session_id == running_session_id)
                        
                        # Look for following lines with details
                        mode = version = edition = union = size = modified = "Unknown"
                        total_size = None
                        
                        for j in range(i+1, min(i+7, len(lines))):  # Increased range for Total Size line
                            detail_line = lines[j].strip()
                            if detail_line.startswith('Mode:'):
                                mode = detail_line.split(':', 1)[1].strip()
                            elif detail_line.startswith('Version:'):
                                version_info = detail_line.split(':', 1)[1].strip()
                                parts = version_info.split('/')
                                if len(parts) >= 2:
                                    version = parts[0].strip()
                                    edition = parts[1].strip()
                                    if len(parts) >= 3:
                                        union = parts[2].strip()
                                else:
                                    version = version_info
                            elif detail_line.startswith('Size:'):
                                size = detail_line.split(':', 1)[1].strip()
                            elif detail_line.startswith('Total Size:'):
                                total_size = detail_line.split(':', 1)[1].strip()
                            elif detail_line.startswith('Last Modified:'):
                                modified = detail_line.split(':', 1)[1].strip()
                        
                        # For dynfilefs, combine size and total_size
                        if mode == 'dynfilefs' and total_size:
                            size = f"{size} / {total_size}"
                        
                        # Create session row
                        self._create_session_row(session_id, is_active, is_running, mode, version, edition, union, size, modified)
            
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
            row.get_style_context().add_class('session-status-active')  # Use 'active' style for current
        else:
            row.get_style_context().add_class('session-status-available')
        
        main_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=15)
        main_box.get_style_context().add_class('session-item')
        
        # Session icon
        icon_name = 'media-floppy'
        img = Gtk.Image.new_from_icon_name(icon_name, Gtk.IconSize.DND)
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
        if is_active:
            status_text = _('ACTIVE')
            status_label.get_style_context().add_class('active-session-badge')
        else:
            status_text = _('AVAILABLE')
            status_label.get_style_context().add_class('available-session-badge')
        
        status_label.set_markup(f'<span size="small" weight="bold">{GLib.markup_escape_text(status_text)}</span>')
        status_label.set_halign(Gtk.Align.CENTER)
        status_box.pack_start(status_label, False, False, 0)
        
        # Running badge (secondary) - in same line
        if is_running:
            running_label = Gtk.Label()
            running_text = _('RUNNING')
            running_label.get_style_context().add_class('running-session-badge')
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
    
    def _create_context_menu(self):
        """Create context menu for session items"""
        self.context_menu = Gtk.Menu()
        self.context_menu.get_style_context().add_class('session-context-menu')
        
        # Activate menu item
        activate_item = Gtk.MenuItem(label=_("Activate Session"))
        activate_item.get_style_context().add_class('context-menu-activate')
        activate_item.connect("activate", self._on_context_activate)
        self.context_menu.append(activate_item)
        
        # Resize menu item
        resize_item = Gtk.MenuItem(label=_("Resize Session"))
        resize_item.get_style_context().add_class('context-menu-resize')
        resize_item.connect("activate", self._on_context_resize)
        self.context_menu.append(resize_item)
        
        # Separator
        separator = Gtk.SeparatorMenuItem()
        self.context_menu.append(separator)
        
        # Delete menu item
        delete_item = Gtk.MenuItem(label=_("Delete Session"))
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
                
                # Update menu items based on session status
                activate_item = self.context_menu.get_children()[0]
                resize_item = self.context_menu.get_children()[1]
                delete_item = self.context_menu.get_children()[3]
                
                # Check session mode for resize availability
                session_mode = getattr(row, 'mode', 'unknown')
                resize_available = session_mode in ['dynfilefs', 'raw']
                
                # Check if sessions directory is writable for create/delete operations
                if not self.sessions_writable:
                    activate_item.set_sensitive(False)
                    resize_item.set_sensitive(False)
                    delete_item.set_sensitive(False)
                elif hasattr(row, 'is_active') and row.is_active:
                    # Disable activate if already active (regardless of running status)
                    activate_item.set_sensitive(False)
                    # For active sessions, check if also running to determine resize availability
                    if hasattr(row, 'is_running') and row.is_running:
                        resize_item.set_sensitive(False)  # Can't resize running session
                    else:
                        resize_item.set_sensitive(resize_available)  # Can resize active session
                    delete_item.set_sensitive(False)  # Can't delete active session
                elif hasattr(row, 'is_running') and row.is_running:
                    # Can activate running session (not active), but can't delete or resize it
                    activate_item.set_sensitive(True)
                    resize_item.set_sensitive(False)  # Can't resize running session
                    delete_item.set_sensitive(False)  # Can't delete running session
                else:
                    activate_item.set_sensitive(True)
                    resize_item.set_sensitive(resize_available)
                    delete_item.set_sensitive(True)
                
                # Show context menu
                self.context_menu.popup_at_pointer(event)
                return True
        return False
    
    def _on_context_activate(self, menu_item):
        """Handle activate from context menu"""
        if self.selected_session_id:
            self.on_activate_clicked(None)
    
    def _on_context_delete(self, menu_item):
        """Handle delete from context menu"""
        if self.selected_session_id:
            self.on_delete_clicked(None)
    
    def _on_context_resize(self, menu_item):
        """Handle resize from context menu"""
        if self.selected_session_id:
            self._show_resize_dialog(self.selected_session_id)
    
    
    def on_create_clicked(self, button):
        """Handle create session button click"""
        # Check if sessions directory is writable
        if not self.sessions_writable:
            self._show_error(_("Sessions directory is not writable. Cannot create new sessions."))
            return
        
        # First, get filesystem information using JSON output
        fs_success, fs_output, fs_error = self._run_cli_command(['info', '--json'])
        compatible_modes = ['native', 'dynfilefs', 'raw']  # Default
        filesystem_type = "unknown"
        limitations = {}
        
        if fs_success:
            try:
                import json
                fs_info = json.loads(fs_output)
                filesystem_type = fs_info.get('filesystem', {}).get('type', 'unknown')
                compatible_modes = fs_info.get('compatible_modes', ['native', 'dynfilefs', 'raw'])
                limitations = fs_info.get('limitations', {})
            except (json.JSONDecodeError, KeyError) as e:
                print(f"Error parsing filesystem info: {e}")
                # Fallback: determine compatibility based on filesystem type
                if filesystem_type in ['ext2', 'ext3', 'ext4', 'btrfs', 'xfs', 'f2fs', 'reiserfs']:
                    # POSIX-compatible filesystems support all modes
                    compatible_modes = ['native', 'dynfilefs', 'raw']
                else:
                    # Non-POSIX filesystems only support container modes
                    compatible_modes = ['dynfilefs', 'raw']
        
        # Create session mode selection dialog
        dialog = Gtk.Dialog(
            title=_("Create New Session"),
            parent=self.window
        )
        dialog.add_buttons(
            Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
            Gtk.STOCK_OK, Gtk.ResponseType.OK
        )
        
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
        
        # Size selection for dynfilefs and raw modes
        size_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        content_area.pack_start(size_box, False, False, 0)
        
        size_label = Gtk.Label(label=_("Size (MB):"))
        size_box.pack_start(size_label, False, False, 0)
        
        adjustment = Gtk.Adjustment(value=4000, lower=100, upper=50000, step_increment=100)
        size_spinbutton = Gtk.SpinButton()
        size_spinbutton.set_adjustment(adjustment)
        size_spinbutton.set_value(4000)
        size_box.pack_start(size_spinbutton, False, False, 0)
        
        size_info_label = Gtk.Label(label=_("(Only used for DynFileFS and Raw modes)"))
        size_info_label.set_sensitive(False)
        size_box.pack_start(size_info_label, False, False, 0)
        
        # Enable/disable size controls based on mode selection
        def on_mode_changed(radio):
            # Check which radio buttons exist and are active
            is_dynfilefs_active = 'dynfilefs' in radio_buttons and radio_buttons['dynfilefs'].get_active()
            is_raw_active = 'raw' in radio_buttons and radio_buttons['raw'].get_active()
            is_sized_mode = is_dynfilefs_active or is_raw_active
            
            size_label.set_sensitive(is_sized_mode)
            size_spinbutton.set_sensitive(is_sized_mode)
            size_info_label.set_sensitive(not is_sized_mode)
            
            # Check for size limitations on FAT32 for raw images
            if is_raw_active and 'max_file_size' in limitations:
                max_size = limitations['max_file_size']
                current_size = int(size_spinbutton.get_value())
                if current_size > max_size:
                    size_spinbutton.set_value(max_size)
                adjustment.set_upper(max_size)
                size_info_label.set_text(_("(Maximum {}MB on FAT32)").format(max_size))
                size_info_label.set_sensitive(True)
            else:
                adjustment.set_upper(50000)
                size_info_label.set_text(_("(Only used for DynFileFS and Raw modes)"))
        
        # Connect signals only for existing radio buttons
        for mode, radio in radio_buttons.items():
            radio.connect("toggled", on_mode_changed)
        
        # Initialize sensitivity
        on_mode_changed(None)
        
        dialog.show_all()
        
        # Style the dialog buttons to remove white colors
        cancel_button = dialog.get_widget_for_response(Gtk.ResponseType.CANCEL)
        ok_button = dialog.get_widget_for_response(Gtk.ResponseType.OK)
        if cancel_button:
            cancel_button.get_style_context().add_class('dialog-neutral-button')
        if ok_button:
            ok_button.get_style_context().add_class('dialog-neutral-button')
        
        response = dialog.run()
        
        if response == Gtk.ResponseType.OK:
            # Determine selected mode from radio buttons
            mode = "native"  # default
            for mode_name, radio in radio_buttons.items():
                if radio.get_active():
                    mode = mode_name
                    break
            
            # Get size if needed
            size_mb = int(size_spinbutton.get_value())
            
            dialog.destroy()
            
            # Show loading overlay
            self._show_loading(True, _("Creating new session, please wait..."))
            
            # Create session in background thread
            def create_session_bg():
                try:
                    if mode in ["dynfilefs", "raw"]:
                        success, output, error = self._run_cli_command(['create', mode, str(size_mb), '--json'])
                    else:
                        success, output, error = self._run_cli_command(['create', mode, '--json'])
                    
                    # Update UI in main thread
                    GLib.idle_add(self._on_session_creation_complete, success, output, error, None)
                except Exception as e:
                    GLib.idle_add(self._on_session_creation_complete, False, "", str(e), None)
            
            thread = threading.Thread(target=create_session_bg)
            thread.daemon = True
            thread.start()
        else:
            dialog.destroy()
    
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
        # Check if sessions directory is writable
        if not self.sessions_writable:
            self._show_error(_("Sessions directory is not writable. Cannot delete sessions."))
            return
        
        session_id = self.selected_session_id
        
        # Confirm deletion
        dialog = Gtk.MessageDialog(
            parent=self.window,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.YES_NO,
            text=_("Delete this session?")
        )
        dialog.format_secondary_text(
            _("You're about to permanently delete session #{}.\n\nThis action cannot be undone and all data in this session will be lost forever.\n\nAre you absolutely sure you want to proceed?").format(session_id)
        )
        
        # Style the dialog buttons
        dialog.get_style_context().add_class('friendly-dialog')
        yes_button = dialog.get_widget_for_response(Gtk.ResponseType.YES)
        no_button = dialog.get_widget_for_response(Gtk.ResponseType.NO)
        yes_button.set_label(_("Yes, delete it"))
        no_button.set_label(_("Keep it safe"))
        yes_button.get_style_context().add_class('destructive-action')
        no_button.get_style_context().add_class('suggested-action')
        
        response = dialog.run()
        dialog.destroy()
        
        if response == Gtk.ResponseType.YES:
            # Show loading overlay
            self._show_loading(True, _("Deleting session, please wait..."))
            
            # Delete session in background thread
            def delete_session_bg():
                try:
                    success, output, error = self._run_cli_command(['delete', session_id, '--json'])
                    GLib.idle_add(self._on_session_operation_complete, success, output, error, None, _("Session deleted successfully"), _("Failed to delete session"))
                except Exception as e:
                    GLib.idle_add(self._on_session_operation_complete, False, "", str(e), None, "", _("Failed to delete session"))
            
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
        
        # Style the dialog buttons to remove white colors
        cancel_button = dialog.get_widget_for_response(Gtk.ResponseType.CANCEL)
        ok_button = dialog.get_widget_for_response(Gtk.ResponseType.OK)
        if cancel_button:
            cancel_button.get_style_context().add_class('dialog-neutral-button')
        if ok_button:
            ok_button.get_style_context().add_class('dialog-neutral-button')
        
        response = dialog.run()
        
        if response == Gtk.ResponseType.OK:
            days = int(spinbutton.get_value())
            dialog.destroy()
            
            # Confirm cleanup
            confirm_dialog = Gtk.MessageDialog(
                parent=self.window,
                message_type=Gtk.MessageType.QUESTION,
                buttons=Gtk.ButtonsType.YES_NO,
                text=_("Confirm Cleanup")
            )
            confirm_dialog.format_secondary_text(
                _("This will delete all sessions older than {} days.\n\nContinue?").format(days)
            )
            
            confirm_response = confirm_dialog.run()
            confirm_dialog.destroy()
            
            if confirm_response == Gtk.ResponseType.YES:
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
        """Show error dialog with friendly styling"""
        dialog = Gtk.MessageDialog(
            parent=self.window,
            message_type=Gtk.MessageType.ERROR,
            buttons=Gtk.ButtonsType.OK,
            text=_("Oops! Something went wrong")
        )
        dialog.format_secondary_text(str(message))
        
        # Style the dialog
        dialog.get_style_context().add_class('friendly-dialog')
        ok_button = dialog.get_widget_for_response(Gtk.ResponseType.OK)
        ok_button.set_label(_("Got it"))
        ok_button.get_style_context().add_class('suggested-action')
        
        dialog.run()
        dialog.destroy()
    
    
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
            error_message = f"{error_prefix}: {error}" if error else error_prefix
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
    
    def _show_resize_dialog(self, session_id):
        """Show resize dialog for a session"""
        # Get session information
        sessions = self._run_cli_command(['list', '--json'])[1]
        if not sessions:
            self._show_error(_("Failed to get session information"))
            return
        
        try:
            sessions_data = json.loads(sessions)
            session_info = None
            for session in sessions_data:
                if session['id'] == session_id:
                    session_info = session
                    break
            
            if not session_info:
                self._show_error(_("Session not found"))
                return
            
            session_mode = session_info.get('mode', 'unknown')
            if session_mode not in ['dynfilefs', 'raw']:
                self._show_error(_("Resize is only supported for dynfilefs and raw mode sessions"))
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
        size_spin.set_range(current_size_mb, 100000)  # Current size to 100GB
        size_spin.set_increments(100, 1000)
        size_spin.set_value(current_size_mb)  # Set to current size
        content_area.pack_start(size_spin, False, False, 0)
        
        dialog.show_all()
        
        response = dialog.run()
        if response == Gtk.ResponseType.OK:
            new_size = int(size_spin.get_value())
            dialog.destroy()
            
            # Show loading overlay
            self._show_loading(True, _("Resizing session, please wait..."))
            
            # Perform resize in background
            def resize_session_bg():
                try:
                    success, output, error = self._run_cli_command(['resize', session_id, str(new_size), '--json'])
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
                    result = json.loads(output)
                    message = result.get('message', error or _('Resize failed'))
                else:
                    message = error or _('Resize failed')
            except json.JSONDecodeError:
                message = error or _('Resize failed')
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