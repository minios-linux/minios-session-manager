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
try:
    gettext.bindtextdomain('minios-session-manager', '/usr/share/locale')
    gettext.textdomain('minios-session-manager')
    _ = gettext.gettext
except Exception:
    _ = lambda x: x

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
    
    def _get_session_size_info(self, session_path, session_data):
        """Get comprehensive size information for a session"""
        session_mode = session_data.get('mode', 'unknown')
        stored_size = session_data.get('size')  # Size in MB from metadata
        
        # Convert stored_size to int if it's a string
        if stored_size is not None:
            try:
                stored_size = int(stored_size)
            except (ValueError, TypeError):
                stored_size = None
        
        if session_mode == 'dynfilefs':
            # For dynfilefs, show used/total format
            used_size = self._get_dynfilefs_size(session_path)
            if stored_size:
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

SESSION MODES:
  native                    Direct filesystem changes (requires POSIX-compatible filesystem)
  dynfilefs                 Dynamic file system overlay (works on any filesystem, 4GB default)
  raw                       Raw disk image (works on any filesystem, custom size required)

COMMAND BEHAVIOR:
  • create without --mode: Uses native mode (may fail on FAT32/NTFS)
  • create without --size: Uses 4000MB (4GB) for dynfilefs/raw modes
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
    minios-session create --mode dynfilefs        Create 4GB dynfilefs session
    minios-session create --mode dynfilefs --size 8000   Create 8GB dynfilefs session
    minios-session create --mode raw --size 2000         Create 2GB raw disk image

  Error Handling:
    minios-session create                         May fail on FAT32: "Use dynfilefs or raw mode"
    minios-session create --mode raw --size 5000 May fail on FAT32: "Exceeds 4000MB limit"

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
                              help=_('Size in MB for dynfilefs/raw modes (default: 4000)'))
    
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
    
    
# GUI command removed
    
    else:
        parser.print_help()

if __name__ == '__main__':
    main()