#!/usr/bin/env python3
"""
MiniOS Session CLI

Command-line utility for managing MiniOS persistent sessions from within the running system.
This is the CLI-only version that performs actual session operations.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
import gettext

# Internationalization setup
try:
    gettext.bindtextdomain('minios-session-manager', '/usr/share/locale')
    gettext.textdomain('minios-session-manager')
    _ = gettext.gettext
except:
    _ = lambda x: x

class SessionManager:
    """Main class for managing MiniOS sessions"""
    
    def __init__(self, privileged_mode=False):
        self.sessions_file = None
        self.sessions_dir = None
        self.current_session = None
        self.session_format = None  # 'json' or 'conf'
        self.privileged_mode = privileged_mode
        self._detect_session_storage()
    
    def _detect_session_storage(self):
        """Detect where sessions are stored and in what format"""
        # Common locations for session storage
        possible_paths = [
            "/run/initramfs/changes",
            "/mnt/live/changes", 
            "/live/changes",
            "/tmp/changes"  # fallback for testing
        ]
        
        for path in possible_paths:
            if os.path.exists(path):
                self.sessions_dir = path
                break
        
        if not self.sessions_dir:
            return False
            
        # Check for session metadata files
        json_file = os.path.join(self.sessions_dir, "session.json")
        conf_file = os.path.join(self.sessions_dir, "session.conf")
        
        if os.path.exists(json_file):
            self.sessions_file = json_file
            self.session_format = "json"
        elif os.path.exists(conf_file):
            self.sessions_file = conf_file  
            self.session_format = "conf"
        
        return True
    
    def _check_access_permissions(self):
        """Check if we have necessary permissions to access sessions directory"""
        if not self.sessions_dir:
            return False
        
        # Try to access the sessions directory
        try:
            os.listdir(self.sessions_dir)
            return True
        except PermissionError:
            return False
    
    def _run_privileged_command(self, command_args):
        """Run a command with elevated privileges using pkexec"""
        # Find the privileged CLI script
        script_dir = os.path.dirname(os.path.abspath(__file__))
        privileged_script = os.path.join(script_dir, "session_cli_privileged.py")
        
        # If running from installed location, adjust path
        if not os.path.exists(privileged_script):
            privileged_script = "/usr/lib/minios-session-manager/session_cli_privileged.py"
        
        if not os.path.exists(privileged_script):
            return False, "", _("Privileged script not found")
        
        try:
            # Use pkexec to run the privileged script
            cmd = ["pkexec", "python3", privileged_script] + command_args
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            return result.returncode == 0, result.stdout, result.stderr
        except subprocess.TimeoutExpired:
            return False, "", _("Command timed out")
        except subprocess.CalledProcessError as e:
            return False, "", str(e)
        except Exception as e:
            return False, "", str(e)
    
    def _requires_privileges(self, operation):
        """Check if an operation requires elevated privileges"""
        # Read operations that might not need privileges
        read_operations = ['list', 'current', 'info']
        
        # Write operations that need privileges
        write_operations = ['activate', 'create', 'delete', 'cleanup']
        
        # Check if we have access to sessions directory
        if not self._check_access_permissions():
            return True
        
        # For write operations, check if sessions directory is writable
        if operation in write_operations:
            try:
                # Try to create a temporary file to test write access
                test_file = os.path.join(self.sessions_dir, ".write_test")
                with open(test_file, 'w') as f:
                    f.write("test")
                os.remove(test_file)
                return False  # We have write access, no privileges needed
            except (PermissionError, OSError):
                return True  # We need privileges for write operations
        
        return False  # Read operations typically don't need privileges
    
    def _execute_operation(self, operation, *args):
        """Execute an operation, using privileges if necessary"""
        if not self.privileged_mode and self._requires_privileges(operation):
            # Build command arguments
            cmd_args = [operation]
            cmd_args.extend(str(arg) for arg in args if arg is not None)
            
            # Run with privileges
            success, output, error = self._run_privileged_command(cmd_args)
            if success:
                # For some operations, we need to return specific formats
                if operation == 'list':
                    print(output.strip())
                    return []  # Return empty list as sessions are already printed
                elif operation in ['activate', 'create', 'delete']:
                    return success, output.strip()
                elif operation == 'cleanup':
                    print(output.strip())
                    return 0, []  # Return success count and empty errors
                else:
                    return output.strip()
            else:
                if operation in ['activate', 'create', 'delete']:
                    return success, error.strip()
                else:
                    raise Exception(error.strip())
        else:
            # Execute normally without privileges
            return None  # Indicates that normal execution should proceed
    
    def _read_sessions_metadata(self):
        """Read session metadata from file"""
        if not self.sessions_file or not os.path.exists(self.sessions_file):
            return {"default": None, "sessions": {}}
            
        try:
            if self.session_format == "json":
                with open(self.sessions_file, 'r') as f:
                    return json.load(f)
            else:  # conf format
                metadata = {"default": None, "sessions": {}}
                with open(self.sessions_file, 'r') as f:
                    for line in f:
                        line = line.strip()
                        if line.startswith("default="):
                            metadata["default"] = line.split("=", 1)[1]
                        elif line.startswith("session_"):
                            # Parse session_mode[1]=native format
                            parts = line.split("=", 1)
                            if len(parts) == 2:
                                key, value = parts
                                if "[" in key and "]" in key:
                                    field = key.split("[")[0].replace("session_", "")
                                    session_id = key.split("[")[1].split("]")[0]
                                    if session_id not in metadata["sessions"]:
                                        metadata["sessions"][session_id] = {}
                                    metadata["sessions"][session_id][field] = value
                return metadata
        except Exception as e:
            print(f"Error reading sessions metadata: {e}", file=sys.stderr)
            return {"default": None, "sessions": {}}
    
    def _write_sessions_metadata(self, metadata):
        """Write session metadata to file"""
        if not self.sessions_file:
            return False
            
        try:
            if self.session_format == "json":
                with open(self.sessions_file, 'w') as f:
                    json.dump(metadata, f, indent=2)
            else:  # conf format
                with open(self.sessions_file, 'w') as f:
                    f.write(f"default={metadata.get('default', '')}\n")
                    for session_id, session_data in metadata.get("sessions", {}).items():
                        for field, value in session_data.items():
                            f.write(f"session_{field}[{session_id}]={value}\n")
            return True
        except Exception as e:
            print(f"Error writing sessions metadata: {e}", file=sys.stderr)
            return False
    
    def list_sessions(self):
        """List all available sessions"""
        # Check if we need to use privileges
        result = self._execute_operation('list')
        if result is not None:
            return result
        
        if not self.sessions_dir:
            return []
            
        sessions = []
        metadata = self._read_sessions_metadata()
        
        # Find session directories (numeric names)
        for item in os.listdir(self.sessions_dir):
            path = os.path.join(self.sessions_dir, item)
            if os.path.isdir(path) and item.isdigit():
                session_id = item
                session_data = metadata.get("sessions", {}).get(session_id, {})
                
                # Get directory stats
                stat = os.stat(path)
                size = self._get_directory_size(path)
                
                sessions.append({
                    'id': session_id,
                    'path': path,
                    'mode': session_data.get('mode', 'unknown'),
                    'version': session_data.get('version', 'unknown'),
                    'edition': session_data.get('edition', 'unknown'), 
                    'size': size,
                    'modified': datetime.fromtimestamp(stat.st_mtime),
                    'is_default': metadata.get('default') == session_id
                })
        
        # Sort by session ID
        sessions.sort(key=lambda x: int(x['id']))
        return sessions
    
    def _get_directory_size(self, path):
        """Get total size of directory in bytes"""
        # Check if this is a dynfilefs session
        changes_file = os.path.join(path, "changes.dat")
        if os.path.exists(changes_file) or any(f.startswith("changes.dat") for f in os.listdir(path) if os.path.isfile(os.path.join(path, f))):
            return self._get_dynfilefs_size(path)
        
        # Regular directory size calculation
        total = 0
        try:
            for dirpath, dirnames, filenames in os.walk(path):
                for filename in filenames:
                    filepath = os.path.join(dirpath, filename)
                    if os.path.exists(filepath):  # Check for broken symlinks
                        total += os.path.getsize(filepath)
        except (OSError, PermissionError):
            pass
        return total
    
    def _format_size(self, size_bytes):
        """Format size in human readable format"""
        for unit in ['B', 'KB', 'MB', 'GB']:
            if size_bytes < 1024.0:
                return f"{size_bytes:.1f}{unit}"
            size_bytes /= 1024.0
        return f"{size_bytes:.1f}TB"
    
    def _check_dynfilefs_available(self):
        """Check if dynfilefs is available on the system"""
        try:
            result = subprocess.run(['which', 'dynfilefs'], capture_output=True)
            if result.returncode == 0:
                return True
            # Also check for mount.dynfilefs
            result = subprocess.run(['which', 'mount.dynfilefs'], capture_output=True)
            return result.returncode == 0
        except:
            return False
    
    def _detect_filesystem_type(self):
        """Detect the filesystem type of the MiniOS media"""
        if not self.sessions_dir:
            return None, _("Sessions directory not found")
        
        try:
            # Get the device where sessions directory is mounted
            # Use df to find the device and mount options
            df_result = subprocess.run(['df', '-T', self.sessions_dir], capture_output=True, text=True)
            if df_result.returncode != 0:
                return None, _("Failed to determine filesystem information")
            
            lines = df_result.stdout.strip().split('\n')
            if len(lines) < 2:
                return None, _("Invalid df output")
            
            # Parse df output: Filesystem Type 1K-blocks Used Available Use% Mounted
            fields = lines[1].split()
            if len(fields) < 2:
                return None, _("Cannot parse filesystem information")
            
            filesystem_type = fields[1].lower()
            device = fields[0]
            
            # Get additional mount information
            mount_result = subprocess.run(['mount'], capture_output=True, text=True)
            mount_options = ""
            
            if mount_result.returncode == 0:
                for line in mount_result.stdout.split('\n'):
                    if device in line and self.sessions_dir in line:
                        # Extract mount options
                        if '(' in line and ')' in line:
                            mount_options = line.split('(')[1].split(')')[0]
                        break
            
            return {
                'type': filesystem_type,
                'device': device,
                'mount_options': mount_options,
                'is_readonly': 'ro' in mount_options,
                'is_posix_compatible': filesystem_type in ['ext2', 'ext3', 'ext4', 'btrfs', 'xfs', 'f2fs', 'reiserfs']
            }, None
            
        except Exception as e:
            return None, _("Error detecting filesystem: {}").format(str(e))
    
    def _get_compatible_session_modes(self, filesystem_info):
        """Get list of compatible session modes for the filesystem"""
        if not filesystem_info:
            return ['native', 'dynfilefs', 'raw']  # Default to all if unknown
        
        fs_type = filesystem_info['type']
        is_readonly = filesystem_info['is_readonly']
        is_posix = filesystem_info['is_posix_compatible']
        
        if is_readonly:
            return []  # No sessions can be created on readonly media
        
        compatible_modes = []
        
        # Native mode: requires POSIX-compatible filesystem
        if is_posix:
            compatible_modes.append('native')
        
        # DynFileFS mode: works on most writable filesystems
        compatible_modes.append('dynfilefs')
        
        # Raw mode: works on any writable filesystem
        compatible_modes.append('raw')
        
        return compatible_modes
    
    def _get_filesystem_limitations(self, filesystem_info):
        """Get filesystem-specific limitations"""
        limitations = {}
        
        if not filesystem_info:
            return limitations
        
        fs_type = filesystem_info['type']
        
        # FAT32 limitations
        if fs_type in ['vfat', 'fat32', 'msdos']:
            limitations['max_file_size'] = 4 * 1024  # 4GB in MB
            limitations['no_posix'] = True
            limitations['case_insensitive'] = True
        
        # NTFS limitations  
        elif fs_type in ['ntfs', 'ntfs-3g']:
            limitations['no_posix'] = True
            limitations['case_insensitive'] = True
        
        # exFAT limitations
        elif fs_type in ['exfat']:
            limitations['no_posix'] = True
            limitations['case_insensitive'] = True
        
        return limitations
    
    def _create_dynfilefs_session(self, session_path, initial_size_mb=4000):
        """Create a dynfilefs session structure"""
        try:
            # Create changes.dat file path
            changes_file = os.path.join(session_path, "changes.dat")
            
            # Create a temporary mount point
            with tempfile.TemporaryDirectory() as temp_mount:
                # Mount dynfilefs
                cmd = [
                    'dynfilefs',
                    '-f', changes_file,
                    '-m', temp_mount,
                    '-s', str(initial_size_mb),
                    '-p', '4000'  # 4GB split size
                ]
                
                # Run dynfilefs
                process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                time.sleep(0.5)  # Give it time to mount
                
                # Check if mount was successful
                virtual_file = os.path.join(temp_mount, "virtual.dat")
                if not os.path.exists(virtual_file):
                    process.terminate()
                    return False, _("Failed to create dynfilefs virtual file")
                
                # Format the virtual file with ext4
                format_cmd = ['mke2fs', '-F', '-t', 'ext4', virtual_file]
                format_result = subprocess.run(format_cmd, capture_output=True)
                
                # Unmount dynfilefs
                subprocess.run(['fusermount', '-u', temp_mount], capture_output=True)
                process.terminate()
                process.wait()
                
                if format_result.returncode != 0:
                    return False, _("Failed to format dynfilefs virtual file: {}").format(format_result.stderr.decode())
                
                return True, _("DynFileFS session created successfully")
                
        except Exception as e:
            return False, _("Error creating dynfilefs session: {}").format(str(e))
    
    def _get_dynfilefs_size(self, session_path):
        """Get the actual size of a dynfilefs session"""
        changes_file = os.path.join(session_path, "changes.dat")
        total_size = 0
        
        try:
            # Count all changes.dat.* files
            for file in os.listdir(session_path):
                if file.startswith("changes.dat"):
                    file_path = os.path.join(session_path, file)
                    if os.path.isfile(file_path):
                        total_size += os.path.getsize(file_path)
        except:
            pass
        
        return total_size
    
    def get_current_session(self):
        """Get information about currently active session"""
        metadata = self._read_sessions_metadata()
        current_id = metadata.get('default')
        
        if not current_id:
            return None
            
        sessions = self.list_sessions()
        for session in sessions:
            if session['id'] == current_id:
                return session
        return None
    
    def activate_session(self, session_id):
        """Activate a session (set as default)"""
        # Check if we need to use privileges
        result = self._execute_operation('activate', session_id)
        if result is not None:
            return result
        
        if not self.sessions_dir:
            return False, _("Sessions directory not found")
            
        session_path = os.path.join(self.sessions_dir, session_id)
        if not os.path.exists(session_path):
            return False, _("Session {} does not exist").format(session_id)
        
        try:
            # Update metadata to set new default
            metadata = self._read_sessions_metadata()
            old_default = metadata.get("default")
            metadata["default"] = session_id
            
            if self._write_sessions_metadata(metadata):
                if old_default:
                    return True, _("Session {} activated (was session {})").format(session_id, old_default)
                else:
                    return True, _("Session {} activated").format(session_id)
            else:
                return False, _("Failed to update session metadata")
        except Exception as e:
            return False, _("Error activating session: {}").format(str(e))
    
    def create_session(self, session_mode="native", size_mb=None):
        """Create a new session"""
        # Check if we need to use privileges
        cmd_args = ['create', '--mode', session_mode]
        if size_mb is not None:
            cmd_args.extend(['--size', str(size_mb)])
        result = self._execute_operation(*cmd_args)
        if result is not None:
            return result
        
        if not self.sessions_dir:
            return False, _("Sessions directory not found")
        
        # Validate session mode
        valid_modes = ["native", "dynfilefs", "raw"]
        if session_mode not in valid_modes:
            return False, _("Invalid session mode. Must be one of: {}").format(", ".join(valid_modes))
        
        # Detect filesystem type and check compatibility
        filesystem_info, fs_error = self._detect_filesystem_type()
        if fs_error:
            return False, fs_error
        
        compatible_modes = self._get_compatible_session_modes(filesystem_info)
        if session_mode not in compatible_modes:
            fs_type = filesystem_info['type'] if filesystem_info else "unknown"
            if filesystem_info and filesystem_info['is_readonly']:
                return False, _("Cannot create sessions on read-only media ({})").format(fs_type)
            elif session_mode == "native" and not filesystem_info['is_posix_compatible']:
                return False, _("Native mode is not compatible with {} filesystem. Use dynfilefs or raw mode instead.").format(fs_type)
            else:
                return False, _("Session mode '{}' is not compatible with {} filesystem").format(session_mode, fs_type)
        
        # Get filesystem limitations
        limitations = self._get_filesystem_limitations(filesystem_info)
        
        # Check size limitations for FAT32
        if size_mb and 'max_file_size' in limitations:
            max_size = limitations['max_file_size']
            if session_mode == "raw" and size_mb > max_size:
                return False, _("Raw image size {}MB exceeds FAT32 file size limit ({}MB). Use dynfilefs mode or smaller size.").format(size_mb, max_size)
        
        # Check dynfilefs availability for dynfilefs mode
        if session_mode == "dynfilefs" and not self._check_dynfilefs_available():
            return False, _("DynFileFS is not available on this system. Please install dynfilefs package.")
        
        try:
            # Find next available session ID
            existing_sessions = []
            for item in os.listdir(self.sessions_dir):
                path = os.path.join(self.sessions_dir, item)
                if os.path.isdir(path) and item.isdigit():
                    existing_sessions.append(int(item))
            
            if existing_sessions:
                new_id = str(max(existing_sessions) + 1)
            else:
                new_id = "1"
            
            # Create session directory
            session_path = os.path.join(self.sessions_dir, new_id)
            os.makedirs(session_path, exist_ok=True)
            
            # Initialize session based on mode
            if session_mode == "dynfilefs":
                # Set default size if not specified
                if size_mb is None:
                    size_mb = 4000  # 4GB default
                
                success, message = self._create_dynfilefs_session(session_path, size_mb)
                if not success:
                    # Clean up on failure
                    try:
                        shutil.rmtree(session_path)
                    except:
                        pass
                    return False, message
            
            elif session_mode == "raw":
                # Create raw image file
                if size_mb is None:
                    size_mb = 4000  # 4GB default
                
                image_file = os.path.join(session_path, "changes.img")
                try:
                    # Create sparse file with specified size
                    with open(image_file, 'wb') as f:
                        f.seek(size_mb * 1024 * 1024 - 1)
                        f.write(b'\0')
                    
                    # Format with ext4
                    format_cmd = ['mke2fs', '-F', '-t', 'ext4', image_file]
                    format_result = subprocess.run(format_cmd, capture_output=True)
                    
                    if format_result.returncode != 0:
                        shutil.rmtree(session_path)
                        return False, _("Failed to format raw image file: {}").format(format_result.stderr.decode())
                        
                except Exception as e:
                    shutil.rmtree(session_path)
                    return False, _("Failed to create raw image file: {}").format(str(e))
            
            # For native mode, just create empty directory (no special initialization needed)
            
            # Get system version and edition
            version = "unknown"
            edition = "unknown"
            
            # Try to read from /etc/minios-release
            release_file = "/etc/minios-release"
            if os.path.exists(release_file):
                try:
                    with open(release_file, 'r') as f:
                        for line in f:
                            if line.startswith("VERSION="):
                                version = line.split("=", 1)[1].strip().strip('"')
                            elif line.startswith("EDITION="):
                                edition = line.split("=", 1)[1].strip().strip('"')
                except:
                    pass
            
            # Update metadata
            metadata = self._read_sessions_metadata()
            if "sessions" not in metadata:
                metadata["sessions"] = {}
            
            metadata["sessions"][new_id] = {
                "mode": session_mode,
                "version": version,
                "edition": edition
            }
            
            if self._write_sessions_metadata(metadata):
                if session_mode == "dynfilefs":
                    return True, _("Session {} created successfully (mode: {}, size: {}MB)").format(new_id, session_mode, size_mb)
                elif session_mode == "raw":
                    return True, _("Session {} created successfully (mode: {}, size: {}MB)").format(new_id, session_mode, size_mb)
                else:
                    return True, _("Session {} created successfully (mode: {})").format(new_id, session_mode)
            else:
                # Clean up on metadata failure
                try:
                    shutil.rmtree(session_path)
                except:
                    pass
                return False, _("Failed to update session metadata")
                
        except Exception as e:
            return False, _("Error creating session: {}").format(str(e))
    
    def delete_session(self, session_id):
        """Delete a session"""
        # Check if we need to use privileges
        result = self._execute_operation('delete', session_id)
        if result is not None:
            return result
        
        if not self.sessions_dir:
            return False, _("Sessions directory not found")
            
        session_path = os.path.join(self.sessions_dir, session_id)
        if not os.path.exists(session_path):
            return False, _("Session {} does not exist").format(session_id)
        
        # Check if it's the current session
        current = self.get_current_session()
        if current and current['id'] == session_id:
            return False, _("Cannot delete currently active session")
        
        try:
            shutil.rmtree(session_path)
            
            # Update metadata
            metadata = self._read_sessions_metadata()
            if session_id in metadata.get("sessions", {}):
                del metadata["sessions"][session_id]
                self._write_sessions_metadata(metadata)
            
            return True, _("Session {} deleted successfully").format(session_id)
        except Exception as e:
            return False, _("Error deleting session: {}").format(str(e))
    
    def cleanup_old_sessions(self, days_threshold=30):
        """Clean up sessions older than specified days"""
        # Check if we need to use privileges
        result = self._execute_operation('cleanup', '--days', str(days_threshold))
        if result is not None:
            return result
        
        sessions = self.list_sessions()
        current = self.get_current_session()
        current_id = current['id'] if current else None
        
        old_sessions = []
        cutoff_date = datetime.now().timestamp() - (days_threshold * 24 * 3600)
        
        for session in sessions:
            if session['id'] != current_id and session['modified'].timestamp() < cutoff_date:
                old_sessions.append(session)
        
        deleted_count = 0
        errors = []
        
        for session in old_sessions:
            success, message = self.delete_session(session['id'])
            if success:
                deleted_count += 1
            else:
                errors.append(f"Session {session['id']}: {message}")
        
        return deleted_count, errors
    
    def get_filesystem_info(self):
        """Get filesystem information and compatibility"""
        filesystem_info, error = self._detect_filesystem_type()
        if error:
            return None, error
            
        compatible_modes = self._get_compatible_session_modes(filesystem_info)
        limitations = self._get_filesystem_limitations(filesystem_info)
        
        return {
            'filesystem': filesystem_info,
            'compatible_modes': compatible_modes,
            'limitations': limitations
        }, None

def format_session_list(sessions):
    """Format session list for display"""
    if not sessions:
        return _("No sessions found")
    
    lines = []
    lines.append(_("Available Sessions:"))
    lines.append("-" * 80)
    
    for session in sessions:
        status = _("(CURRENT)") if session['is_default'] else ""
        modified_str = session['modified'].strftime("%Y-%m-%d %H:%M:%S")
        size_str = SessionManager()._format_size(session['size'])
        
        lines.append(f"Session #{session['id']} {status}")
        lines.append(f"  Mode: {session['mode']}")
        lines.append(f"  Version: {session['version']} / {session['edition']}")
        lines.append(f"  Size: {size_str}")
        lines.append(f"  Last Modified: {modified_str}")
        lines.append("")
    
    return "\n".join(lines)

# GUI functions removed - this is CLI-only version

def main():
    """Main application entry point"""
    parser = argparse.ArgumentParser(
        description=_('MiniOS Session Manager - Manage persistent sessions'),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_("""
Examples:
  minios-session-cli list                       # List all sessions
  minios-session-cli current                    # Show current session
  minios-session-cli info                       # Show filesystem compatibility info
  minios-session-cli activate 2                # Activate session #2
  minios-session-cli create --mode native      # Create new native session
  minios-session-cli create --mode dynfilefs   # Create dynfilefs session (4GB)
  minios-session-cli create --mode dynfilefs --size 8000  # Create 8GB dynfilefs session
  minios-session-cli create --mode raw --size 2000        # Create 2GB raw session
  minios-session-cli delete 3                  # Delete session #3
  minios-session-cli cleanup --days 30         # Delete sessions older than 30 days
        """)
    )
    
    subparsers = parser.add_subparsers(dest='command', help=_('Available commands'))
    
    # List command
    list_parser = subparsers.add_parser('list', help=_('List all sessions'))
    
    # Current command
    current_parser = subparsers.add_parser('current', help=_('Show current session'))
    
    # Info command
    info_parser = subparsers.add_parser('info', help=_('Show filesystem and compatibility information'))
    
    # Activate command
    activate_parser = subparsers.add_parser('activate', help=_('Activate a session'))
    activate_parser.add_argument('session_id', help=_('Session ID to activate'))
    
    # Create command
    create_parser = subparsers.add_parser('create', help=_('Create a new session'))
    create_parser.add_argument('--mode', choices=['native', 'dynfilefs', 'raw'], 
                              default='native', help=_('Session mode (default: native)'))
    create_parser.add_argument('--size', type=int, metavar='MB',
                              help=_('Size in MB for dynfilefs/raw modes (default: 4000)'))
    
    # Delete command
    delete_parser = subparsers.add_parser('delete', help=_('Delete a session'))
    delete_parser.add_argument('session_id', help=_('Session ID to delete'))
    
    # Cleanup command
    cleanup_parser = subparsers.add_parser('cleanup', help=_('Clean up old sessions'))
    cleanup_parser.add_argument('--days', type=int, default=30, 
                               help=_('Delete sessions older than N days (default: 30)'))
    
# GUI command removed - use minios-session-manager for GUI
    
    args = parser.parse_args()
    
    # Initialize session manager
    manager = SessionManager()
    
    if not manager.sessions_dir:
        print(_("Error: Could not find sessions directory."), file=sys.stderr)
        print(_("This tool must be run from within a MiniOS live system with persistent sessions enabled."), file=sys.stderr)
        sys.exit(1)
    
    # Handle commands
    if args.command == 'list':
        sessions = manager.list_sessions()
        print(format_session_list(sessions))
    
    elif args.command == 'current':
        current = manager.get_current_session()
        if current:
            print(_("Current session: #{}").format(current['id']))
            print(_("Mode: {}").format(current['mode']))
            print(_("Version: {} / {}").format(current['version'], current['edition']))
            print(_("Size: {}").format(manager._format_size(current['size'])))
            print(_("Last Modified: {}").format(current['modified'].strftime("%Y-%m-%d %H:%M:%S")))
        else:
            print(_("No current session found"))
    
    elif args.command == 'info':
        fs_info, error = manager.get_filesystem_info()
        if error:
            print(_("Error: {}").format(error), file=sys.stderr)
            sys.exit(1)
        
        print(_("MiniOS Media Information:"))
        print("-" * 40)
        fs = fs_info['filesystem']
        print(_("Filesystem Type: {}").format(fs['type']))
        print(_("Device: {}").format(fs['device']))
        print(_("Mount Options: {}").format(fs['mount_options'] or _("none")))
        print(_("Read-only: {}").format(_("Yes") if fs['is_readonly'] else _("No")))
        print(_("POSIX Compatible: {}").format(_("Yes") if fs['is_posix_compatible'] else _("No")))
        print()
        
        print(_("Compatible Session Modes:"))
        compatible = fs_info['compatible_modes']
        if compatible:
            for mode in compatible:
                print(f"  ✓ {mode}")
        else:
            print(_("  None (read-only media)"))
        print()
        
        limitations = fs_info['limitations']
        if limitations:
            print(_("Filesystem Limitations:"))
            if 'max_file_size' in limitations:
                print(_("  • Maximum file size: {}MB ({:.1f}GB)").format(
                    limitations['max_file_size'], limitations['max_file_size'] / 1024))
            if 'no_posix' in limitations:
                print(_("  • No POSIX features (no native mode support)"))
            if 'case_insensitive' in limitations:
                print(_("  • Case-insensitive filenames"))
        else:
            print(_("No known limitations"))
    
    elif args.command == 'activate':
        success, message = manager.activate_session(args.session_id)
        print(message)
        sys.exit(0 if success else 1)
    
    elif args.command == 'create':
        success, message = manager.create_session(args.mode, args.size)
        print(message)
        sys.exit(0 if success else 1)
    
    elif args.command == 'delete':
        success, message = manager.delete_session(args.session_id)
        print(message)
        sys.exit(0 if success else 1)
    
    elif args.command == 'cleanup':
        deleted_count, errors = manager.cleanup_old_sessions(args.days)
        print(_("Cleanup completed: {} sessions deleted").format(deleted_count))
        if errors:
            print(_("Errors:"))
            for error in errors:
                print(f"  {error}")
    
# GUI command removed
    
    else:
        parser.print_help()

if __name__ == '__main__':
    main()