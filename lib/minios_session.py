#!/usr/bin/env python3
"""
MiniOS Session CLI

Command-line utility for managing MiniOS persistent sessions from within the running system.
This is the CLI-only version that performs actual session operations.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
import gettext

# Internationalization setup
def _(message):
    """Translation function wrapper"""
    try:
        return gettext.dgettext('minios-session-manager', message)
    except:
        return message

try:
    gettext.bindtextdomain('minios-session-manager', '/usr/share/locale')
    gettext.textdomain('minios-session-manager')
except Exception:
    pass

class SessionManager:
    """Main class for managing MiniOS sessions"""

    def __init__(self, custom_sessions_dir=None):
        self.sessions_file = None
        self.sessions_dir = None
        self.current_session = None
        self.session_format = None  # 'json' or 'conf'
        self.custom_sessions_dir = custom_sessions_dir
        
        # Setup cache directory and file in /tmp (clears on reboot)
        self.cache_dir = f"/tmp/minios-session-manager-{os.getuid()}"
        self.cache_file = os.path.join(self.cache_dir, "session_sizes.json")
        self._ensure_cache_dir()
        
            
        self._detect_session_storage()

    def _ensure_cache_dir(self):
        """Ensure cache directory exists"""
        try:
            os.makedirs(self.cache_dir, mode=0o755, exist_ok=True)
        except OSError:
            # Fallback to system temp directory if creation fails
            import tempfile
            self.cache_dir = tempfile.gettempdir()
            self.cache_file = os.path.join(self.cache_dir, f"minios-session-cache-{os.getuid()}.json")

    def _load_size_cache(self):
        """Load size cache from /tmp"""
        if not os.path.exists(self.cache_file):
            return {}
        
        try:
            with open(self.cache_file, 'r') as f:
                data = json.load(f)
                # Validate cache against current sessions directory
                if data.get('sessions_dir') != self.sessions_dir:
                    return {}  # Cache invalid - different sessions directory
                return data.get('cache', {})
        except (json.JSONDecodeError, OSError):
            return {}

    def _save_size_cache(self, cache_data):
        """Save size cache to /tmp"""
        try:
            cache_content = {
                "version": "1.0",
                "sessions_dir": self.sessions_dir,
                "updated_at": time.time(),
                "cache": cache_data
            }
            
            with open(self.cache_file, 'w') as f:
                json.dump(cache_content, f, indent=2)
        except OSError:
            pass  # Ignore cache write failures

    def _make_temp_dir(self):
        """Create temporary directory in sessions directory

        Returns path to temporary directory that should be cleaned up after use
        """
        import random
        import string

        # Generate random name
        rand_suffix = ''.join(random.choices(string.ascii_lowercase + string.digits, k=8))
        temp_name = f".tmp_{rand_suffix}"
        temp_path = os.path.join(self.sessions_dir, temp_name)
        os.makedirs(temp_path, exist_ok=True)
        return temp_path

    def _update_size_cache(self, session_id, size, mtime):
        """Update size cache for specific session"""
        cache_data = self._load_size_cache()
        cache_data[session_id] = {
            'size': size,
            'size_formatted': self._format_size(size),
            'mtime': mtime,
            'cached_at': time.time()
        }
        self._save_size_cache(cache_data)


    def _get_current_union_fs(self):
        """Get current union filesystem type"""
        try:
            # First check if union= parameter was used
            with open('/proc/cmdline', 'r') as f:
                cmdline = f.read().strip()
                union_match = re.search(r'union=(\w+)', cmdline)
                if union_match:
                    union_param = union_match.group(1)
                    if union_param in ['aufs', 'overlayfs']:
                        return union_param

            # Auto-detection based on kernel support
            with open('/proc/filesystems', 'r') as f:
                filesystems = f.read()
                if 'aufs' in filesystems:
                    return 'aufs'
                else:
                    return 'overlayfs'
        except (OSError, IOError):
            return 'unknown'

    def _get_system_version(self):
        """Get MiniOS system version"""
        try:
            release_file = "/etc/minios-release"
            if os.path.exists(release_file):
                with open(release_file, 'r') as f:
                    for line in f:
                        if line.startswith("VERSION="):
                            return line.split("=", 1)[1].strip().strip('"')
        except:
            pass
        return "unknown"

    def _get_system_edition(self):
        """Get MiniOS system edition"""
        try:
            release_file = "/etc/minios-release"
            if os.path.exists(release_file):
                with open(release_file, 'r') as f:
                    for line in f:
                        if line.startswith("EDITION="):
                            return line.split("=", 1)[1].strip().strip('"')
        except:
            pass
        return "unknown"

    def _check_free_space(self, path, required_mb):
        """Check if there is enough free space at the given path

        Args:
            path: Path to check (file or directory)
            required_mb: Required space in megabytes

        Returns:
            (bool, str): (has_space, error_message)
        """
        try:
            # Get the directory to check
            if os.path.isfile(path):
                check_path = os.path.dirname(path)
            else:
                check_path = path

            # Get filesystem stats
            stat = os.statvfs(check_path)

            # Calculate free space in bytes
            free_bytes = stat.f_bavail * stat.f_frsize
            free_mb = free_bytes / (1024 * 1024)

            # Add 10% buffer for safety
            required_with_buffer = required_mb * 1.1

            if free_mb < required_with_buffer:
                return False, _("Insufficient disk space: {} MB required, {} MB available").format(
                    int(required_with_buffer), int(free_mb))

            return True, None

        except Exception as e:
            # If we can't check, proceed anyway but log the issue
            return True, None

    def _detect_session_storage(self):
        """Detect where sessions are stored and in what format"""
        # If custom directory is specified, use it
        if self.custom_sessions_dir:
            if os.path.exists(self.custom_sessions_dir):
                self.sessions_dir = self.custom_sessions_dir
            else:
                return False
        else:
            # Common locations for session storage
            possible_paths = [
                "/run/initramfs/memory/data/minios/changes",
                "/lib/live/mount/data/minios/changes",
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




    def check_sessions_directory_status(self):
        """Check sessions directory status and write permissions"""
        if not self.sessions_dir:
            return {
                'success': False,
                'found': False,
                'writable': False,
                'sessions_dir': None,
                'error': _('Sessions directory not found')
            }
        
        # Check if directory exists
        if not os.path.exists(self.sessions_dir):
            return {
                'success': True,
                'found': False,
                'writable': False,
                'sessions_dir': self.sessions_dir,
                'error': _('Sessions directory does not exist')
            }
        
        # Get filesystem type
        fs_type = "unknown"
        try:
            result = subprocess.run(['stat', '-f', '-c', '%T', self.sessions_dir], 
                                  capture_output=True, text=True, check=True)
            fs_type = result.stdout.strip()
        except (subprocess.CalledProcessError, FileNotFoundError):
            # Fallback method using /proc/mounts
            try:
                with open('/proc/mounts', 'r') as f:
                    for line in f:
                        parts = line.split()
                        if len(parts) >= 3:
                            mount_point, fs_type_mount = parts[1], parts[2]
                            if self.sessions_dir.startswith(mount_point):
                                fs_type = fs_type_mount
                                break
            except:
                pass
        
        # Check if directory is writable
        writable = False
        error_msg = None
        
        try:
            # SquashFS is always read-only
            if fs_type == 'squashfs':
                writable = False
                error_msg = _("Directory is on a SquashFS filesystem (read-only)")
            else:
                # Try to create a temporary file to test write access
                try:
                    import tempfile
                    with tempfile.NamedTemporaryFile(dir=self.sessions_dir, delete=True):
                        pass
                    writable = True
                except (OSError, PermissionError) as e:
                    writable = False
                    error_msg = _("Permission denied: {}").format(str(e))
        except Exception as e:
            writable = False
            error_msg = _("Error checking directory: {}").format(str(e))
        
        result = {
            'success': True,
            'found': True,
            'writable': writable,
            'sessions_dir': self.sessions_dir,
            'filesystem_type': fs_type
        }
        if error_msg:
            result['error'] = error_msg
        
        return result


    def _read_sessions_metadata(self):
        """Read session metadata from file"""
        if not self.sessions_file or not os.path.exists(self.sessions_file):
            return {"default": None, "sessions": {}}
            
        try:
            if self.session_format == "json":
                with open(self.sessions_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            else:  # conf format
                metadata = {"default": None, "sessions": {}}
                with open(self.sessions_file, 'r', encoding='utf-8') as f:
                    for line in f:
                        line = line.strip()
                        if line.startswith("default="):
                            metadata["default"] = line.split("=", 1)[1]
                        elif line.startswith("running="):
                            metadata["running"] = line.split("=", 1)[1]
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
            # Try to create sessions file if it doesn't exist
            if self.sessions_dir:
                json_file = os.path.join(self.sessions_dir, "session.json")
                if os.access(os.path.dirname(json_file), os.W_OK):
                    self.sessions_file = json_file
                    self.session_format = "json"
                else:
                    return False
            else:
                return False
            
        try:
            if self.session_format == "json":
                with open(self.sessions_file, 'w', encoding='utf-8') as f:
                    json.dump(metadata, f, indent=2)
            else:  # conf format
                with open(self.sessions_file, 'w', encoding='utf-8') as f:
                    f.write(f"default={metadata.get('default', '')}\n")
                    if 'running' in metadata:
                        f.write(f"running={metadata['running']}\n")
                    for session_id, session_data in metadata.get("sessions", {}).items():
                        for field, value in session_data.items():
                            f.write(f"session_{field}[{session_id}]={value}\n")
            return True
        except Exception as e:
            print(f"Error writing sessions metadata: {e}", file=sys.stderr)
            return False

    def list_sessions(self, include_running_check=True):
        """List all available sessions"""
        if not self.sessions_dir:
            return []
            
        sessions = []
        metadata = self._read_sessions_metadata()
        
        # Get running session info for comparison (avoid recursion)
        running_id = None
        if include_running_check:
            running_session = self.get_running_session(avoid_recursion=True)
            if running_session and 'id' in running_session:
                running_id = running_session['id']
        
        # Find session directories (numeric names)
        for item in os.listdir(self.sessions_dir):
            path = os.path.join(self.sessions_dir, item)
            if os.path.isdir(path) and item.isdigit():
                session_id = item
                session_data = metadata.get("sessions", {}).get(session_id, {})
                
                # Get directory stats
                stat = os.stat(path)
                size_info = self._get_session_size_info(path, session_data)
                
                sessions.append({
                    'id': session_id,
                    'path': path,
                    'mode': session_data.get('mode', 'unknown'),
                    'version': session_data.get('version', 'unknown'),
                    'edition': session_data.get('edition', 'unknown'),
                    'union': session_data.get('union', 'unknown'), 
                    'size': size_info['used_size'],
                    'size_display': size_info['display'],
                    'total_size': size_info.get('total_size'),
                    'total_size_mb': session_data.get('size'),  # Size from metadata in MB
                    'modified': datetime.fromtimestamp(stat.st_mtime),
                    'is_default': metadata.get('default') == session_id,
                    'is_running': session_id == running_id
                })
        
        # Sort by session ID
        sessions.sort(key=lambda x: int(x['id']))
        return sessions

    def _get_directory_size(self, path):
        """Get total size of directory in bytes with caching"""
        session_id = os.path.basename(path)
        
        try:
            # Get current directory modification time
            current_mtime = os.path.getmtime(path)
            
            # Load cache and check if valid
            cache_data = self._load_size_cache()
            session_cache = cache_data.get(session_id, {})
            cached_size = session_cache.get('size')
            cached_mtime = session_cache.get('mtime')
            
            # Check if cache is valid (mtime unchanged)
            if cached_size is not None and cached_mtime == current_mtime:
                return cached_size
            
            # Cache miss or outdated - recalculate
            actual_size = self._calculate_directory_size(path)
            
            # Update cache
            self._update_size_cache(session_id, actual_size, current_mtime)
            
            return actual_size
            
        except (OSError, PermissionError):
            # Fallback to direct calculation without caching
            return self._calculate_directory_size(path)

    def _calculate_directory_size(self, path):
        """Calculate actual directory size (original implementation)"""
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
        
        compatible_modes = []
        
        # Native mode: requires POSIX-compatible filesystem (ext2/3/4, btrfs, xfs, etc.)
        if is_posix:
            compatible_modes.append('native')
        
        # DynFileFS mode: works on ALL writable filesystems (including FAT32, NTFS, ext4, etc.)
        compatible_modes.append('dynfilefs')
        
        # Raw mode: works on ALL writable filesystems (static images)
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

    def _create_dynfilefs_session(self, session_path, initial_size_mb=1000):
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
                # Sync to ensure filesystem is written (important for FAT32/NTFS)
                subprocess.run(['sync'], capture_output=True)

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

    def _get_session_size_info(self, session_path, session_data):
        """Get comprehensive size information for a session"""
        session_mode = session_data.get('mode', 'unknown')
        stored_size = session_data.get('size')  # Size in MB from metadata

        # Convert stored_size to int if it's a string, and normalize to MB
        if stored_size is not None:
            try:
                stored_size = int(stored_size)
                # Auto-detect if stored as bytes vs MB
                # If > 100000, assume it's in bytes and convert to MB
                if stored_size > 100000:
                    stored_size = max(100, int(stored_size / (1024 * 1024)))
            except (ValueError, TypeError):
                stored_size = None

        if session_mode == 'dynfilefs':
            # For dynfilefs, show used/total format
            used_size = self._get_dynfilefs_size(session_path)
            if stored_size is not None:
                total_size_bytes = stored_size * 1024 * 1024
                display = f"{self._format_size(used_size)}/{self._format_size(total_size_bytes)}"
                return {
                    'used_size': used_size,
                    'total_size': total_size_bytes,
                    'display': display
                }
            else:
                # Fallback to just used size if no stored size
                display = self._format_size(used_size)
                return {
                    'used_size': used_size,
                    'display': display
                }
        
        elif session_mode == 'raw':
            # For raw, show total size (the image file size)
            image_file = os.path.join(session_path, "changes.img")
            if os.path.exists(image_file):
                size = os.path.getsize(image_file)
                display = self._format_size(size)
                return {
                    'used_size': size,
                    'display': display
                }
            elif stored_size:
                # Fallback to stored size if image not found
                size_bytes = stored_size * 1024 * 1024
                display = self._format_size(size_bytes)
                return {
                    'used_size': size_bytes,
                    'display': display
                }
        
        # For native mode or fallback, calculate directory size
        size = self._get_directory_size(session_path)
        display = self._format_size(size)
        return {
            'used_size': size,
            'display': display
        }

    def get_current_session(self):
        """Get information about currently active session"""
        metadata = self._read_sessions_metadata()
        current_id = metadata.get('default')
        
        if not current_id:
            return None
            
        sessions = self.list_sessions(include_running_check=False)  # Avoid recursion
        for session in sessions:
            if session['id'] == current_id:
                return session
        return None

    def get_running_session(self, avoid_recursion=False):
        """Get information about currently running session"""
        # Read running session ID from metadata
        metadata = self._read_sessions_metadata()
        running_id = metadata.get('running')
        
        if not running_id:
            return None
            
        # Get session info for the running session
        return self._get_session_info(running_id, avoid_recursion)

    def set_running_session(self, session_id):
        """Set currently running session in metadata"""
        try:
            metadata = self._read_sessions_metadata()
            metadata['running'] = session_id
            return self._write_sessions_metadata(metadata)
        except Exception as e:
            print(f"Error setting running session: {e}", file=sys.stderr)
            return False

    def clear_running_session(self):
        """Clear running session from metadata"""
        try:
            metadata = self._read_sessions_metadata()
            if 'running' in metadata:
                del metadata['running']
            return self._write_sessions_metadata(metadata)
        except Exception as e:
            print(f"Error clearing running session: {e}", file=sys.stderr)
            return False

    def _get_session_info(self, session_id, avoid_recursion=False):
        """Helper to get session info by ID"""
        if avoid_recursion:
            # Simple session info without full list
            session_path = os.path.join(self.sessions_dir, session_id) if self.sessions_dir else None
            return {
                'id': session_id,
                'path': session_path,
                'mode': 'unknown',
                'version': 'unknown', 
                'edition': 'unknown',
                'union': 'unknown',
                'size': 0,
                'modified': None,
                'is_default': False
            }
        else:
            sessions = self.list_sessions(include_running_check=False)
            for session in sessions:
                if session['id'] == session_id:
                    return session
            # Session exists in cmdline but not in filesystem
            return {
                'id': session_id,
                'path': os.path.join(self.sessions_dir, session_id) if self.sessions_dir else None,
                'mode': 'unknown',
                'version': 'unknown',
                'edition': 'unknown',
                'union': 'unknown',
                'size': 0,
                'modified': None,
                'is_default': False,
                'status': 'running_missing'
            }

    def activate_session(self, session_id):
        """Activate a session (set as default)"""
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
        if not self.sessions_dir:
            return False, _("Sessions directory not found")
        
        # Validate session mode
        valid_modes = ["native", "dynfilefs", "raw"]
        if session_mode not in valid_modes:
            return False, _("Invalid session mode. Must be one of: {}").format(", ".join(valid_modes))
        
        # Check if sessions directory is writable
        dir_status = self.check_sessions_directory_status()
        if not dir_status.get('writable', False):
            error_msg = dir_status.get('error', _("Sessions directory is not writable"))
            return False, error_msg
        
        # Detect filesystem type and check compatibility
        filesystem_info, fs_error = self._detect_filesystem_type()
        if fs_error:
            return False, fs_error
        
        compatible_modes = self._get_compatible_session_modes(filesystem_info)
        if session_mode not in compatible_modes:
            fs_type = filesystem_info['type'] if filesystem_info else "unknown"
            if session_mode == "native" and not filesystem_info['is_posix_compatible']:
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

        # Check free disk space
        required_mb = size_mb if size_mb else 1000
        has_space, space_error = self._check_free_space(self.sessions_dir, required_mb)
        if not has_space:
            return False, space_error

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
                    size_mb = 1000  # 1GB default

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
                    size_mb = 1000  # 1GB default
                
                image_file = os.path.join(session_path, "changes.img")
                try:
                    # Create image file with fallocate (works on both sparse and non-sparse filesystems)
                    size_bytes = size_mb * 1024 * 1024
                    result = subprocess.run(['fallocate', '-l', str(size_bytes), image_file],
                                          capture_output=True)
                    if result.returncode != 0:
                        # Fallback to truncate if fallocate not available
                        with open(image_file, 'wb') as f:
                            f.truncate(size_bytes)

                    # Format with ext4
                    format_cmd = ['mke2fs', '-F', '-t', 'ext4', image_file]
                    format_result = subprocess.run(format_cmd, capture_output=True)
                    # Sync to ensure filesystem is written (important for FAT32/NTFS)
                    subprocess.run(['sync'], capture_output=True)

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
            union = self._get_current_union_fs()
            
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
                "edition": edition,
                "union": union
            }
            
            # Add size information for dynfilefs and raw modes
            if session_mode in ["dynfilefs", "raw"] and size_mb:
                metadata["sessions"][new_id]["size"] = size_mb
            
            if self._write_sessions_metadata(metadata):
                # Use safer string formatting to avoid potential translation issues
                try:
                    if session_mode == "dynfilefs":
                        message = _("Session {} created successfully (mode: {}, size: {}MB)").format(new_id, session_mode, size_mb)
                    elif session_mode == "raw":
                        message = _("Session {} created successfully (mode: {}, size: {}MB)").format(new_id, session_mode, size_mb)
                    else:
                        message = _("Session {} created successfully (mode: {})").format(new_id, session_mode)
                    return True, message
                except Exception:
                    # Fallback to simple English message if translation fails
                    if session_mode in ["dynfilefs", "raw"] and size_mb is not None:
                        return True, f"Session {new_id} created successfully (mode: {session_mode}, size: {size_mb}MB)"
                    else:
                        return True, f"Session {new_id} created successfully (mode: {session_mode})"
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
        if not self.sessions_dir:
            return False, _("Sessions directory not found")
            
        session_path = os.path.join(self.sessions_dir, session_id)
        if not os.path.exists(session_path):
            return False, _("Session {} does not exist").format(session_id)
        
        # Check if it's the current session
        current = self.get_current_session()
        if current and current['id'] == session_id:
            return False, _("Cannot delete currently active session")
        
        # Check if it's the running session
        running = self.get_running_session()
        if running and running['id'] == session_id:
            return False, _("Cannot delete currently running session")
        
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
        sessions = self.list_sessions()
        current = self.get_current_session()
        current_id = current['id'] if current else None
        running = self.get_running_session()
        running_id = running['id'] if running else None
        
        old_sessions = []
        cutoff_date = datetime.now().timestamp() - (days_threshold * 24 * 3600)
        
        for session in sessions:
            # Skip current (active) session, running session, and sessions newer than cutoff
            if (session['id'] != current_id and 
                session['id'] != running_id and 
                session['modified'].timestamp() < cutoff_date):
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

    def resize_session(self, session_id, new_size_mb):
        """Resize a session to new size"""
        if not self.sessions_dir:
            return False, _("Sessions directory not found")
            
        session_path = os.path.join(self.sessions_dir, session_id)
        if not os.path.exists(session_path):
            return False, _("Session {} does not exist").format(session_id)
        
        # Get session metadata to determine mode
        metadata = self._read_sessions_metadata()
        session_data = metadata.get("sessions", {}).get(session_id, {})
        session_mode = session_data.get("mode", "unknown")
        
        if session_mode not in ["dynfilefs", "raw"]:
            return False, _("Resize is only supported for dynfilefs and raw mode sessions")
        
        # Check if it's the running session
        running = self.get_running_session()
        if running and running['id'] == session_id:
            return False, _("Cannot resize currently running session")
        
        try:
            if session_mode == "dynfilefs":
                return self._resize_dynfilefs_session(session_path, new_size_mb, session_id, metadata)
            elif session_mode == "raw":
                return self._resize_raw_session(session_path, new_size_mb, session_id, metadata)
                
        except Exception as e:
            return False, _("Error resizing session: {}").format(str(e))

    def _resize_dynfilefs_session(self, session_path, new_size_mb, session_id, metadata):
        """Resize a dynfilefs session"""
        changes_file = os.path.join(session_path, "changes.dat")
        if not os.path.exists(changes_file):
            return False, _("DynFileFS changes.dat file not found")
        
        try:
            # Get current total size from metadata
            current_total_size = metadata.get("sessions", {}).get(session_id, {}).get("size", 0)
            if isinstance(current_total_size, str):
                current_total_size = int(current_total_size)
            
            # Get actually used size
            used_size_bytes = self._get_dynfilefs_size(session_path)
            used_size_mb = used_size_bytes // (1024 * 1024)
            
            # Check both conditions: new size must be larger than both current total AND used size
            if new_size_mb <= current_total_size:
                return False, _("New size must be larger than current total size ({}MB)").format(current_total_size)
            
            if new_size_mb <= used_size_mb:
                return False, _("New size must be larger than used size ({}MB)").format(used_size_mb)
            
            # Create temporary mount point for resizing
            temp_mount = f"/tmp/dynfilefs_resize_{session_id}_{os.getpid()}"
            os.makedirs(temp_mount, exist_ok=True)
            
            try:
                # Mount dynfilefs with the new size - this will expand the virtual.dat file automatically
                mount_cmd = [
                    'dynfilefs', 
                    '-f', changes_file, 
                    '-m', temp_mount, 
                    '-s', str(new_size_mb),
                    '-d'  # Debug mode to run in foreground
                ]
                
                # Start dynfilefs process
                process = subprocess.Popen(mount_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                
                # Give it time to mount and resize
                import time
                time.sleep(2)
                
                # Check if virtual.dat was created/resized
                virtual_file = os.path.join(temp_mount, "virtual.dat")
                if not os.path.exists(virtual_file):
                    process.terminate()
                    return False, _("Failed to create/resize virtual.dat file")
                
                # Now we need to resize the filesystem inside virtual.dat
                # Resize the filesystem (will perform necessary checks)
                resize_cmd = ['resize2fs', '-f', virtual_file]
                resize_result = subprocess.run(resize_cmd, capture_output=True)
                
                if resize_result.returncode != 0:
                    process.terminate()
                    return False, _("Failed to resize filesystem: {}").format(resize_result.stderr.decode())
                
                # Terminate the dynfilefs process (unmount)
                process.terminate()
                process.wait()
                
            finally:
                # Clean up: ensure unmount and remove temp directory
                subprocess.run(['fusermount', '-u', temp_mount], capture_output=True)
                try:
                    os.rmdir(temp_mount)
                except:
                    pass
            
            # Update our session metadata with new size
            metadata["sessions"][session_id]["size"] = new_size_mb
            if not self._write_sessions_metadata(metadata):
                return False, _("Failed to update session metadata")
            
            return True, _("Session {} resized to {}MB successfully").format(session_id, new_size_mb)
            
        except Exception as e:
            return False, _("Failed to resize dynfilefs session: {}").format(str(e))

    def _resize_raw_session(self, session_path, new_size_mb, session_id, metadata):
        """Resize a raw session"""
        image_file = os.path.join(session_path, "changes.img")
        if not os.path.exists(image_file):
            return False, _("Raw image file not found")
        
        try:
            # Get current size
            current_size = os.path.getsize(image_file) // (1024 * 1024)
            if new_size_mb <= current_size:
                return False, _("New size must be larger than current size ({}MB)").format(current_size)
            
            # Truncate image file to new size
            new_size_bytes = new_size_mb * 1024 * 1024
            with open(image_file, 'r+b') as f:
                f.truncate(new_size_bytes)
            
            # Resize the filesystem inside the image (will perform necessary checks)
            resize_cmd = ['resize2fs', '-f', image_file]
            resize_result = subprocess.run(resize_cmd, capture_output=True)
            
            if resize_result.returncode != 0:
                return False, _("Failed to resize filesystem: {}").format(resize_result.stderr.decode())
            
            # Update metadata with new size
            metadata["sessions"][session_id]["size"] = new_size_mb
            if not self._write_sessions_metadata(metadata):
                return False, _("Failed to update session metadata")
            
            return True, _("Session {} resized to {}MB successfully").format(session_id, new_size_mb)
            
        except Exception as e:
            return False, _("Failed to resize raw session: {}").format(str(e))

    def export_session(self, session_id, output_path, verify=True):
        """Export session to TAR.ZSTD archive

        Args:
            session_id: ID of session to export
            output_path: Path to output file or directory
            verify: Verify archive after creation

        Returns:
            (success, message) tuple
        """
        # Get session info
        session_info = self._get_session_info(session_id)
        if not session_info:
            return False, _("Session #{} not found").format(session_id)

        session_path = os.path.join(self.sessions_dir, session_id)

        # Check if session is running
        running = self.get_running_session()
        if running and running['id'] == session_id:
            return False, _("Cannot export currently running session")

        # Generate filename if output_path is directory
        if os.path.isdir(output_path):
            timestamp = datetime.now().strftime('%Y%m%d-%H%M%S')
            filename = f"session-{session_id}-{timestamp}.tar.zst"
            output_file = os.path.join(output_path, filename)
        else:
            output_file = output_path
            # Ensure .tar.zst extension
            if not output_file.endswith('.tar.zst'):
                output_file += '.tar.zst'

        # Check free disk space (estimate: session size + 50% for compression overhead)
        session_size_mb = session_info.get('size', 0) / (1024 * 1024)
        estimated_archive_mb = session_size_mb * 1.5
        output_dir = os.path.dirname(output_file) if not os.path.isdir(output_path) else output_path
        has_space, space_error = self._check_free_space(output_dir, estimated_archive_mb)
        if not has_space:
            return False, space_error

        # Create temporary directory for metadata
        tmpdir = self._make_temp_dir()
        try:
            # Prepare metadata
            metadata = self._prepare_export_metadata(session_info)
            metadata_file = os.path.join(tmpdir, 'metadata.json')
            with open(metadata_file, 'w') as f:
                json.dump(metadata, f, indent=2)

            # Create human-readable info
            info_file = os.path.join(tmpdir, 'session.info')
            self._create_session_info_file(session_info, info_file)

            # Create TAR.ZSTD archive
            self._export_tar_zstd(session_path, output_file, tmpdir)

            # Verify if requested
            if verify:
                if not self._verify_export(output_file):
                    if os.path.exists(output_file):
                        os.remove(output_file)
                    return False, _("Export verification failed")

            file_size = os.path.getsize(output_file)
            return True, _("Session exported successfully to {} ({})").format(
                output_file, self._format_size(file_size))

        except Exception as e:
            # Clean up on error
            if os.path.exists(output_file):
                os.remove(output_file)
            return False, _("Export failed: {}").format(str(e))
        finally:
            # Clean up temporary directory
            shutil.rmtree(tmpdir, ignore_errors=True)

    def _prepare_export_metadata(self, session_info):
        """Prepare minimal export metadata

        Only essential information for compatibility checks and auto-sizing.
        """
        metadata = {
            "version": "1.0",
            "date": datetime.now().isoformat() + 'Z',
            "session": {
                "mode": session_info['mode'],
                "version": session_info['version'],
                "edition": session_info['edition'],
                "union": session_info['union'],
                "size": session_info['size']
            }
        }

        return metadata

    def _create_session_info_file(self, session_info, output_path):
        """Create human-readable session info file"""
        lines = [
            "MiniOS Session Archive",
            "=" * 40,
            "",
            f"Version: {session_info['version']}",
            f"Edition: {session_info['edition']}",
            f"Union FS: {session_info['union']}",
            f"Size: {self._format_size(session_info['size'])}",
            "",
            f"Exported: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        ]

        with open(output_path, 'w') as f:
            f.write('\n'.join(lines))

    def _export_tar_zstd(self, session_path, output_file, metadata_dir):
        """Export session as TAR.ZSTD archive with unified format

        All sessions are exported as raw file trees, regardless of original mode.
        This allows importing into any mode on any filesystem.
        """
        # Check if zstd is available
        try:
            subprocess.run(['which', 'zstd'], capture_output=True, check=True)
        except subprocess.CalledProcessError:
            raise Exception(_("zstd not found. Please install zstd package."))

        # Get session mode from metadata
        with open(os.path.join(metadata_dir, 'metadata.json'), 'r') as f:
            metadata = json.load(f)

        session_mode = metadata['session']['mode']

        # Extract session data to uniform format (raw file tree)
        tmpdir = self._make_temp_dir()
        try:
            extracted_path = self._extract_session_to_files(session_path, session_mode, tmpdir)

            # Create archive with uniform data
            cmd = [
                'tar', '-cf', output_file,
                '--use-compress-program=zstd -3 -T0',
                '-C', metadata_dir, 'metadata.json', 'session.info',
                '--transform', 's,^,data/,',
                '-C', extracted_path, '.'
            ]

            result = subprocess.run(cmd, capture_output=True)
            if result.returncode != 0:
                raise Exception(result.stderr.decode())
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def _extract_session_to_files(self, session_path, mode, tmpdir):
        """Extract session data to raw file tree

        Args:
            session_path: Path to session directory
            mode: Session mode (native/dynfilefs/raw)
            tmpdir: Temporary directory for extraction

        Returns:
            Path to extracted files
        """
        extract_path = os.path.join(tmpdir, 'files')
        os.makedirs(extract_path)

        if mode == 'native':
            # Already in raw format - just copy
            for item in os.listdir(session_path):
                src = os.path.join(session_path, item)
                dst = os.path.join(extract_path, item)
                if os.path.isdir(src):
                    shutil.copytree(src, dst, symlinks=True)
                else:
                    shutil.copy2(src, dst)
            return extract_path

        elif mode == 'dynfilefs':
            # Mount dynfilefs and extract files
            return self._extract_from_dynfilefs(session_path, extract_path)

        elif mode == 'raw':
            # Mount raw image and extract files
            return self._extract_from_raw(session_path, extract_path)

        else:
            raise Exception(_("Unknown session mode: {}").format(mode))

    def _extract_from_dynfilefs(self, session_path, extract_path):
        """Extract files from dynfilefs container"""
        changes_file = os.path.join(session_path, 'changes.dat')

        if not os.path.exists(changes_file):
            raise Exception(_("DynFileFS container not found"))

        # Check if dynfilefs is available
        try:
            subprocess.run(['which', 'dynfilefs'], capture_output=True, check=True)
        except subprocess.CalledProcessError:
            raise Exception(_("dynfilefs not found. Cannot extract dynfilefs session."))

        mount_point = self._make_temp_dir()
        virtual_mount = None
        process = None

        try:
            # Mount dynfilefs
            cmd = ['dynfilefs', '-f', changes_file, '-m', mount_point]
            process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            time.sleep(3)  # Wait for mount

            # Check if mounted
            virtual_file = os.path.join(mount_point, 'virtual.dat')
            if not os.path.exists(virtual_file):
                raise Exception(_("Failed to mount dynfilefs"))

            # Mount virtual file
            virtual_mount = self._make_temp_dir()
            result = subprocess.run(['mount', '-o', 'loop,ro', virtual_file, virtual_mount],
                                  capture_output=True)
            if result.returncode != 0:
                raise Exception(_("Failed to mount virtual file"))

            # Copy all files
            for item in os.listdir(virtual_mount):
                src = os.path.join(virtual_mount, item)
                dst = os.path.join(extract_path, item)
                if os.path.isdir(src):
                    shutil.copytree(src, dst, symlinks=True)
                else:
                    shutil.copy2(src, dst)

            return extract_path

        finally:
            # Cleanup
            if virtual_mount:
                subprocess.run(['umount', virtual_mount], capture_output=True)
                shutil.rmtree(virtual_mount, ignore_errors=True)
            if process:
                subprocess.run(['fusermount', '-u', mount_point], capture_output=True)
                process.terminate()
                process.wait(timeout=5)
            shutil.rmtree(mount_point, ignore_errors=True)

    def _extract_from_raw(self, session_path, extract_path):
        """Extract files from raw image"""
        image_file = os.path.join(session_path, 'changes.img')

        if not os.path.exists(image_file):
            raise Exception(_("Raw image not found"))

        mount_point = self._make_temp_dir()

        try:
            # Mount raw image
            result = subprocess.run(['mount', '-o', 'loop,ro', image_file, mount_point],
                                  capture_output=True)
            if result.returncode != 0:
                raise Exception(_("Failed to mount raw image"))

            # Copy all files
            for item in os.listdir(mount_point):
                src = os.path.join(mount_point, item)
                dst = os.path.join(extract_path, item)
                if os.path.isdir(src):
                    shutil.copytree(src, dst, symlinks=True)
                else:
                    shutil.copy2(src, dst)

            return extract_path

        finally:
            # Cleanup
            subprocess.run(['umount', mount_point], capture_output=True)
            shutil.rmtree(mount_point, ignore_errors=True)

    def _verify_export(self, archive_file):
        """Verify TAR.ZSTD archive integrity"""
        try:
            # Test archive integrity
            cmd = ['tar', '-tf', archive_file, '--use-compress-program=zstd -T0']
            result = subprocess.run(cmd, capture_output=True)

            if result.returncode != 0:
                return False

            # Verify metadata.json exists
            output = result.stdout.decode()
            if 'metadata.json' not in output or 'session.info' not in output:
                return False

            return True
        except Exception:
            return False

    def import_session(self, archive_path, auto_convert=False, force_mode=None,
                      verify=True, skip_compatibility_check=False):
        """Import session from TAR.ZSTD archive

        Args:
            archive_path: Path to archive file
            auto_convert: Automatically convert to compatible mode
            force_mode: Force specific mode (native/dynfilefs/raw)
            verify: Verify session integrity after import
            skip_compatibility_check: Skip compatibility checks

        Returns:
            (success, message) tuple
        """
        # Check archive exists
        if not os.path.exists(archive_path):
            return False, _("Archive file not found")

        # Detect and verify archive format
        if not archive_path.endswith('.tar.zst'):
            return False, _("Invalid archive format. Only .tar.zst files are supported.")

        tmpdir = self._make_temp_dir()
        try:
            # Extract metadata first
            metadata = self._extract_metadata(archive_path, tmpdir)
            if not metadata:
                return False, _("Invalid session archive: missing or corrupted metadata")

            # Compatibility checks
            if not skip_compatibility_check:
                compat_result = self._check_import_compatibility(metadata)
                if not compat_result['compatible']:
                    if not auto_convert and not force_mode:
                        return False, _("Incompatible session: {}").format(
                            ', '.join(compat_result['issues']))

            # Determine import mode
            import_mode = metadata['session']['mode']
            if force_mode:
                import_mode = force_mode
            elif auto_convert:
                import_mode = self._select_compatible_mode(metadata)

            # Check if conversion needed
            needs_conversion = import_mode != metadata['session']['mode']

            # Check free disk space
            session_size_bytes = metadata['session'].get('size', 4000 * 1024 * 1024)
            if isinstance(session_size_bytes, int) and session_size_bytes > 100000:
                required_mb = session_size_bytes / (1024 * 1024)
            else:
                required_mb = session_size_bytes if isinstance(session_size_bytes, int) else 4000
            has_space, space_error = self._check_free_space(self.sessions_dir, required_mb)
            if not has_space:
                return False, space_error

            # Find next available session ID
            new_id = self._get_next_session_id()
            session_path = os.path.join(self.sessions_dir, str(new_id))

            # Extract archive
            extract_path = os.path.join(tmpdir, 'extract')
            os.makedirs(extract_path)
            self._extract_archive(archive_path, extract_path)

            # Get data directory from extraction
            data_path = os.path.join(extract_path, 'data')

            # Check if there are any files/dirs besides metadata
            has_data = False
            for item in os.listdir(data_path):
                if item not in ['metadata.json', 'session.info']:
                    has_data = True
                    break

            # Use data_path as source (contains all session files)
            session_data = data_path if has_data else None

            # Handle empty sessions
            if not session_data:
                # Create empty session structure based on import_mode
                os.makedirs(session_path, exist_ok=True)
                if import_mode == 'native':
                    # Just empty directory
                    success = True
                elif import_mode in ['dynfilefs', 'raw']:
                    # Need to create empty container
                    # Create a temporary empty directory to use as source
                    empty_dir = self._make_temp_dir()
                    try:
                        size_mb = 1000  # Default size for empty sessions
                        if import_mode == 'dynfilefs':
                            success = self._import_to_dynfilefs(empty_dir, session_path, size_mb)
                        else:  # raw
                            success = self._import_to_raw(empty_dir, session_path, size_mb)
                    finally:
                        shutil.rmtree(empty_dir, ignore_errors=True)
                else:
                    success = True
            else:
                # Import/convert
                if needs_conversion:
                    success = self._import_with_conversion(
                        session_data, session_path,
                        metadata['session']['mode'], import_mode, metadata)
                else:
                    success = self._import_direct(session_data, session_path, import_mode, metadata)

            if not success:
                if os.path.exists(session_path):
                    shutil.rmtree(session_path)
                return False, _("Failed to import session data")

            # Create metadata entry
            self._create_session_metadata(new_id, import_mode, metadata)

            # Verify if requested
            if verify:
                if not self._verify_session_integrity(new_id):
                    shutil.rmtree(session_path)
                    return False, _("Imported session failed verification")

            return True, _("Session imported successfully as #{}").format(new_id)

        except Exception as e:
            return False, _("Import failed: {}").format(str(e))
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def _extract_metadata(self, archive_path, tmpdir):
        """Extract and return metadata from archive"""
        try:
            # Extract only metadata.json
            cmd = ['tar', '-xf', archive_path,
                   '--use-compress-program=zstd -T0',
                   '-C', tmpdir,
                   'data/metadata.json']
            result = subprocess.run(cmd, capture_output=True)

            if result.returncode != 0:
                return None

            # Read metadata
            metadata_file = os.path.join(tmpdir, 'data', 'metadata.json')
            with open(metadata_file, 'r') as f:
                return json.load(f)
        except Exception:
            return None

    def _extract_archive(self, archive_path, extract_path):
        """Extract full archive"""
        cmd = ['tar', '-xf', archive_path,
               '--use-compress-program=zstd -T0',
               '-C', extract_path]
        result = subprocess.run(cmd, capture_output=True)
        if result.returncode != 0:
            raise Exception(result.stderr.decode())

    def _check_import_compatibility(self, metadata):
        """Check if imported session is compatible

        With unified export format, we only check version/edition/union.
        Mode compatibility is no longer checked since data is always raw files.
        """
        issues = []

        # Get current system info
        current_version = self._get_system_version()
        current_edition = self._get_system_edition()
        current_union = self._get_current_union_fs()

        session = metadata['session']

        # Check version (warning only, not blocking)
        if session['version'] != current_version:
            issues.append(_("Version mismatch: {} → {}").format(
                session['version'], current_version))

        # Check edition (warning only, not blocking)
        if session['edition'] != current_edition:
            issues.append(_("Edition mismatch: {} → {}").format(
                session['edition'], current_edition))

        # Check union FS (warning only, not blocking)
        if session['union'] != current_union:
            issues.append(_("Union FS mismatch: {} → {}").format(
                session['union'], current_union))

        # Note: Mode compatibility is NOT checked anymore
        # Archive contains raw files that can be imported as any mode

        return {
            'compatible': len(issues) == 0,
            'issues': issues,
            'warnings': len(issues) > 0
        }

    def _select_compatible_mode(self, metadata):
        """Select compatible mode for import"""
        fs_info, _err = self._detect_filesystem_type()
        if not fs_info:
            return 'native'

        compatible_modes = self._get_compatible_session_modes(fs_info)
        original_mode = metadata['session']['mode']

        # Try to keep original mode if compatible
        if original_mode in compatible_modes:
            return original_mode

        # Otherwise pick first compatible mode
        if compatible_modes:
            return compatible_modes[0]

        return 'native'

    def _import_direct(self, source_path, target_path, target_mode, metadata):
        """Import session data (unified format - always raw files)

        Since export creates unified format (raw file tree),
        this method creates the session in the target mode.
        """
        # Get size from metadata if available
        size_mb = None
        if target_mode in ['dynfilefs', 'raw']:
            size_bytes = metadata['session'].get('size', 4000 * 1024 * 1024)
            # Convert bytes to MB
            if isinstance(size_bytes, int) and size_bytes > 100000:
                size_mb = max(100, int(size_bytes / (1024 * 1024)))
            elif isinstance(size_bytes, str):
                try:
                    size_mb = int(size_bytes)
                except (ValueError, TypeError):
                    size_mb = 1000
            else:
                size_mb = int(size_bytes) if size_bytes > 100 else 4000

        return self._import_files_to_mode(source_path, target_path, target_mode, size_mb)

    def _import_with_conversion(self, source_path, target_path, source_mode,
                                target_mode, metadata):
        """Import with mode conversion (unified format)

        Note: source_mode is ignored since archive always contains raw files.
        """
        size_bytes = metadata['session'].get('size', 4000 * 1024 * 1024)

        # Convert bytes to MB
        if isinstance(size_bytes, int) and size_bytes > 100000:
            # If it's a large number, assume it's in bytes
            size_mb = max(100, int(size_bytes / (1024 * 1024)))
        elif isinstance(size_bytes, str):
            try:
                size_mb = int(size_bytes)
            except (ValueError, TypeError):
                size_mb = 1000
        else:
            # Small number, might already be MB or default
            size_mb = int(size_bytes) if size_bytes > 100 else 4000

        return self._import_files_to_mode(source_path, target_path, target_mode, size_mb)

    def _import_files_to_mode(self, files_path, target_path, target_mode, size_mb=None):
        """Import raw file tree into specified mode

        Args:
            files_path: Path to raw file tree
            target_path: Target session directory
            target_mode: Target mode (native/dynfilefs/raw)
            size_mb: Size for container-based modes

        Returns:
            True on success, False on failure
        """
        try:
            os.makedirs(target_path, exist_ok=True)

            if target_mode == 'native':
                # Direct copy (skip metadata files)
                for item in os.listdir(files_path):
                    if item in ['metadata.json', 'session.info']:
                        continue
                    src = os.path.join(files_path, item)
                    dst = os.path.join(target_path, item)
                    if os.path.isdir(src):
                        shutil.copytree(src, dst, symlinks=True)
                    else:
                        shutil.copy2(src, dst)
                return True

            elif target_mode == 'dynfilefs':
                # Create dynfilefs container and populate
                return self._import_to_dynfilefs(files_path, target_path, size_mb or 4000)

            elif target_mode == 'raw':
                # Create raw image and populate
                return self._import_to_raw(files_path, target_path, size_mb or 4000)

            else:
                return False

        except Exception:
            return False

    def _import_to_dynfilefs(self, files_path, target_path, size_mb):
        """Import files into dynfilefs container"""
        # Check if dynfilefs is available
        try:
            subprocess.run(['which', 'dynfilefs'], capture_output=True, check=True)
        except subprocess.CalledProcessError:
            raise Exception(_("dynfilefs not found. Cannot create dynfilefs session."))

        changes_file = os.path.join(target_path, 'changes.dat')
        mount_point = self._make_temp_dir()
        virtual_mount = None
        process = None

        try:
            # Create and mount dynfilefs
            cmd = ['dynfilefs', '-f', changes_file, '-m', mount_point,
                   '-s', str(size_mb), '-p', '4000']
            process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            time.sleep(2)  # Wait for mount

            # Check virtual file
            virtual_file = os.path.join(mount_point, 'virtual.dat')
            if not os.path.exists(virtual_file):
                raise Exception(_("Failed to create dynfilefs"))

            # Format virtual file
            result = subprocess.run(['mke2fs', '-F', '-t', 'ext4', virtual_file],
                                  capture_output=True)
            # Sync to ensure filesystem is written (important for FAT32/NTFS)
            subprocess.run(['sync'], capture_output=True)
            if result.returncode != 0:
                raise Exception(_("Failed to format dynfilefs"))

            # Mount virtual file
            virtual_mount = self._make_temp_dir()
            result = subprocess.run(['mount', '-o', 'loop', virtual_file, virtual_mount],
                                  capture_output=True)
            if result.returncode != 0:
                raise Exception(_("Failed to mount virtual file"))

            # Copy all files (skip metadata files)
            for item in os.listdir(files_path):
                if item in ['metadata.json', 'session.info']:
                    continue
                src = os.path.join(files_path, item)
                dst = os.path.join(virtual_mount, item)
                # Skip existing files (like lost+found created by mkfs)
                if os.path.exists(dst):
                    continue
                if os.path.isdir(src):
                    shutil.copytree(src, dst, symlinks=True)
                else:
                    shutil.copy2(src, dst)

            return True

        finally:
            # Cleanup
            if virtual_mount:
                subprocess.run(['umount', virtual_mount], capture_output=True)
                shutil.rmtree(virtual_mount, ignore_errors=True)
            if process:
                subprocess.run(['fusermount', '-u', mount_point], capture_output=True)
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
            shutil.rmtree(mount_point, ignore_errors=True)

    def _import_to_raw(self, files_path, target_path, size_mb):
        """Import files into raw image"""
        image_file = os.path.join(target_path, 'changes.img')

        # Create image with fallocate
        try:
            size_bytes = size_mb * 1024 * 1024
            result = subprocess.run(['fallocate', '-l', str(size_bytes), image_file],
                                  capture_output=True)
            if result.returncode != 0:
                # Fallback to truncate if fallocate not available
                with open(image_file, 'wb') as f:
                    f.truncate(size_bytes)
        except Exception as e:
            raise Exception(_("Failed to create raw image: {}").format(str(e)))

        # Format with ext4
        result = subprocess.run(['mke2fs', '-F', '-t', 'ext4', image_file],
                              capture_output=True)
        # Sync to ensure filesystem is written (important for FAT32/NTFS)
        subprocess.run(['sync'], capture_output=True)
        if result.returncode != 0:
            raise Exception(_("Failed to format raw image"))

        mount_point = self._make_temp_dir()

        try:
            # Mount image
            result = subprocess.run(['mount', '-o', 'loop', image_file, mount_point],
                                  capture_output=True)
            if result.returncode != 0:
                raise Exception(_("Failed to mount raw image"))

            # Copy all files (skip metadata files)
            for item in os.listdir(files_path):
                if item in ['metadata.json', 'session.info']:
                    continue
                src = os.path.join(files_path, item)
                dst = os.path.join(mount_point, item)
                # Skip existing files (like lost+found created by mkfs)
                if os.path.exists(dst):
                    continue
                if os.path.isdir(src):
                    shutil.copytree(src, dst, symlinks=True)
                else:
                    shutil.copy2(src, dst)

            return True

        finally:
            # Cleanup
            subprocess.run(['umount', mount_point], capture_output=True)
            shutil.rmtree(mount_point, ignore_errors=True)

    def _get_next_session_id(self):
        """Get next available session ID"""
        existing_ids = []
        for item in os.listdir(self.sessions_dir):
            if item.isdigit():
                existing_ids.append(int(item))

        if not existing_ids:
            return 1

        return max(existing_ids) + 1

    def _create_session_metadata(self, session_id, mode, import_metadata):
        """Create metadata entry for imported session"""
        metadata = self._read_sessions_metadata()

        session_data = {
            'mode': mode,
            'version': import_metadata['session']['version'],
            'edition': import_metadata['session']['edition'],
            'union': import_metadata['session']['union']
        }

        if mode in ['dynfilefs', 'raw']:
            size = import_metadata['session'].get('size', 4000)
            if isinstance(size, int):
                # Size in metadata is in bytes, convert to MB for storage
                if size > 100000:  # If > 100KB, assume it's in bytes
                    size_mb = max(100, int(size / (1024 * 1024)))
                    session_data['size'] = size_mb
                elif size > 0:
                    # Already in MB
                    session_data['size'] = size
                else:
                    # Zero or invalid, use default
                    session_data['size'] = 4000

        metadata['sessions'][str(session_id)] = session_data
        self._write_sessions_metadata(metadata)

    def _verify_session_integrity(self, session_id):
        """Verify session integrity"""
        session_path = os.path.join(self.sessions_dir, str(session_id))
        return os.path.exists(session_path) and os.path.isdir(session_path)

    def copy_session(self, session_id, to_mode=None, size_mb=None):
        """Copy session, optionally converting mode

        Args:
            session_id: Source session ID
            to_mode: Target mode (None = same as source)
            size_mb: Size for dynfilefs/raw modes

        Returns:
            (success, message) tuple
        """
        # Get source session
        source_session = self._get_session_info(session_id)
        if not source_session:
            return False, _("Source session not found")

        source_path = os.path.join(self.sessions_dir, session_id)
        source_mode = source_session['mode']

        # Determine target mode
        target_mode = to_mode if to_mode else source_mode

        # Check if running
        running = self.get_running_session()
        if running and running['id'] == session_id:
            return False, _("Cannot copy currently running session")

        # Check free disk space
        source_size = source_session.get('size', 0)
        required_mb = size_mb if size_mb else (source_size / (1024 * 1024) if source_size > 100000 else 4000)
        has_space, space_error = self._check_free_space(self.sessions_dir, required_mb)
        if not has_space:
            return False, space_error

        # Get next session ID
        new_id = self._get_next_session_id()
        target_path = os.path.join(self.sessions_dir, str(new_id))

        try:
            os.makedirs(target_path, exist_ok=True)

            if source_mode == target_mode:
                # Direct copy
                success = self._copy_session_direct(source_path, target_path, source_mode)
            else:
                # Copy with conversion
                success = self._copy_session_with_conversion(
                    source_path, target_path, source_mode, target_mode, size_mb)

            if not success:
                if os.path.exists(target_path):
                    shutil.rmtree(target_path)
                return False, _("Failed to copy session data")

            # Create metadata for new session
            metadata = self._read_sessions_metadata()
            metadata['sessions'][str(new_id)] = {
                'mode': target_mode,
                'version': source_session['version'],
                'edition': source_session['edition'],
                'union': source_session['union']
            }

            if target_mode in ['dynfilefs', 'raw']:
                if size_mb:
                    metadata['sessions'][str(new_id)]['size'] = size_mb
                elif source_mode == target_mode and source_session.get('total_size_mb'):
                    # Copy size from source session when doing direct copy
                    metadata['sessions'][str(new_id)]['size'] = source_session['total_size_mb']

            self._write_sessions_metadata(metadata)

            return True, _("Session copied successfully to #{}").format(new_id)

        except Exception as e:
            if os.path.exists(target_path):
                shutil.rmtree(target_path)
            return False, _("Copy failed: {}").format(str(e))

    def _copy_session_direct(self, source_path, target_path, mode):
        """Direct copy of session without conversion"""
        try:
            if mode == 'native':
                # Copy directory tree
                for item in os.listdir(source_path):
                    src = os.path.join(source_path, item)
                    dst = os.path.join(target_path, item)
                    if os.path.isdir(src):
                        shutil.copytree(src, dst, symlinks=True)
                    else:
                        shutil.copy2(src, dst)
                return True

            elif mode == 'dynfilefs':
                # Copy changes.dat files
                for file in os.listdir(source_path):
                    if file.startswith('changes.dat'):
                        shutil.copy2(
                            os.path.join(source_path, file),
                            os.path.join(target_path, file))
                return True

            elif mode == 'raw':
                # Copy changes.img
                shutil.copy2(
                    os.path.join(source_path, 'changes.img'),
                    os.path.join(target_path, 'changes.img'))
                return True

            return False
        except Exception:
            return False

    def _copy_session_with_conversion(self, source_path, target_path,
                                      source_mode, target_mode, size_mb=None):
        """Copy session with mode conversion"""
        tmpdir = self._make_temp_dir()
        try:
            # Extract files from source
            extracted_path = self._extract_session_to_files(source_path, source_mode, tmpdir)

            # Build target structure
            success = self._build_session_structure(extracted_path, target_path,
                                                    target_mode, size_mb)
            return success

        except Exception:
            return False
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def convert_session(self, session_id, target_mode, size_mb=None,
                       in_place=True):
        """Convert session storage mode

        Args:
            session_id: Session ID to convert
            target_mode: Target mode (native/dynfilefs/raw)
            size_mb: Size for dynfilefs/raw modes
            in_place: Convert in-place (vs create new session)

        Returns:
            (success, message) tuple
        """
        # Get session
        session_info = self._get_session_info(session_id)
        if not session_info:
            return False, _("Session not found")

        source_mode = session_info['mode']

        # Check if conversion needed
        if source_mode == target_mode:
            return False, _("Session is already in {} mode").format(target_mode)

        # Check if session is running
        running = self.get_running_session()
        if running and running['id'] == session_id:
            return False, _("Cannot convert currently running session")

        # Check if session is active
        current = self.get_current_session()
        if current and current['id'] == session_id:
            return False, _("Cannot convert currently active session. "
                           "Please activate another session first.")

        # Check target mode compatibility
        fs_info, _err = self._detect_filesystem_type()
        if fs_info:
            compatible_modes = self._get_compatible_session_modes(fs_info)
            if target_mode not in compatible_modes:
                return False, _("Target mode '{}' is not compatible with "
                               "current filesystem").format(target_mode)

        # Set default size for dynfilefs/raw if not specified
        if target_mode in ['dynfilefs', 'raw'] and not size_mb:
            # Try to use size from source session if available
            if session_info.get('total_size_mb'):
                size_mb = session_info['total_size_mb']
            elif source_mode == 'native' and session_info.get('size'):
                # For native sessions, use actual size + 100 MB
                size_mb = int(session_info['size'] / (1024 * 1024)) + 100
            else:
                size_mb = 1000

        session_path = os.path.join(self.sessions_dir, session_id)

        # Create new session with temporary name
        new_session_path = self._make_temp_dir()

        try:
            # Extract files and build new structure in temp location
            tmpdir = self._make_temp_dir()
            try:
                extracted_path = self._extract_session_to_files(session_path, source_mode, tmpdir)

                # Build new session structure
                try:
                    success = self._build_session_structure(extracted_path, new_session_path,
                                                            target_mode, size_mb)

                    if not success:
                        return False, _("Failed to convert session structure")
                except Exception as build_err:
                    return False, _("Build session error: {}").format(str(build_err))

            finally:
                # Clean up extraction temp directory
                shutil.rmtree(tmpdir, ignore_errors=True)

            # Conversion successful - replace old session with new
            shutil.rmtree(session_path, ignore_errors=True)
            shutil.move(new_session_path, session_path)

            # Update metadata
            metadata = self._read_sessions_metadata()
            if session_id in metadata.get('sessions', {}):
                metadata['sessions'][session_id]['mode'] = target_mode
                if target_mode in ['dynfilefs', 'raw'] and size_mb:
                    metadata['sessions'][session_id]['size'] = size_mb
                self._write_sessions_metadata(metadata)

            return True, _("Session converted successfully from {} to {}").format(
                source_mode, target_mode)

        except Exception as e:
            # Clean up temp new session on error
            shutil.rmtree(new_session_path, ignore_errors=True)
            return False, _("Conversion failed: {}").format(str(e))

        except Exception as e:
            return False, _("Conversion error: {}").format(str(e))
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def _build_session_structure(self, source_files_path, target_path, target_mode, size_mb=None):
        """Build session structure in target mode

        Args:
            source_files_path: Path to extracted session files
            target_path: Path where to create new session
            target_mode: Target mode (native/dynfilefs/raw)
            size_mb: Size for dynfilefs/raw modes

        Returns:
            True on success, False on failure
        """
        try:
            if target_mode == 'native':
                # Copy files directly to target
                for item in os.listdir(source_files_path):
                    src = os.path.join(source_files_path, item)
                    dst = os.path.join(target_path, item)
                    if os.path.isdir(src):
                        shutil.copytree(src, dst, symlinks=True)
                    else:
                        shutil.copy2(src, dst)
                return True

            elif target_mode == 'dynfilefs':
                # Create dynfilefs container and copy files
                if not self._check_dynfilefs_available():
                    return False

                changes_file = os.path.join(target_path, 'changes.dat')
                size_mb_value = size_mb if size_mb else 4000

                # Mount dynfilefs (will create if doesn't exist)
                mount_point = self._make_temp_dir()
                virtual_mount = None
                process = None
                success = False
                try:
                    # Mount dynfilefs with size
                    cmd = ['dynfilefs', '-f', changes_file, '-m', mount_point, '-s', str(size_mb_value)]
                    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    time.sleep(3)

                    # Check if mounted
                    virtual_file = os.path.join(mount_point, 'virtual.dat')
                    if not os.path.exists(virtual_file):
                        return False

                    # Format virtual.dat as ext4
                    result = subprocess.run(
                        ['mkfs.ext4', '-F', virtual_file],
                        capture_output=True
                    )
                    # Sync to ensure filesystem is written (important for FAT32/NTFS)
                    subprocess.run(['sync'], capture_output=True)
                    if result.returncode != 0:
                        return False

                    # Mount virtual.dat
                    virtual_mount = self._make_temp_dir()
                    result = subprocess.run(
                        ['mount', '-o', 'loop', virtual_file, virtual_mount],
                        capture_output=True
                    )
                    if result.returncode != 0:
                        return False

                    # Copy files
                    for item in os.listdir(source_files_path):
                        src = os.path.join(source_files_path, item)
                        dst = os.path.join(virtual_mount, item)
                        # Skip if destination already exists (e.g., lost+found)
                        if os.path.exists(dst):
                            continue
                        if os.path.isdir(src):
                            shutil.copytree(src, dst, symlinks=True)
                        else:
                            shutil.copy2(src, dst)

                    # Success!
                    success = True

                finally:
                    # Cleanup mounts
                    if virtual_mount:
                        subprocess.run(['umount', virtual_mount], capture_output=True)
                        try:
                            os.rmdir(virtual_mount)
                        except:
                            pass
                    if process:
                        process.terminate()
                        try:
                            process.wait(timeout=5)
                        except:
                            process.kill()
                    if mount_point:
                        # Wait a bit for FUSE to release
                        time.sleep(1)
                        try:
                            os.rmdir(mount_point)
                        except:
                            pass

                return success

            elif target_mode == 'raw':
                # Create raw image and copy files
                image_file = os.path.join(target_path, 'changes.img')
                # Ensure size_mb is an integer
                if size_mb:
                    size_mb = int(size_mb) if not isinstance(size_mb, int) else size_mb
                    size_bytes = size_mb * 1024 * 1024
                else:
                    size_bytes = 1000 * 1024 * 1024

                # Create image file with fallocate
                result = subprocess.run(['fallocate', '-l', str(size_bytes), image_file],
                                      capture_output=True)
                if result.returncode != 0:
                    # Fallback to truncate if fallocate not available
                    with open(image_file, 'wb') as f:
                        f.truncate(size_bytes)

                # Format as ext4
                result = subprocess.run(
                    ['mkfs.ext4', '-F', image_file],
                    capture_output=True
                )
                # Sync to ensure filesystem is written (important for FAT32/NTFS)
                subprocess.run(['sync'], capture_output=True)
                if result.returncode != 0:
                    return False

                # Mount and copy files
                mount_point = self._make_temp_dir()
                try:
                    result = subprocess.run(
                        ['mount', '-o', 'loop', image_file, mount_point],
                        capture_output=True
                    )
                    if result.returncode != 0:
                        return False

                    # Copy files
                    for item in os.listdir(source_files_path):
                        src = os.path.join(source_files_path, item)
                        dst = os.path.join(mount_point, item)
                        # Skip if destination already exists (e.g., lost+found)
                        if os.path.exists(dst):
                            continue
                        if os.path.isdir(src):
                            shutil.copytree(src, dst, symlinks=True)
                        else:
                            shutil.copy2(src, dst)

                    return True

                finally:
                    subprocess.run(['umount', mount_point], capture_output=True)
                    try:
                        os.rmdir(mount_point)
                    except:
                        pass

            return False

        except Exception:
            return False

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

    for session in sessions:
        status_parts = []
        if session['is_default']:
            status_parts.append(_("ACTIVE"))
        if session.get('is_running', False):
            status_parts.append(_("RUNNING"))
        
        status = f" ({', '.join(status_parts)})" if status_parts else ""
        modified_str = session['modified'].strftime("%Y-%m-%d %H:%M:%S") if session['modified'] else "unknown"
        size_str = SessionManager()._format_size(session['size'])
        
        lines.append(f"{_('Session')} #{session['id']}{status}")
        lines.append(f"  {_('Mode:').rstrip(':')} {session['mode']}")
        lines.append(f"  {_('Version:').rstrip(':')} {session['version']}")
        lines.append(f"  {_('Edition:').rstrip(':')} {session['edition']}")
        lines.append(f"  {_('Union FS:').rstrip(':')} {session['union']}")
        lines.append(f"  {_('Size:').rstrip(':')} {size_str}")
        
        # Add Total Size for dynfilefs sessions
        if session['mode'] == 'dynfilefs' and 'total_size_mb' in session and session['total_size_mb']:
            total_size_mb = session['total_size_mb']
            # Convert to int if it's a string
            if isinstance(total_size_mb, str):
                try:
                    total_size_mb = int(total_size_mb)
                except (ValueError, TypeError):
                    total_size_mb = 0
            
            if total_size_mb > 0:
                total_size_str = SessionManager()._format_size(total_size_mb * 1024 * 1024)
                lines.append(f"  {_('Total Size:').rstrip(':')} {total_size_str}")
        
        lines.append(f"  {_('Last Modified:').rstrip(':')} {modified_str}")
        lines.append("")

    return "\n".join(lines)

def format_sessions_json(sessions):
    """Format session list as JSON"""
    json_sessions = []
    for session in sessions:
        json_session = {
            'id': session['id'],
            'mode': session['mode'],
            'version': session['version'],
            'edition': session['edition'],
            'union': session['union'],
            'size': session['size'],
            'size_formatted': SessionManager()._format_size(session['size'])
        }
        
        # Add total_size fields right after size_formatted for dynfilefs sessions
        if session['mode'] == 'dynfilefs' and 'total_size_mb' in session and session['total_size_mb']:
            total_size_mb = session['total_size_mb']
            if isinstance(total_size_mb, str):
                try:
                    total_size_mb = int(total_size_mb)
                except (ValueError, TypeError):
                    total_size_mb = 0
            
            if total_size_mb > 0:
                total_size_bytes = total_size_mb * 1024 * 1024
                json_session['total_size'] = total_size_bytes
                json_session['total_size_formatted'] = SessionManager()._format_size(total_size_bytes)
        
        # Continue with remaining fields
        json_session.update({
            'modified': session['modified'].isoformat() if session['modified'] else None,
            'path': session['path'],
            'is_default': session['is_default'],
            'is_running': session.get('is_running', False)
        })

        # Add status if present (for running sessions)
        if 'status' in session:
            json_session['status'] = session['status']
        json_sessions.append(json_session)

    return json.dumps(json_sessions, indent=2, ensure_ascii=False)

def format_session_json(session):
    """Format single session as JSON"""
    if not session:
        return json.dumps(None)

    json_session = {
        'id': session['id'],
        'mode': session['mode'],
        'version': session['version'],
        'edition': session['edition'],
        'union': session['union'],
        'size': session['size'],
        'size_formatted': SessionManager()._format_size(session['size'])
    }

    # Add total_size fields right after size_formatted for dynfilefs sessions
    if session['mode'] == 'dynfilefs' and 'total_size_mb' in session and session['total_size_mb']:
        total_size_mb = session['total_size_mb']
        if isinstance(total_size_mb, str):
            try:
                total_size_mb = int(total_size_mb)
            except (ValueError, TypeError):
                total_size_mb = 0
        
        if total_size_mb > 0:
            total_size_bytes = total_size_mb * 1024 * 1024
            json_session['total_size'] = total_size_bytes
            json_session['total_size_formatted'] = SessionManager()._format_size(total_size_bytes)

    # Continue with remaining fields
    json_session.update({
        'modified': session['modified'].isoformat() if session['modified'] else None,
        'path': session['path'],
        'is_default': session['is_default']
    })

    # Add status if present (for running sessions)
    if 'status' in session:
        json_session['status'] = session['status']

    return json.dumps(json_session, indent=2, ensure_ascii=False)

def format_filesystem_info_json(fs_info):
    """Format filesystem info as JSON"""
    if not fs_info:
        return json.dumps({'error': 'Filesystem information not available'})

    return json.dumps(fs_info, indent=2, ensure_ascii=False)



def main():
    """Main application entry point"""
    import sys

    # Pre-check for --json flag before parsing
    json_output = '--json' in sys.argv

    # Check for root privileges
    if os.geteuid() != 0:
        error_msg = _("This tool requires root privileges. Please run with sudo or through pkexec.")
        if json_output:
            print(json.dumps({"success": False, "error": error_msg}), file=sys.stderr)
        else:
            print(error_msg, file=sys.stderr)
        sys.exit(1)

    parser = argparse.ArgumentParser(
        description=_('MiniOS Session Manager - Command line tool for managing persistent sessions'),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_("""
GLOBAL OPTIONS:
  --json                    Output results in JSON format (can be used with any command)
  --sessions-dir PATH       Use custom sessions directory instead of default

COMMANDS:
  list                      List all available sessions with detailed information
  active                    Show currently active session (will boot next)
  running                   Show currently running session (current boot)
  info                      Show filesystem type and compatible session modes
  activate SESSION_ID       Activate specified session (required: session_id)
  create [OPTIONS]          Create new session (optional: --mode, --size)
  delete SESSION_ID         Delete specified session (required: session_id)
  cleanup [OPTIONS]         Delete old sessions (optional: --days, default: 30)
  status                    Check sessions directory status and permissions
  resize SESSION_ID SIZE    Resize session to new size in MB (dynfilefs/raw only)
  export SESSION_ID PATH    Export session to .tar.zst archive
  import ARCHIVE            Import session from .tar.zst archive
  copy SESSION_ID           Copy session (optional: --to-mode, --size)
  convert SESSION_ID MODE   Convert session to different mode (optional: --size)

SESSION MODES:
  native                    Direct filesystem changes (requires POSIX-compatible filesystem)
  dynfilefs                 Dynamic file system overlay (works on any filesystem, 1000MB default)
  raw                       Raw disk image (works on any filesystem, custom size required)

COMMAND BEHAVIOR:
  • create without --mode: Uses native mode (may fail on FAT32/NTFS/exFAT)
  • create without --size: Uses 1000MB for dynfilefs/raw modes
  • cleanup without --days: Uses 30-day threshold for deletion
  • cleanup protects both active and running sessions from deletion

EXAMPLES:

  Basic Usage:
    minios-session list                           List all available sessions
    minios-session active                         Show which session will boot next
    minios-session running                        Show currently running session
    minios-session info                           Show filesystem compatibility info

  Session Management:
    minios-session activate 2                     Set session #2 as default for next boot
    minios-session delete 3                       Delete session #3 permanently
    minios-session cleanup --days 30              Delete sessions older than 30 days
    minios-session cleanup                        Delete sessions older than 30 days (default)

  Creating Sessions:
    minios-session create --mode native           Create native session (filesystem changes)
    minios-session create --mode dynfilefs        Create 1000MB dynfilefs session (default)
    minios-session create --mode dynfilefs --size 8000   Create 8000MB dynfilefs session
    minios-session create --mode raw --size 2000         Create 2000MB raw disk image

  Session Operations:
    minios-session resize 2 4000                  Resize session #2 to 4000MB
    minios-session export 3 /tmp/backup.tar.zst   Export session #3 to archive
    minios-session import /tmp/backup.tar.zst     Import session from archive
    minios-session copy 2                         Copy session #2
    minios-session copy 2 --to-mode raw --size 3000  Copy session #2 as raw with 3000MB
    minios-session convert 3 dynfilefs --size 2000   Convert session #3 to dynfilefs with 2000MB

  Error Handling:
    minios-session create                         May fail on FAT32/NTFS/exFAT: "Use dynfilefs or raw mode"
    minios-session create --mode raw --size 5000  Will fail on FAT32: file size limit is 4096MB (4GB)

  JSON Output (for automation):
    minios-session --json list                    List sessions in JSON format
    minios-session active --json                  Get active session info as JSON
    minios-session info --json                    Get system info as JSON

  Custom Session Directory:
    minios-session --sessions-dir /mnt/usb/sessions list
    minios-session --sessions-dir /tmp/test create --mode native

        """)
    )

    # Add global flags that can be used anywhere
    parser.add_argument('--json', action='store_true', help=_('Output in JSON format'))
    parser.add_argument('--sessions-dir', type=str, metavar='PATH', 
                       help=_('Custom path to sessions directory'))

    subparsers = parser.add_subparsers(dest='command', help=_('Available commands'))

    # Create parent parser with common arguments
    parent_parser = argparse.ArgumentParser(add_help=False)
    parent_parser.add_argument('--json', action='store_true', help=_('Output in JSON format'))
    parent_parser.add_argument('--sessions-dir', type=str, metavar='PATH', 
                              help=_('Custom path to sessions directory'))

    # List command
    list_parser = subparsers.add_parser('list', help=_('List all sessions'), parents=[parent_parser])

    # Active command (renamed from current for GUI consistency)
    active_parser = subparsers.add_parser('active', help=_('Show active session'), parents=[parent_parser])

    # Running command
    running_parser = subparsers.add_parser('running', help=_('Show running session'), parents=[parent_parser])

    # Info command
    info_parser = subparsers.add_parser('info', help=_('Show filesystem and compatibility information'), parents=[parent_parser])

    # Activate command
    activate_parser = subparsers.add_parser('activate', help=_('Activate a session'), parents=[parent_parser])
    activate_parser.add_argument('session_id', help=_('Session ID to activate'))

    # Create command
    create_parser = subparsers.add_parser('create', help=_('Create a new session'), parents=[parent_parser])
    create_parser.add_argument('mode', nargs='?', choices=['native', 'dynfilefs', 'raw'], 
                              default='native', help=_('Session mode (default: native)'))
    create_parser.add_argument('size', nargs='?', type=int, metavar='MB',
                              help=_('Size in MB for dynfilefs/raw modes (default: 1000)'))

    # Delete command
    delete_parser = subparsers.add_parser('delete', help=_('Delete a session'), parents=[parent_parser])
    delete_parser.add_argument('session_id', help=_('Session ID to delete'))

    # Cleanup command
    cleanup_parser = subparsers.add_parser('cleanup', help=_('Clean up old sessions'), parents=[parent_parser])
    cleanup_parser.add_argument('--days', type=int, default=30, 
                               help=_('Delete sessions older than N days (default: 30)'))

    # Status command
    status_parser = subparsers.add_parser('status', help=_('Check sessions directory status'), parents=[parent_parser])

    # Resize command
    resize_parser = subparsers.add_parser('resize', help=_('Resize a session'), parents=[parent_parser])
    resize_parser.add_argument('session_id', help=_('Session ID to resize'))
    resize_parser.add_argument('size', type=int, metavar='MB', help=_('New size in MB'))

    # Export command
    export_parser = subparsers.add_parser('export', help=_('Export session to archive'), parents=[parent_parser])
    export_parser.add_argument('session_id', help=_('Session ID to export'))
    export_parser.add_argument('output_path', help=_('Output file or directory path'))
    export_parser.add_argument('--no-verify', action='store_true', help=_('Skip archive verification'))

    # Import command
    import_parser = subparsers.add_parser('import', help=_('Import session from archive'), parents=[parent_parser])
    import_parser.add_argument('archive_path', help=_('Path to session archive (.tar.zst)'))
    import_parser.add_argument('--auto-convert', action='store_true',
                               help=_('Automatically convert to compatible mode'))
    import_parser.add_argument('--force-mode', choices=['native', 'dynfilefs', 'raw'],
                               help=_('Force specific session mode'))
    import_parser.add_argument('--no-verify', action='store_true', help=_('Skip integrity verification'))
    import_parser.add_argument('--skip-compatibility-check', action='store_true',
                               help=_('Skip compatibility checks'))

    # Copy command
    copy_parser = subparsers.add_parser('copy', help=_('Copy a session'), parents=[parent_parser])
    copy_parser.add_argument('session_id', help=_('Session ID to copy'))
    copy_parser.add_argument('--to-mode', choices=['native', 'dynfilefs', 'raw'],
                            help=_('Convert to different mode (optional)'))
    copy_parser.add_argument('--size', type=int, metavar='MB',
                            help=_('Size for new session (for dynfilefs/raw)'))

    # Convert command
    convert_parser = subparsers.add_parser('convert', help=_('Convert session mode'), parents=[parent_parser])
    convert_parser.add_argument('session_id', help=_('Session ID to convert'))
    convert_parser.add_argument('target_mode', choices=['native', 'dynfilefs', 'raw'],
                               help=_('Target mode'))
    convert_parser.add_argument('--size', type=int, metavar='MB',
                               help=_('Size for dynfilefs/raw mode'))
    convert_parser.add_argument('--new-session', action='store_true',
                               help=_('Create new session instead of in-place conversion'))

# GUI command removed - use minios-session-manager for GUI

    # Parse arguments - handle global flags that can appear anywhere
    # Extract global flags from any position
    global_json = '--json' in sys.argv
    sessions_dir = None

    # Find sessions-dir parameter
    for i, arg in enumerate(sys.argv):
        if arg == '--sessions-dir' and i + 1 < len(sys.argv):
            sessions_dir = sys.argv[i + 1]
            break
        elif arg.startswith('--sessions-dir='):
            sessions_dir = arg.split('=', 1)[1]
            break

    # Parse normally  
    args = parser.parse_args()

    # Apply global flags
    if global_json:
        args.json = True

    if sessions_dir and not hasattr(args, 'sessions_dir'):
        args.sessions_dir = sessions_dir
    elif sessions_dir:
        args.sessions_dir = sessions_dir

    # Initialize session manager with custom directory if specified
    custom_dir = getattr(args, 'sessions_dir', None)
    manager = SessionManager(custom_sessions_dir=custom_dir)

    if not manager.sessions_dir:
        if args.json:
            error_data = {
                "success": False,
                "error": _("Could not find sessions directory."),
                "details": _("This tool must be run from within a MiniOS live system with persistent sessions enabled.")
            }
            print(json.dumps(error_data), file=sys.stderr)
        else:
            print(_("Error: Could not find sessions directory."), file=sys.stderr)
            print(_("This tool must be run from within a MiniOS live system with persistent sessions enabled."), file=sys.stderr)
        sys.exit(1)

    # Handle commands
    if args.command == 'list':
        sessions = manager.list_sessions()
        if args.json:
            print(format_sessions_json(sessions))
        else:
            print(format_session_list(sessions))

    elif args.command == 'active':
        current = manager.get_current_session()
        if args.json:
            print(format_session_json(current))
        else:
            if current:
                print(_("Active session: #{}").format(current['id']))
                print(_("Mode: {}").format(current['mode']))
                print(_("Version: {}").format(current['version']))
                print(_("Edition: {}").format(current['edition']))
                print(_("Union FS: {}").format(current['union']))
                print(_("Size: {}").format(manager._format_size(current['size'])))
                print(_("Last Modified: {}").format(current['modified'].strftime("%Y-%m-%d %H:%M:%S") if current['modified'] else "unknown"))
            else:
                print(_("No active session found"))

    elif args.command == 'running':
        running = manager.get_running_session()
        if args.json:
            print(format_session_json(running))
        else:
            if running:
                print(_("Running session: #{}").format(running['id']))
                print(_("Mode: {}").format(running['mode']))
                print(_("Version: {}").format(running['version']))
                print(_("Edition: {}").format(running['edition']))
                print(_("Union FS: {}").format(running['union']))
                print(_("Size: {}").format(manager._format_size(running['size'])))
                if running['modified']:
                    print(_("Last Modified: {}").format(running['modified'].strftime("%Y-%m-%d %H:%M:%S")))
                if 'status' in running:
                    print(_("Status: {}").format(running['status']))
            else:
                print(_("No running session detected"))

    elif args.command == 'info':
        fs_info, error = manager.get_filesystem_info()
        if error:
            if args.json:
                print(json.dumps({'success': False, 'error': error}), file=sys.stderr)
            else:
                print(_("Error: {}").format(error), file=sys.stderr)
            sys.exit(1)
        
        if args.json:
            print(format_filesystem_info_json(fs_info))
        else:
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
        if args.json:
            result = {"success": success, "message": message}
            print(json.dumps(result))
        else:
            print(message)
        sys.exit(0 if success else 1)

    elif args.command == 'create':
        success, message = manager.create_session(args.mode, args.size)
        if args.json:
            result = {"success": success, "message": message}
            print(json.dumps(result))
        else:
            print(message)
        sys.exit(0 if success else 1)

    elif args.command == 'delete':
        success, message = manager.delete_session(args.session_id)
        if args.json:
            result = {"success": success, "message": message}
            print(json.dumps(result))
        else:
            print(message)
        sys.exit(0 if success else 1)

    elif args.command == 'cleanup':
        deleted_count, errors = manager.cleanup_old_sessions(args.days)
        if args.json:
            result = {
                "success": len(errors) == 0,
                "deleted_count": deleted_count,
                "errors": errors,
                "message": _("Cleanup completed: {} sessions deleted").format(deleted_count)
            }
            print(json.dumps(result))
        else:
            print(_("Cleanup completed: {} sessions deleted").format(deleted_count))
            if errors:
                print(_("Errors:"))
                for error in errors:
                    print(f"  {error}")

    elif args.command == 'resize':
        success, message = manager.resize_session(args.session_id, args.size)
        if args.json:
            result = {
                "success": success,
                "message": message
            }
            print(json.dumps(result))
        else:
            print(message)
        sys.exit(0 if success else 1)

    elif args.command == 'status':
        status_info = manager.check_sessions_directory_status()
        if args.json:
            print(json.dumps(status_info))
        else:
            print(_("Sessions directory: {}").format(status_info.get('sessions_dir', 'N/A')))
            if status_info.get('found', False):
                print(_("Status: {}").format(_("Found")))
                if status_info.get('writable', False):
                    print(_("Access: {}").format(_("Writable")))
                else:
                    print(_("Access: {}").format(_("Read-only")))
                    if 'error' in status_info:
                        print(_("Reason: {}").format(status_info['error']))
                print(_("Filesystem type: {}").format(status_info.get('filesystem_type', 'unknown')))
            else:
                print(_("Status: {}").format(_("Not found")))
                if 'error' in status_info:
                    print(_("Error: {}").format(status_info['error']))

    elif args.command == 'export':
        verify = not args.no_verify
        success, message = manager.export_session(args.session_id, args.output_path, verify=verify)
        if args.json:
            result = {"success": success, "message": message}
            print(json.dumps(result))
        else:
            print(message)
        sys.exit(0 if success else 1)

    elif args.command == 'import':
        verify = not args.no_verify
        success, message = manager.import_session(
            args.archive_path,
            auto_convert=args.auto_convert,
            force_mode=args.force_mode,
            verify=verify,
            skip_compatibility_check=args.skip_compatibility_check
        )
        if args.json:
            result = {"success": success, "message": message}
            print(json.dumps(result))
        else:
            print(message)
        sys.exit(0 if success else 1)

    elif args.command == 'copy':
        success, message = manager.copy_session(
            args.session_id,
            to_mode=args.to_mode,
            size_mb=args.size
        )
        if args.json:
            result = {"success": success, "message": message}
            print(json.dumps(result))
        else:
            print(message)
        sys.exit(0 if success else 1)

    elif args.command == 'convert':
        in_place = not args.new_session
        success, message = manager.convert_session(
            args.session_id,
            args.target_mode,
            size_mb=args.size,
            in_place=in_place
        )
        if args.json:
            result = {"success": success, "message": message}
            print(json.dumps(result))
        else:
            print(message)
        sys.exit(0 if success else 1)


# GUI command removed

    else:
        parser.print_help()

if __name__ == '__main__':
    main()