#!/usr/bin/env python3
"""
MiniOS Session Manager GUI

Graphical interface for managing MiniOS persistent sessions.
This GUI application calls the CLI utility (minios-session-cli) to perform actual operations.
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
from gi.repository import Gtk, GLib

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
        self.cli_command = self._find_cli_command()
        if not self.cli_command:
            self._show_error(_("Error: minios-session-cli not found in PATH"))
            sys.exit(1)
        
        self.builder = Gtk.Builder()
        self.create_interface()
        self.refresh_session_list()
    
    def _find_cli_command(self):
        """Find the CLI command executable"""
        # Try different possible locations
        possible_paths = [
            'minios-session-cli',  # In PATH
            '/usr/bin/minios-session-cli',  # System install
            os.path.join(os.path.dirname(os.path.dirname(__file__)), 'bin', 'minios-session-cli')  # Source
        ]
        
        for path in possible_paths:
            try:
                result = subprocess.run([path, '--help'], capture_output=True, timeout=5)
                if result.returncode == 0:
                    return path
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
                continue
        
        return None
    
    def _run_cli_command(self, args):
        """Run CLI command and return result"""
        try:
            cmd = [self.cli_command] + args
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)  # Increased timeout for pkexec
            return result.returncode == 0, result.stdout, result.stderr
        except subprocess.TimeoutExpired:
            return False, "", _("Command timed out - authentication may have been cancelled")
        except Exception as e:
            return False, "", str(e)
    
    def create_interface(self):
        """Create the main interface"""
        # Main window
        self.window = Gtk.Window()
        self.window.set_title(_("MiniOS Session Manager"))
        self.window.set_default_size(800, 600)
        self.window.set_position(Gtk.WindowPosition.CENTER)
        self.window.connect("destroy", Gtk.main_quit)
        
        # Main container
        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        main_box.set_margin_left(10)
        main_box.set_margin_right(10)
        main_box.set_margin_top(10)
        main_box.set_margin_bottom(10)
        self.window.add(main_box)
        
        # Header
        header_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        main_box.pack_start(header_box, False, False, 0)
        
        title_label = Gtk.Label()
        title_label.set_markup(f"<b>{_('MiniOS Session Manager')}</b>")
        header_box.pack_start(title_label, False, False, 0)
        
        # Current session info
        self.current_session_label = Gtk.Label()
        self.current_session_label.set_halign(Gtk.Align.START)
        main_box.pack_start(self.current_session_label, False, False, 0)
        
        # Session list
        list_frame = Gtk.Frame()
        list_frame.set_label(_("Available Sessions"))
        main_box.pack_start(list_frame, True, True, 0)
        
        # Create scrollable list
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        list_frame.add(scrolled)
        
        # TreeView for sessions
        self.session_store = Gtk.ListStore(str, str, str, str, str, str, str)  # ID, Status, Mode, Version, Edition, Size, Modified
        self.session_tree = Gtk.TreeView(model=self.session_store)
        scrolled.add(self.session_tree)
        
        # Columns
        columns = [
            (_("ID"), 0),
            (_("Status"), 1), 
            (_("Mode"), 2),
            (_("Version"), 3),
            (_("Edition"), 4),
            (_("Size"), 5),
            (_("Modified"), 6)
        ]
        
        for title, col_id in columns:
            renderer = Gtk.CellRendererText()
            column = Gtk.TreeViewColumn(title, renderer, text=col_id)
            column.set_resizable(True)
            column.set_sort_column_id(col_id)
            self.session_tree.append_column(column)
        
        # First row of buttons
        button_row1 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        main_box.pack_start(button_row1, False, False, 0)
        
        # Refresh button
        refresh_btn = Gtk.Button.new_with_label(_("Refresh"))
        refresh_btn.connect("clicked", self.on_refresh_clicked)
        button_row1.pack_start(refresh_btn, False, False, 0)
        
        # Create button
        create_btn = Gtk.Button.new_with_label(_("Create Session"))
        create_btn.connect("clicked", self.on_create_clicked)
        button_row1.pack_start(create_btn, False, False, 0)
        
        # Activate button
        activate_btn = Gtk.Button.new_with_label(_("Activate Session"))
        activate_btn.connect("clicked", self.on_activate_clicked)
        button_row1.pack_start(activate_btn, False, False, 0)
        
        # Delete button
        delete_btn = Gtk.Button.new_with_label(_("Delete Session"))
        delete_btn.connect("clicked", self.on_delete_clicked)
        button_row1.pack_start(delete_btn, False, False, 0)
        
        # Second row of buttons
        button_row2 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        main_box.pack_start(button_row2, False, False, 0)
        
        # Cleanup button
        cleanup_btn = Gtk.Button.new_with_label(_("Cleanup Old Sessions"))
        cleanup_btn.connect("clicked", self.on_cleanup_clicked)
        button_row2.pack_start(cleanup_btn, False, False, 0)
        
        # About button
        about_btn = Gtk.Button.new_with_label(_("About"))
        about_btn.connect("clicked", self.on_about_clicked)
        button_row2.pack_end(about_btn, False, False, 0)
    
    def refresh_session_list(self):
        """Refresh the session list from CLI"""
        def update_ui():
            # Get current session info
            success, output, error = self._run_cli_command(['current'])
            if success and output.strip():
                self.current_session_label.set_text(output.strip())
            else:
                self.current_session_label.set_text(_("No current session found"))
            
            # Clear existing data
            self.session_store.clear()
            
            # Get session list
            success, output, error = self._run_cli_command(['list'])
            if not success:
                self._show_error(_("Failed to get session list: {}").format(error))
                return
            
            # Parse the output - this is a bit crude but works
            lines = output.split('\n')
            current_session = None
            
            for i, line in enumerate(lines):
                if line.startswith('Session #'):
                    # Extract session info
                    session_id = line.split('#')[1].split()[0]
                    is_current = '(CURRENT)' in line
                    status = _("CURRENT") if is_current else _("Available")
                    
                    # Look for following lines with details
                    mode = version = edition = size = modified = "Unknown"
                    
                    for j in range(i+1, min(i+6, len(lines))):
                        detail_line = lines[j].strip()
                        if detail_line.startswith('Mode:'):
                            mode = detail_line.split(':', 1)[1].strip()
                        elif detail_line.startswith('Version:'):
                            version_info = detail_line.split(':', 1)[1].strip()
                            if '/' in version_info:
                                version, edition = version_info.split('/', 1)
                                version = version.strip()
                                edition = edition.strip()
                            else:
                                version = version_info
                        elif detail_line.startswith('Size:'):
                            size = detail_line.split(':', 1)[1].strip()
                        elif detail_line.startswith('Last Modified:'):
                            modified = detail_line.split(':', 1)[1].strip()
                    
                    self.session_store.append([session_id, status, mode, version, edition, size, modified])
        
        # Run in thread to avoid blocking UI
        thread = threading.Thread(target=update_ui)
        thread.daemon = True
        thread.start()
    
    def on_refresh_clicked(self, button):
        """Handle refresh button click"""
        self.refresh_session_list()
    
    def on_create_clicked(self, button):
        """Handle create session button click"""
        # First, get filesystem information
        fs_success, fs_output, fs_error = self._run_cli_command(['info'])
        compatible_modes = ['native', 'dynfilefs', 'raw']  # Default
        filesystem_type = "unknown"
        limitations = {}
        
        if fs_success:
            # Parse filesystem info from CLI output
            # This is a simplified approach - in a real implementation you might want
            # to add a --json flag to the info command for easier parsing
            lines = fs_output.split('\n')
            for line in lines:
                if 'Filesystem Type:' in line:
                    filesystem_type = line.split(':')[1].strip()
                elif line.strip().startswith('✓'):
                    # Compatible modes are marked with ✓
                    pass
        
        # Get compatible modes by calling CLI with a different approach
        # For now, we'll determine compatibility based on filesystem type
        if 'vfat' in filesystem_type.lower() or 'fat32' in filesystem_type.lower():
            compatible_modes = ['dynfilefs', 'raw']
            limitations['max_file_size'] = 4096  # 4GB in MB
        elif 'ntfs' in filesystem_type.lower():
            compatible_modes = ['dynfilefs', 'raw']
        elif filesystem_type in ['ext2', 'ext3', 'ext4', 'btrfs', 'xfs']:
            compatible_modes = ['native', 'dynfilefs', 'raw']
        
        # Create session mode selection dialog
        dialog = Gtk.Dialog(
            title=_("Create New Session"),
            parent=self.window,
            flags=0
        )
        dialog.add_buttons(
            Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
            Gtk.STOCK_OK, Gtk.ResponseType.OK
        )
        
        content_area = dialog.get_content_area()
        content_area.set_spacing(10)
        content_area.set_margin_left(10)
        content_area.set_margin_right(10)
        content_area.set_margin_top(10)
        content_area.set_margin_bottom(10)
        
        # Show filesystem info
        fs_info_label = Gtk.Label()
        fs_info_label.set_markup(f"<b>{_('Detected filesystem:')} {filesystem_type}</b>")
        content_area.pack_start(fs_info_label, False, False, 0)
        
        label = Gtk.Label(_("Select session mode:"))
        content_area.pack_start(label, False, False, 0)
        
        # Radio buttons for mode selection (only for compatible modes)
        radio_buttons = {}
        first_radio = None
        
        if 'native' in compatible_modes:
            native_radio = Gtk.RadioButton.new_with_label_from_widget(None, _("Native Mode"))
            native_radio.set_tooltip_text(_("Direct storage on POSIX filesystems (ext4, btrfs, etc.)"))
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
            dynfilefs_radio.set_tooltip_text(_("Dynamic files for FAT32, NTFS filesystems"))
            content_area.pack_start(dynfilefs_radio, False, False, 0)
            radio_buttons['dynfilefs'] = dynfilefs_radio
            if first_radio is None:
                first_radio = dynfilefs_radio
        
        if 'raw' in compatible_modes:
            base_radio = first_radio if first_radio else None
            raw_radio = Gtk.RadioButton.new_with_label_from_widget(base_radio, _("Raw Mode"))
            raw_radio.set_tooltip_text(_("Fixed-size image files for any filesystem"))
            content_area.pack_start(raw_radio, False, False, 0)
            radio_buttons['raw'] = raw_radio
            if first_radio is None:
                first_radio = raw_radio
        
        # Size selection for dynfilefs and raw modes
        size_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        content_area.pack_start(size_box, False, False, 0)
        
        size_label = Gtk.Label(_("Size (MB):"))
        size_box.pack_start(size_label, False, False, 0)
        
        adjustment = Gtk.Adjustment(value=4000, lower=100, upper=50000, step_increment=100)
        size_spinbutton = Gtk.SpinButton()
        size_spinbutton.set_adjustment(adjustment)
        size_spinbutton.set_value(4000)
        size_box.pack_start(size_spinbutton, False, False, 0)
        
        size_info_label = Gtk.Label(_("(Only used for DynFileFS and Raw modes)"))
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
            
            # Create session with appropriate parameters
            if mode in ["dynfilefs", "raw"]:
                success, output, error = self._run_cli_command(['create', '--mode', mode, '--size', str(size_mb)])
            else:
                success, output, error = self._run_cli_command(['create', '--mode', mode])
            
            if success:
                self._show_info(output.strip())
                self.refresh_session_list()
            else:
                self._show_error(_("Failed to create session: {}").format(error))
        else:
            dialog.destroy()
    
    def on_activate_clicked(self, button):
        """Handle activate session button click"""
        selection = self.session_tree.get_selection()
        model, tree_iter = selection.get_selected()
        
        if tree_iter is None:
            self._show_info(_("Please select a session to activate"))
            return
        
        session_id = model[tree_iter][0]
        session_status = model[tree_iter][1]
        
        # Check if already active
        if session_status == _("CURRENT"):
            self._show_info(_("Session #{} is already active").format(session_id))
            return
        
        # Confirm activation
        dialog = Gtk.MessageDialog(
            parent=self.window,
            flags=0,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.YES_NO,
            text=_("Confirm Activation")
        )
        dialog.format_secondary_text(
            _("Are you sure you want to activate session #{}?\n\nThis will take effect on the next boot.").format(session_id)
        )
        
        response = dialog.run()
        dialog.destroy()
        
        if response == Gtk.ResponseType.YES:
            # Activate session
            success, output, error = self._run_cli_command(['activate', session_id])
            if success:
                self._show_info(output.strip())
                self.refresh_session_list()
            else:
                self._show_error(_("Failed to activate session: {}").format(error))
    
    def on_delete_clicked(self, button):
        """Handle delete button click"""
        selection = self.session_tree.get_selection()
        model, tree_iter = selection.get_selected()
        
        if tree_iter is None:
            self._show_info(_("Please select a session to delete"))
            return
        
        session_id = model[tree_iter][0]
        session_status = model[tree_iter][1]
        
        # Prevent deleting current session
        if session_status == _("CURRENT"):
            self._show_error(_("Cannot delete the currently active session"))
            return
        
        # Confirm deletion
        dialog = Gtk.MessageDialog(
            parent=self.window,
            flags=0,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.YES_NO,
            text=_("Confirm Deletion")
        )
        dialog.format_secondary_text(
            _("Are you sure you want to delete session #{}?\n\nThis action cannot be undone.").format(session_id)
        )
        
        response = dialog.run()
        dialog.destroy()
        
        if response == Gtk.ResponseType.YES:
            # Delete session
            success, output, error = self._run_cli_command(['delete', session_id])
            if success:
                self._show_info(output.strip())
                self.refresh_session_list()
            else:
                self._show_error(_("Failed to delete session: {}").format(error))
    
    def on_cleanup_clicked(self, button):
        """Handle cleanup button click"""
        # Ask for days threshold
        dialog = Gtk.Dialog(
            title=_("Cleanup Old Sessions"),
            parent=self.window,
            flags=0
        )
        dialog.add_buttons(
            Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
            Gtk.STOCK_OK, Gtk.ResponseType.OK
        )
        
        content_area = dialog.get_content_area()
        content_area.set_spacing(10)
        content_area.set_margin_left(10)
        content_area.set_margin_right(10)
        content_area.set_margin_top(10)
        content_area.set_margin_bottom(10)
        
        label = Gtk.Label(_("Delete sessions older than how many days?"))
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
            confirm_dialog = Gtk.MessageDialog(
                parent=self.window,
                flags=0,
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
                # Run cleanup
                success, output, error = self._run_cli_command(['cleanup', '--days', str(days)])
                if success:
                    self._show_info(output.strip())
                    self.refresh_session_list()
                else:
                    self._show_error(_("Cleanup failed: {}").format(error))
        else:
            dialog.destroy()
    
    def on_about_clicked(self, button):
        """Show about dialog"""
        about = Gtk.AboutDialog()
        about.set_transient_for(self.window)
        about.set_program_name(_("MiniOS Session Manager"))
        about.set_version("1.0.0")
        about.set_copyright("Copyright © 2025 MiniOS Team")
        about.set_comments(_("A utility for managing MiniOS persistent sessions"))
        about.set_website("https://minios.dev")
        about.set_website_label("minios.dev")
        about.set_license_type(Gtk.License.GPL_3_0)
        about.set_authors(["MiniOS Team <team@minios.dev>"])
        
        about.run()
        about.destroy()
    
    def _show_error(self, message):
        """Show error dialog"""
        dialog = Gtk.MessageDialog(
            parent=self.window,
            flags=0,
            message_type=Gtk.MessageType.ERROR,
            buttons=Gtk.ButtonsType.OK,
            text=_("Error")
        )
        dialog.format_secondary_text(str(message))
        dialog.run()
        dialog.destroy()
    
    def _show_info(self, message):
        """Show info dialog"""
        dialog = Gtk.MessageDialog(
            parent=self.window,
            flags=0,
            message_type=Gtk.MessageType.INFO,
            buttons=Gtk.ButtonsType.OK,
            text=_("Information")
        )
        dialog.format_secondary_text(str(message))
        dialog.run()
        dialog.destroy()
    
    def run(self):
        """Start the application"""
        self.window.show_all()
        Gtk.main()

def main():
    """Main entry point"""
    app = SessionManagerGUI()
    app.run()

if __name__ == '__main__':
    main()