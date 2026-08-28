#!/usr/bin/env python3
"""
MiniOS Session CLI

Command-line utility for managing MiniOS persistent sessions from within the running system.
This is the CLI-only version that performs actual session operations.
"""

import argparse
import fcntl
import errno
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import contextlib
import signal
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path
import gettext
import getpass

# Handle SIGTERM to ensure finally blocks run (cleanup)
def _handle_sigterm(signum, frame):
    raise SystemExit(130)

signal.signal(signal.SIGTERM, _handle_sigterm)

# Internationalization setup
def _(message):
    """Translation function wrapper"""
    try:
        return gettext.dgettext('minios-session-manager', message)
    except Exception:
        return message

try:
    gettext.bindtextdomain('minios-session-manager', '/usr/share/locale')
    gettext.textdomain('minios-session-manager')
except Exception:
    pass

INITRD_CRYPTO_MARKER = '/run/initramfs/etc/minios-initramfs-crypt'


def luks_runtime_available():
    return bool(shutil.which('cryptsetup') and shutil.which('losetup') and
                os.path.isfile(INITRD_CRYPTO_MARKER))


class MetadataCommitUncertain(OSError):
    """A metadata rename became visible but its directory sync failed."""


class SessionManager:
    """Main class for managing MiniOS sessions"""

    MAX_ARCHIVE_EXTRACTED_BYTES = 8 * 1024 * 1024 * 1024
    DEFAULT_CONTAINER_SIZE_MB = 4000
    MAX_CONTAINER_SIZE_MB = 1000000
    # DynFileFS containers are thin: only a small footprint is consumed at
    # creation and growth is protected at runtime by the persistence guard, so
    # admission requires only this floor instead of the full logical size.
    DYNFILEFS_INITIAL_MB = 64
    SAVECHANGES_COMMAND = '/usr/bin/savechanges'
    SQUASHFS_SAVE_COMMAND = '/usr/bin/minios-squashfs-save'
    BOOT_STATE_FILE = '/run/initramfs/minios-persistence/boot-state'
    BOOT_ID_FILE = '/proc/sys/kernel/random/boot_id'
    SESSION_PATHS = (
        '/run/initramfs/memory/data/minios/changes',
        '/lib/live/mount/medium/minios/changes',
    )
    DURABLE_SESSION_FILESYSTEMS = (
        'ext2', 'ext3', 'ext4', 'btrfs', 'xfs', 'f2fs', 'reiserfs',
        'vfat', 'fat', 'msdos', 'exfat', 'ntfs', 'ntfs3', 'ntfs-3g',
        'fuseblk',
    )
    EXACT_STAGING_FILESYSTEMS = (
        'ext2', 'ext3', 'ext4', 'btrfs', 'xfs', 'f2fs', 'reiserfs',
    )
    SAVECHANGES_PHASES = (
        'prepare', 'inventory', 'capture', 'compress', 'verify', 'publish',
        'complete',
    )
    SQUASHFS_AUTOSAVE_INTERVALS = (0, 30, 60, 120, 240, 480)

    def __init__(self, custom_sessions_dir=None):
        self.sessions_file = None
        self.sessions_dir = None
        self.current_session = None
        self.session_format = None  # 'json' or 'conf'
        self.metadata_valid = True
        self.custom_sessions_dir = custom_sessions_dir
        self._lock_fd = None
        self._lock_depth = 0
        
        # Setup cache directory and file in /tmp (clears on reboot).
        self.cache_dir = f"/tmp/minios-session-manager-{os.getuid()}"
        self.cache_file = os.path.join(self.cache_dir, "session_sizes.json")
        self._ensure_cache_dir()
        
            
        self._detect_session_storage()

    def _ensure_cache_dir(self):
        """Ensure the shared-/tmp cache path is a directory owned by this uid."""
        try:
            os.makedirs(self.cache_dir, mode=0o700, exist_ok=True)
            flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, 'O_NOFOLLOW', 0)
            cache_fd = os.open(self.cache_dir, flags)
            try:
                cache_stat = os.fstat(cache_fd)
                if cache_stat.st_uid != os.getuid() or not stat.S_ISDIR(cache_stat.st_mode):
                    raise OSError("unsafe cache directory")
                os.fchmod(cache_fd, 0o700)
            finally:
                os.close(cache_fd)
        except OSError:
            # Fall back to an unpredictable private directory without following
            # an existing attacker-controlled /tmp entry.
            self.cache_dir = tempfile.mkdtemp(prefix="minios-session-manager-", dir="/tmp")
            self.cache_file = os.path.join(self.cache_dir, "session_sizes.json")

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
                os.chmod(self.cache_file, 0o600)
                json.dump(cache_content, f, indent=2)
        except OSError:
            pass  # Ignore cache write failures

    def _make_temp_dir(self):
        """Create temporary directory in sessions directory

        Returns path to temporary directory that should be cleaned up after use
        """
        if not self.sessions_dir:
            raise OSError("sessions directory not found")
        return tempfile.mkdtemp(prefix=".tmp_", dir=self.sessions_dir)

    def _validate_session_id(self, session_id):
        """Return a canonical numeric ID or reject paths and metadata keys."""
        if not isinstance(session_id, str) or not re.fullmatch(r"[0-9]+", session_id):
            raise ValueError(_("Invalid session ID"))
        return session_id

    def _session_path(self, session_id, require_exists=False):
        """Resolve a session path while keeping it inside the configured root."""
        session_id = self._validate_session_id(session_id)
        if not self.sessions_dir:
            raise ValueError(_("Sessions directory not found"))
        root = os.path.realpath(self.sessions_dir)
        candidate = os.path.join(root, session_id)
        if os.path.commonpath([root, os.path.realpath(candidate)]) != root:
            raise ValueError(_("Invalid session path"))
        if require_exists and (not os.path.isdir(candidate) or os.path.islink(candidate)):
            raise ValueError(_("Session {} does not exist").format(session_id))
        return candidate

    @contextlib.contextmanager
    def _mutation_lock(self):
        """Serialize metadata and session-tree mutations across CLI processes."""
        if not self.sessions_dir:
            raise OSError("sessions directory not found")
        if self._lock_depth == 0:
            lock_path = os.path.join(self.sessions_dir, '.session.lock')
            flags = os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0)
            self._lock_fd = os.open(lock_path, flags, 0o600)
            lock_stat = os.fstat(self._lock_fd)
            if (not stat.S_ISREG(lock_stat.st_mode) or lock_stat.st_uid != os.geteuid()
                    or lock_stat.st_nlink != 1):
                os.close(self._lock_fd)
                self._lock_fd = None
                raise OSError("unsafe session lock file")
            os.fchmod(self._lock_fd, 0o600)
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX)
        self._lock_depth += 1
        try:
            yield
        finally:
            self._lock_depth -= 1
            if self._lock_depth == 0 and self._lock_fd is not None:
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
                os.close(self._lock_fd)
                self._lock_fd = None

    def _reserve_session(self):
        """Reserve a unique ID by creating its directory while holding the lock."""
        with self._mutation_lock():
            new_id = str(self._get_next_session_id())
            path = self._session_path(new_id)
            os.mkdir(path, 0o700)
            return new_id, path

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

    def _invalidate_size_cache(self, session_id):
        cache_data = self._load_size_cache()
        if session_id in cache_data:
            del cache_data[session_id]
            self._save_size_cache(cache_data)


    def _get_current_union_fs(self):
        """Get current union filesystem type"""
        try:
            with open('/proc/self/mountinfo', 'r') as f:
                for line in f:
                    fields = line.split()
                    if len(fields) < 7 or fields[4] != '/' or '-' not in fields:
                        continue
                    separator = fields.index('-')
                    filesystem = fields[separator + 1]
                    if filesystem in ('aufs', 'overlay', 'overlayfs'):
                        return 'overlayfs' if filesystem == 'overlay' else filesystem
                    return 'unknown'
        except (OSError, IOError):
            pass
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
        except Exception:
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
        except Exception:
            pass
        return "unknown"

    def _current_boot_id(self):
        try:
            with open(self.BOOT_ID_FILE, 'r', encoding='ascii') as boot_id_file:
                boot_id = boot_id_file.read().strip()
            if re.fullmatch(r'[0-9a-fA-F-]+', boot_id or ''):
                return boot_id
        except OSError:
            pass
        return None

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
            return False, _("Unable to check free disk space: {}").format(str(e))

    def _detect_session_storage(self):
        """Detect where sessions are stored and in what format"""
        # If custom directory is specified, use it
        if self.custom_sessions_dir:
            if os.path.isdir(self.custom_sessions_dir):
                self.sessions_dir = os.path.realpath(self.custom_sessions_dir)
            else:
                return False
        else:
            # Common locations for session storage
            possible_paths = [
                "/run/initramfs/memory/data/minios/changes",
                "/lib/live/mount/medium/minios/changes",
            ]
            
            for path in possible_paths:
                if os.path.exists(path):
                    self.sessions_dir = path
                    break
        
        if not self.sessions_dir:
            return False
            
        json_file = os.path.join(self.sessions_dir, "session.json")
        conf_file = os.path.join(self.sessions_dir, "session.conf")

        if os.path.exists(json_file):
            self.sessions_file = json_file
            self.session_format = "json"
            try:
                with open(json_file, 'r', encoding='utf-8') as f:
                    self._normalize_metadata(json.load(f))
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                self.metadata_valid = False
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
        
        fs_info, fs_error = self._detect_filesystem_type()
        fs_type = fs_info['type'] if fs_info else "unknown"
        
        # Check if directory is writable
        writable = False
        error_msg = None
        
        try:
            # SquashFS is always read-only
            if fs_error:
                writable = False
                error_msg = fs_error
            elif fs_info.get('is_readonly') or fs_type == 'squashfs':
                writable = False
                error_msg = _("Sessions directory is on a read-only filesystem")
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
        if self.sessions_file and not os.path.exists(self.sessions_file):
            self.sessions_file = None
            self.session_format = None
            self.metadata_valid = True
            self._detect_session_storage()
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
                            metadata["default"] = line.split("=", 1)[1] or None
                        elif line.startswith("running="):
                            metadata["running"] = line.split("=", 1)[1]
                        elif line.startswith("session_"):
                            match = re.fullmatch(
                                r'session_([a-z][a-z0-9_]*)\[([0-9]+)\]=(.*)',
                                line)
                            if not match:
                                raise ValueError("invalid session metadata line")
                            field, session_id, value = match.groups()
                            if session_id not in metadata["sessions"]:
                                metadata["sessions"][session_id] = {}
                            metadata["sessions"][session_id][field] = value
                return metadata
        except Exception as e:
            self.metadata_valid = False
            print(f"Error reading sessions metadata: {e}", file=sys.stderr)
            return {"default": None, "sessions": {}}

    def _serialize_metadata(self, f, fmt, metadata):
        """Write metadata to an open file object in the given format."""
        if fmt == "json":
            json.dump(metadata, f, indent=2)
            f.write('\n')
        else:  # conf format
            f.write(f"default={metadata.get('default') or ''}\n")
            if 'running' in metadata:
                f.write(f"running={metadata['running']}\n")
            for session_id, session_data in metadata.get("sessions", {}).items():
                for field, value in session_data.items():
                    f.write(f"session_{field}[{session_id}]={value}\n")

    def _normalize_metadata(self, metadata):
        """Return the scalar metadata representation shared by JSON and conf."""
        if not isinstance(metadata, dict) or not isinstance(metadata.get('sessions', {}), dict):
            raise ValueError("invalid session metadata")

        normalized = {"default": None, "sessions": {}}
        for selector in ('default', 'running'):
            value = metadata.get(selector)
            if value in (None, ''):
                continue
            value = str(value)
            if not re.fullmatch(r'[0-9]+', value):
                raise ValueError("invalid session ID")
            normalized[selector] = value

        for session_id, fields in metadata.get('sessions', {}).items():
            session_id = str(session_id)
            if not re.fullmatch(r'[0-9]+', session_id) or not isinstance(fields, dict):
                raise ValueError("invalid session metadata")
            normalized_fields = {}
            for field, value in fields.items():
                if not isinstance(field, str) or not re.fullmatch(r'[a-z][a-z0-9_]*', field):
                    raise ValueError("invalid session metadata field")
                if value is None:
                    continue
                if isinstance(value, bool):
                    value = 'true' if value else 'false'
                elif isinstance(value, (str, int)):
                    value = str(value)
                else:
                    raise ValueError("invalid session metadata value")
                if any(character in value for character in ('\r', '\n', '\0')):
                    raise ValueError("invalid session metadata value")
                normalized_fields[field] = value
            normalized["sessions"][session_id] = normalized_fields
        return normalized

    def _atomic_write_metadata_file(self, path, fmt, metadata):
        """Atomically replace one metadata file (temp -> fsync -> rename -> dir fsync)."""
        temp_name = None
        try:
            fd, temp_name = tempfile.mkstemp(prefix='.session-metadata-', dir=self.sessions_dir)
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                self._serialize_metadata(f, fmt, metadata)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(temp_name, 0o600)
            os.replace(temp_name, path)
            temp_name = None
            try:
                directory_fd = os.open(self.sessions_dir, os.O_DIRECTORY)
            except OSError as error:
                raise MetadataCommitUncertain(
                    "metadata publication directory cannot be synchronized") from error
            try:
                try:
                    os.fsync(directory_fd)
                except OSError as error:
                    unsupported = (errno.EINVAL, errno.ENOTSUP,
                                   getattr(errno, 'EOPNOTSUPP', errno.ENOTSUP))
                    if error.errno not in unsupported:
                        raise MetadataCommitUncertain(
                            "metadata publication durability is uncertain") from error
            finally:
                os.close(directory_fd)
            return True
        finally:
            if temp_name and os.path.exists(temp_name):
                os.unlink(temp_name)

    def _write_sessions_metadata(self, metadata):
        """Atomically refresh both equivalent session metadata formats."""
        if not self.metadata_valid:
            print("Error writing sessions metadata: selected metadata is invalid",
                  file=sys.stderr)
            return False
        if not self.sessions_file:
            if self.sessions_dir and os.access(self.sessions_dir, os.W_OK):
                self.sessions_file = os.path.join(self.sessions_dir, "session.json")
                self.session_format = "json"
            else:
                return False

        try:
            metadata = self._normalize_metadata(metadata)
            with self._mutation_lock():
                conf_path = os.path.join(self.sessions_dir, "session.conf")
                json_path = os.path.join(self.sessions_dir, "session.json")
                if os.path.exists(json_path):
                    with open(json_path, 'r', encoding='utf-8') as f:
                        previous = self._normalize_metadata(json.load(f))
                    # Guarantee a durable fallback before removing the selected
                    # JSON representation.
                    self._atomic_write_metadata_file(conf_path, "conf", previous)
                    os.unlink(json_path)
                    directory_fd = os.open(self.sessions_dir, os.O_DIRECTORY)
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
                if not self._atomic_write_metadata_file(conf_path, "conf", metadata):
                    return False
                try:
                    self._atomic_write_metadata_file(json_path, "json", metadata)
                except MetadataCommitUncertain:
                    raise
                except OSError as e:
                    self.sessions_file = conf_path
                    self.session_format = "conf"
                    print(f"Warning writing JSON session metadata: {e}", file=sys.stderr)
                    return True
                self.sessions_file = json_path
                self.session_format = "json"
                return True
        except MetadataCommitUncertain:
            raise
        except Exception as e:
            print(f"Error writing sessions metadata: {e}", file=sys.stderr)
            return False

    def _session_modified(self, session_path, session_data):
        """Return the timestamp that best represents the stored session state."""
        if session_data.get('mode') == 'squashfs':
            saved = session_data.get('saved')
            if saved:
                try:
                    return datetime.strptime(saved, '%Y-%m-%dT%H:%M:%SZ')
                except (TypeError, ValueError):
                    pass
            artifact = os.path.join(session_path, 'changes.sb')
            try:
                return datetime.fromtimestamp(os.path.getmtime(artifact))
            except OSError:
                return None
        try:
            return datetime.fromtimestamp(os.path.getmtime(session_path))
        except OSError:
            return None

    def list_sessions(self, include_running_check=True):
        """List all available sessions"""
        if not self.sessions_dir:
            return []
            
        sessions = []
        metadata = self._read_sessions_metadata()
        metadata.setdefault('sessions', {})
        
        # Get running session info for comparison (avoid recursion)
        running_id = None
        if include_running_check:
            running_session = self.get_running_session(avoid_recursion=True)
            if running_session and 'id' in running_session:
                running_id = running_session['id']
        
        # Find session directories (numeric names)
        for item in os.listdir(self.sessions_dir):
            path = os.path.join(self.sessions_dir, item)
            if os.path.isdir(path) and not os.path.islink(path) and item.isdigit():
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
                    'policy': session_data.get('policy'),
                    'autosave': session_data.get('autosave'),
                    'saved': session_data.get('saved'),
                    'size': size_info['used_size'],
                    'size_display': size_info['display'],
                    'total_size': size_info.get('total_size'),
                    'total_size_mb': session_data.get('size'),  # Size from metadata in MB
                    'modified': self._session_modified(path, session_data),
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
            result = subprocess.run(['which', 'dynfilefs'], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if result.returncode == 0:
                return True
            # Also check for mount.dynfilefs
            result = subprocess.run(['which', 'mount.dynfilefs'], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            return result.returncode == 0
        except Exception:
            return False

    def _check_luks_available(self):
        """Check both the userspace tools and the initrd LUKS persistence hook."""
        if not shutil.which('cryptsetup') or not shutil.which('losetup'):
            return False, _("LUKS mode requires cryptsetup and util-linux.")
        if luks_runtime_available():
            return True, None
        return False, _("This MiniOS initrd does not support perchmode=luks. Boot an image with the LUKS persistence hook before creating or activating LUKS sessions.")

    def _detect_filesystem_type(self):
        """Detect the filesystem type of the MiniOS media"""
        if not self.sessions_dir:
            return None, _("Sessions directory not found")
        
        try:
            target = os.path.realpath(self.sessions_dir)
            best_mount = None
            with open('/proc/self/mountinfo', encoding='utf-8') as mounts:
                for line in mounts:
                    fields = line.rstrip('\n').split(' ')
                    separator = fields.index('-')
                    mount_point = fields[4].replace('\\040', ' ')
                    if target == mount_point or target.startswith(mount_point.rstrip('/') + '/'):
                        if best_mount is None or len(mount_point) > len(best_mount[0]):
                            best_mount = (mount_point, fields, separator)
            if best_mount is None:
                return None, _("Failed to determine filesystem information")
            _mount_point, fields, separator = best_mount
            filesystem_type = fields[separator + 1].lower()
            device = fields[separator + 2]
            mount_options = fields[5]
            super_options = fields[separator + 3] if len(fields) > separator + 3 else ''
            return {
                'type': filesystem_type,
                'device': device,
                'mount_options': mount_options,
                'is_readonly': 'ro' in mount_options.split(',') or 'ro' in super_options.split(','),
                'is_posix_compatible': filesystem_type in ['ext2', 'ext3', 'ext4', 'btrfs', 'xfs', 'f2fs', 'reiserfs', 'tmpfs']
            }, None
            
        except Exception as e:
            return None, _("Error detecting filesystem: {}").format(str(e))

    def _get_compatible_session_modes(self, filesystem_info):
        """Get list of compatible session modes for the filesystem"""
        if not filesystem_info or filesystem_info.get('is_readonly'):
            return []
        
        fs_type = filesystem_info['type']
        is_readonly = filesystem_info['is_readonly']
        is_posix = filesystem_info['is_posix_compatible']
        
        compatible_modes = []
        
        # Native mode: requires POSIX-compatible filesystem (ext2/3/4, btrfs, xfs, etc.)
        if is_posix:
            compatible_modes.append('native')
        if fs_type in self.EXACT_STAGING_FILESYSTEMS:
            compatible_modes.append('squashfs')
        
        # DynFileFS mode: works on all writable filesystems.
        compatible_modes.append('dynfilefs')
        
        # Raw mode: works on ALL writable filesystems (static images)
        compatible_modes.append('raw')
        luks_available, _luks_error = self._check_luks_available()
        if luks_available:
            compatible_modes.append('luks')
        
        return compatible_modes

    def _get_filesystem_limitations(self, filesystem_info):
        """Get filesystem-specific limitations"""
        limitations = {}
        
        if not filesystem_info:
            return limitations
        
        fs_type = filesystem_info['type']
        
        # FAT32 limitations
        if fs_type in ['vfat', 'fat32', 'msdos']:
            # Match perchsize's supported FAT32 policy exactly.
            limitations['max_file_size'] = 4000
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

    def _validate_target_mode(self, mode, size_mb=None):
        """Validate a requested storage mode against the actual target filesystem."""
        if mode not in ('native', 'squashfs', 'dynfilefs', 'raw', 'luks'):
            return False, _("Invalid session mode")
        fs_info, error = self._detect_filesystem_type()
        if error or not fs_info:
            return False, error or _("Failed to determine filesystem information")
        if fs_info.get('is_readonly'):
            return False, _("Sessions directory is read-only")
        if mode not in self._get_compatible_session_modes(fs_info):
            return False, _("Session mode '{}' is not compatible with {} filesystem").format(mode, fs_info['type'])
        if size_mb is not None and size_mb <= 0:
            return False, _("Session size must be greater than zero")
        if mode == 'squashfs' and size_mb is not None:
            return False, _("SquashFS sessions do not use a fixed container size")
        if mode == 'dynfilefs' and not self._check_dynfilefs_available():
            return False, _("DynFileFS is not available on this system. Please install dynfilefs package.")
        max_size = self._get_filesystem_limitations(fs_info).get('max_file_size')
        if mode == 'luks':
            luks_available, luks_error = self._check_luks_available()
            if not luks_available:
                return False, luks_error
        if mode in ('raw', 'luks') and max_size and size_mb and size_mb > max_size:
            return False, _("Container size {}MB exceeds FAT32 file size limit ({}MB).").format(size_mb, max_size)
        if mode in ('raw', 'luks') and size_mb and size_mb > self.MAX_CONTAINER_SIZE_MB:
            return False, _("Container size exceeds the 1TB limit.")
        return True, None

    def _luks_mapper_name(self):
        return 'minios-session-{}-{}'.format(os.getpid(), next(tempfile._get_candidate_names()))

    def _cryptsetup(self, command, password):
        """Run cryptsetup with the passphrase exclusively on its stdin."""
        result = subprocess.run(command, input=password + b'\n', stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE)
        if result.returncode:
            raise OSError(result.stderr.decode(errors='replace').strip() or 'cryptsetup failed')

    @contextlib.contextmanager
    def _mount_luks(self, image_file, password, writable):
        """Expose one LUKS file through an owned loop and mapper, then clean both."""
        if not password:
            raise ValueError(_("A LUKS passphrase is required."))
        loop_device = mapper_name = mount_point = None
        try:
            loop_result = subprocess.run(['losetup', '--find', '--show', '--', image_file],
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if loop_result.returncode:
                raise OSError(loop_result.stderr.decode(errors='replace').strip() or 'failed to attach loop device')
            loop_device = loop_result.stdout.decode().strip()
            mapper_name = self._luks_mapper_name()
            self._cryptsetup(['cryptsetup', 'open', '--type', 'luks', '--key-file', '-',
                              loop_device, mapper_name], password)
            mount_point = tempfile.mkdtemp(prefix='minios_luks_')
            options = [] if writable else ['-o', 'ro']
            subprocess.run(['mount'] + options + ['/dev/mapper/' + mapper_name, mount_point], check=True)
            yield mount_point
        finally:
            if mount_point:
                self._safe_unmount(mount_point)
                self._safe_rmtree(mount_point)
            if mapper_name:
                subprocess.run(['cryptsetup', 'close', mapper_name], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if loop_device:
                subprocess.run(['losetup', '--detach', loop_device], stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def _create_luks_container(self, session_path, size_mb, password):
        """Create exactly one LUKS2/ext4 file, without a partition table or nesting."""
        image_file = os.path.join(session_path, 'changes.luks')
        size_bytes = size_mb * 1024 * 1024
        result = subprocess.run(['fallocate', '-l', str(size_bytes), image_file],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if result.returncode:
            with open(image_file, 'wb') as image:
                image.truncate(size_bytes)
        loop_device = mapper_name = None
        try:
            loop_result = subprocess.run(['losetup', '--find', '--show', '--', image_file],
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if loop_result.returncode:
                raise OSError(loop_result.stderr.decode(errors='replace').strip() or 'failed to attach loop device')
            loop_device = loop_result.stdout.decode().strip()
            self._cryptsetup(['cryptsetup', 'luksFormat', '--type', 'luks2', '--batch-mode',
                              '--key-file', '-', loop_device], password)
            mapper_name = self._luks_mapper_name()
            self._cryptsetup(['cryptsetup', 'open', '--type', 'luks', '--key-file', '-',
                              loop_device, mapper_name], password)
            subprocess.run(['mke2fs', '-F', '-t', 'ext4', '/dev/mapper/' + mapper_name], check=True,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            subprocess.run(['sync'], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        finally:
            if mapper_name:
                subprocess.run(['cryptsetup', 'close', mapper_name], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if loop_device:
                subprocess.run(['losetup', '--detach', loop_device], stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def _wait_for_mount(self, path, timeout=10):
        """Wait for a file or directory to appear (polling)"""
        start_time = time.time()
        while time.time() - start_time < timeout:
            if os.path.exists(path):
                return True
            time.sleep(0.1)
        return False

    def _safe_unmount(self, mount_point, max_retries=5, use_lazy=True):
        """
        Safely unmount a mount point with retries and optional lazy unmount.
        
        Returns True if unmounted successfully, False otherwise.
        """
        if not mount_point or not os.path.exists(mount_point):
            return True
        
        # Check if actually mounted
        try:
            result = subprocess.run(
                ['mountpoint', '-q', mount_point],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            if result.returncode != 0:
                return True  # Not mounted
        except (OSError, IOError):
            pass
        
        # Try normal unmount with retries
        for i in range(max_retries):
            subprocess.run(['sync'], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            res = subprocess.run(
                ['umount', mount_point],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            if res.returncode == 0:
                return True
            time.sleep(0.5 * (i + 1))  # Increasing delay
        
        # Try lazy unmount as last resort
        if use_lazy:
            res = subprocess.run(
                ['umount', '-l', mount_point],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            if res.returncode == 0:
                return True
        
        return False

    def _safe_fusermount(self, mount_point, max_retries=5):
        """
        Safely unmount a FUSE mount point with retries.
        
        Returns True if unmounted successfully, False otherwise.
        """
        if not mount_point or not os.path.exists(mount_point):
            return True
        
        for i in range(max_retries):
            subprocess.run(['sync'], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            res = subprocess.run(
                ['fusermount', '-u', mount_point],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            if res.returncode == 0:
                return True
            time.sleep(0.5 * (i + 1))
        
        # Try lazy unmount
        res = subprocess.run(
            ['fusermount', '-uz', mount_point],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        return res.returncode == 0

    def _cleanup_process(self, process, timeout=5):
        """
        Safely terminate and cleanup a subprocess.
        
        Sends SIGTERM, waits, then SIGKILL if needed.
        """
        if process is None:
            return
        
        # Check if already terminated
        if process.poll() is not None:
            return
        
        # Try graceful termination
        try:
            process.terminate()
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            # Force kill
            process.kill()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
        
        # Close pipes to prevent resource leaks
        try:
            if process.stdout:
                process.stdout.close()
            if process.stderr:
                process.stderr.close()
        except (OSError, IOError):
            pass

    def _safe_rmtree(self, path):
        """
        Safely remove a directory tree, only if not a mount point.
        """
        if not path or not os.path.exists(path):
            return True
        
        # Check if it's a mount point - don't remove if still mounted
        try:
            result = subprocess.run(
                ['mountpoint', '-q', path],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            if result.returncode == 0:
                # Still mounted, don't remove
                return False
        except (OSError, IOError):
            pass
        
        try:
            shutil.rmtree(path, ignore_errors=True)
            return True
        except (OSError, IOError):
            return False

    @contextlib.contextmanager
    def _mount_session_read(self, session_path, mode, password=None):
        """Context manager to mount a session for reading"""
        mount_point = None
        virtual_mount = None
        process = None
        
        try:
            if mode == 'native':
                # Check for OverlayFS structure
                changes_dir = os.path.join(session_path, 'changes')
                if os.path.exists(changes_dir) and os.path.isdir(changes_dir):
                    yield changes_dir
                else:
                    yield session_path
            
            elif mode == 'dynfilefs':
                changes_file = os.path.join(session_path, 'changes.dat')
                if not os.path.exists(changes_file):
                    raise Exception(_("DynFileFS container not found"))
                
                # Use system temp for mount point
                mount_point = tempfile.mkdtemp(prefix="minios_dyn_read_")
                
                # Mount dynfilefs
                cmd = ['dynfilefs', '-f', changes_file, '-m', mount_point]
                process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                
                virtual_file = os.path.join(mount_point, 'virtual.dat')
                if not self._wait_for_mount(virtual_file):
                    raise Exception(_("Failed to mount dynfilefs (timeout)"))
                    
                # Mount virtual file
                virtual_mount = tempfile.mkdtemp(prefix="minios_virt_read_")
                subprocess.run(['mount', '-o', 'loop,ro', virtual_file, virtual_mount], check=True)
                
                # Check for OverlayFS structure inside
                changes_dir = os.path.join(virtual_mount, 'changes')
                if os.path.exists(changes_dir) and os.path.isdir(changes_dir):
                    yield changes_dir
                else:
                    yield virtual_mount
            
            elif mode == 'raw':
                image_file = os.path.join(session_path, 'changes.img')
                if not os.path.exists(image_file):
                    raise Exception(_("Raw image not found"))
                
                mount_point = tempfile.mkdtemp(prefix="minios_raw_read_")
                subprocess.run(['mount', '-o', 'loop,ro', image_file, mount_point], check=True)
                
                changes_dir = os.path.join(mount_point, 'changes')
                if os.path.exists(changes_dir) and os.path.isdir(changes_dir):
                    yield changes_dir
                else:
                    yield mount_point

            elif mode == 'luks':
                image_file = os.path.join(session_path, 'changes.luks')
                if not os.path.exists(image_file):
                    raise Exception(_("LUKS container not found"))
                with self._mount_luks(image_file, password, writable=False) as luks_mount:
                    changes_dir = os.path.join(luks_mount, 'changes')
                    yield changes_dir if os.path.isdir(changes_dir) else luks_mount
            
            else:
                raise Exception(_("Unknown session mode: {}").format(mode))
                
        finally:
            # Cleanup virtual mount first (if exists)
            if virtual_mount:
                self._safe_unmount(virtual_mount)
                self._safe_rmtree(virtual_mount)
            
            # Cleanup dynfilefs process and FUSE mount
            if process:
                if mount_point:
                    self._safe_fusermount(mount_point)
                self._cleanup_process(process)
            elif mode == 'raw' and mount_point:
                # Raw mode unmount
                self._safe_unmount(mount_point)
            
            # Cleanup mount point directory
            if mount_point:
                self._safe_rmtree(mount_point)

    @contextlib.contextmanager
    def _mount_session_write(self, session_path, mode, size_mb=None, password=None):
        """Context manager to mount a session for writing"""
        mount_point = None
        virtual_mount = None
        process = None
        
        try:
            os.makedirs(session_path, exist_ok=True)
            
            if mode == 'native':
                # Use changes directory for native sessions to match OverlayFS structure
                changes_dir = os.path.join(session_path, 'changes')
                os.makedirs(changes_dir, exist_ok=True)
                yield changes_dir
            
            elif mode == 'dynfilefs':
                if not size_mb: size_mb = 4000
                changes_file = os.path.join(session_path, 'changes.dat')
                mount_point = tempfile.mkdtemp(prefix="minios_dyn_write_")
                
                cmd = ['dynfilefs', '-f', changes_file, '-m', mount_point, '-s', str(size_mb), '-p', '4000']
                process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                
                virtual_file = os.path.join(mount_point, 'virtual.dat')
                if not self._wait_for_mount(virtual_file):
                    raise Exception(_("Failed to create dynfilefs (timeout)"))
                    
                # Only format if new (size check is a rough proxy, better to check if file existed before)
                # But here we assume we are setting up for write. 
                # If changes.dat didn't exist, dynfilefs created it empty.
                # We need to check if it has a filesystem.
                # For simplicity in this refactor, we assume if we are calling this, we might be creating or writing.
                # If it's a new session creation, we need to format.
                # Let's check if we can mount it first.
                
                virtual_mount = tempfile.mkdtemp(prefix="minios_virt_write_")
                mount_result = subprocess.run(['mount', '-o', 'loop', virtual_file, virtual_mount], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                
                if mount_result.returncode != 0:
                    # Not formatted, format it
                    subprocess.run(['mke2fs', '-F', '-t', 'ext4', virtual_file], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    subprocess.run(['sync'], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    subprocess.run(['mount', '-o', 'loop', virtual_file, virtual_mount], check=True)
                
                yield virtual_mount
                
            elif mode == 'raw':
                if not size_mb: size_mb = 4000
                image_file = os.path.join(session_path, 'changes.img')
                size_bytes = size_mb * 1024 * 1024
                
                if not os.path.exists(image_file):
                    subprocess.run(['fallocate', '-l', str(size_bytes), image_file], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    if not os.path.exists(image_file) or os.path.getsize(image_file) < size_bytes:
                         with open(image_file, 'wb') as f: f.truncate(size_bytes)
                    # Format new image
                    subprocess.run(['mke2fs', '-F', '-t', 'ext4', image_file], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    subprocess.run(['sync'], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                
                mount_point = tempfile.mkdtemp(prefix="minios_raw_write_")
                subprocess.run(['mount', '-o', 'loop', image_file, mount_point], check=True)
                
                yield mount_point

            elif mode == 'luks':
                image_file = os.path.join(session_path, 'changes.luks')
                if not os.path.exists(image_file):
                    self._create_luks_container(session_path, size_mb or self.DEFAULT_CONTAINER_SIZE_MB, password)
                with self._mount_luks(image_file, password, writable=True) as luks_mount:
                    yield luks_mount
                
        finally:
            # Cleanup virtual mount first (if exists)
            if virtual_mount:
                self._safe_unmount(virtual_mount)
                self._safe_rmtree(virtual_mount)
            
            # Cleanup dynfilefs process and FUSE mount
            if process:
                if mount_point:
                    self._safe_fusermount(mount_point)
                self._cleanup_process(process)
            elif mode == 'raw' and mount_point:
                # Raw mode unmount
                self._safe_unmount(mount_point)
            
            # Cleanup mount point directory
            if mount_point:
                self._safe_rmtree(mount_point)

    def _create_dynfilefs_session(self, session_path, initial_size_mb=1000):
        """Create a dynfilefs session structure"""
        temp_mount = None
        process = None
        try:
            changes_file = os.path.join(session_path, "changes.dat")
            temp_mount = tempfile.mkdtemp(prefix="minios_dyn_create_")
            process = subprocess.Popen([
                'dynfilefs', '-f', changes_file, '-m', temp_mount,
                '-s', str(initial_size_mb), '-p', '4000'
            ], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            virtual_file = os.path.join(temp_mount, "virtual.dat")
            if not self._wait_for_mount(virtual_file):
                return False, _("Failed to create dynfilefs virtual file (timeout)")
            format_result = subprocess.run(
                ['mke2fs', '-F', '-t', 'ext4', virtual_file],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if format_result.returncode != 0:
                return False, _("Failed to format dynfilefs virtual file: {}").format(format_result.stderr.decode())
            subprocess.run(['sync'], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            return True, _("DynFileFS session created successfully")
        except Exception as e:
            return False, _("Error creating dynfilefs session: {}").format(str(e))
        finally:
            if temp_mount:
                self._safe_fusermount(temp_mount)
            self._cleanup_process(process)
            if temp_mount:
                self._safe_rmtree(temp_mount)

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
        except Exception:
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

        if session_mode == 'squashfs':
            try:
                logical_size = int(session_data.get('uncompressed', ''))
            except (TypeError, ValueError):
                logical_size = None
            if logical_size is not None and logical_size >= 0:
                return {
                    'used_size': logical_size,
                    'display': self._format_size(logical_size)
                }
            artifact = os.path.join(session_path, 'changes.sb')
            try:
                size = os.path.getsize(artifact)
            except OSError:
                size = 0
            return {'used_size': size, 'display': self._format_size(size)}

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

        elif session_mode == 'luks':
            image_file = os.path.join(session_path, 'changes.luks')
            if os.path.exists(image_file):
                size = os.path.getsize(image_file)
                return {'used_size': size, 'display': self._format_size(size), 'total_size': size}
            if stored_size:
                size = stored_size * 1024 * 1024
                return {'used_size': size, 'display': self._format_size(size), 'total_size': size}
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
            self._validate_session_id(session_id)
            with self._mutation_lock():
                metadata = self._read_sessions_metadata()
                metadata['running'] = session_id
                return self._write_sessions_metadata(metadata)
        except Exception as e:
            print(f"Error setting running session: {e}", file=sys.stderr)
            return False

    def clear_running_session(self):
        """Clear running session from metadata"""
        try:
            with self._mutation_lock():
                metadata = self._read_sessions_metadata()
                if 'running' in metadata:
                    del metadata['running']
                return self._write_sessions_metadata(metadata)
        except Exception as e:
            print(f"Error clearing running session: {e}", file=sys.stderr)
            return False

    def _get_session_info(self, session_id, avoid_recursion=False):
        """Helper to get session info by ID"""
        try:
            session_id = self._validate_session_id(session_id)
        except ValueError:
            return None
        if avoid_recursion:
            # Simple session info without full list
            session_path = self._session_path(session_id) if self.sessions_dir else None
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
                'path': self._session_path(session_id) if self.sessions_dir else None,
                'mode': 'unknown',
                'version': 'unknown',
                'edition': 'unknown',
                'union': 'unknown',
                'size': 0,
                'modified': None,
                'is_default': False,
                'status': 'running_missing'
            }

    def _validate_squashfs_activation(self, session_id, session_data):
        """Reject a SquashFS session that initramfs cannot restore safely."""
        if session_data.get('policy', 'manual') not in ('manual', 'shutdown'):
            return False, _("SquashFS session has an invalid save policy")
        if session_data.get('union') != self._get_current_union_fs():
            return False, _("SquashFS session union filesystem does not match this system")
        digest = session_data.get('digest', '')
        if not re.fullmatch(r'[0-9a-f]{64}', digest):
            return False, _("SquashFS session metadata has an invalid digest")
        try:
            compressed = int(session_data.get('compressed', ''))
            uncompressed = int(session_data.get('uncompressed', ''))
            entries = int(session_data.get('entries', ''))
        except (TypeError, ValueError):
            return False, _("SquashFS session metadata is incomplete")
        if compressed < 1 or uncompressed < 0 or entries < 0:
            return False, _("SquashFS session metadata is incomplete")
        try:
            session_path = self._session_path(session_id, require_exists=True)
            artifact_path = os.path.join(session_path, 'changes.sb')
            artifact = os.stat(artifact_path, follow_symlinks=False)
            if (not stat.S_ISREG(artifact.st_mode) or artifact.st_nlink != 1 or
                    artifact.st_uid != os.geteuid() or artifact.st_size != compressed):
                return False, _("SquashFS session artifact does not match its metadata")
            with open(artifact_path, 'rb') as artifact_file:
                if artifact_file.read(4) != b'hsqs':
                    return False, _("SquashFS session artifact is invalid")
            directory_fd = os.open(session_path, os.O_RDONLY | os.O_DIRECTORY |
                                   getattr(os, 'O_NOFOLLOW', 0))
            try:
                if self._artifact_digest(directory_fd, 'changes.sb') != digest:
                    return False, _("SquashFS session artifact does not match its metadata")
            finally:
                os.close(directory_fd)
        except OSError as error:
            return False, _("SquashFS session artifact is unavailable: {}").format(error)
        return True, None

    def activate_session(self, session_id):
        """Activate a session (set as default)"""
        if not self.sessions_dir:
            return False, _("Sessions directory not found")
            
        try:
            self._session_path(session_id, require_exists=True)
        except ValueError as e:
            return False, str(e)
        
        try:
            # Update metadata to set new default
            with self._mutation_lock():
                metadata = self._normalize_metadata(self._read_sessions_metadata())
                session_data = metadata.get("sessions", {}).get(session_id)
                if session_data is None:
                    return False, _("Session {} does not exist").format(session_id)
                if session_data.get("mode") == "squashfs":
                    valid, error = self._validate_squashfs_activation(
                        session_id, session_data)
                    if not valid:
                        return False, error
                old_default = metadata.get("default")
                metadata["default"] = session_id
                updated = self._write_sessions_metadata(metadata)
            if updated:
                if old_default:
                    return True, _("Session {} activated (was session {})").format(session_id, old_default)
                else:
                    return True, _("Session {} activated").format(session_id)
            else:
                return False, _("Failed to update session metadata")
        except Exception as e:
            return False, _("Error activating session: {}").format(str(e))

    def set_squashfs_save_settings(self, session_id, shutdown=None, autosave=None):
        """Update shutdown and periodic-save settings for a SquashFS session."""
        try:
            session_id = self._validate_session_id(session_id)
        except ValueError as error:
            return False, str(error), None
        if autosave is not None:
            try:
                autosave = int(autosave)
            except (TypeError, ValueError):
                return False, _("Invalid SquashFS automatic save interval"), None
            if autosave not in self.SQUASHFS_AUTOSAVE_INTERVALS:
                return False, _("Invalid SquashFS automatic save interval"), None
        with self._mutation_lock():
            metadata = self._normalize_metadata(self._read_sessions_metadata())
            session_data = metadata.get('sessions', {}).get(session_id)
            if session_data is None:
                return False, _("Session {} does not exist").format(session_id), None
            if session_data.get('mode') != 'squashfs':
                return False, _("Session {} is not a SquashFS session").format(session_id), None
            changed = shutdown is not None or autosave is not None
            if shutdown is not None:
                session_data['policy'] = 'shutdown' if shutdown else 'manual'
            if autosave is not None:
                if autosave:
                    session_data['autosave'] = autosave
                else:
                    session_data.pop('autosave', None)
            if changed and not self._write_sessions_metadata(metadata):
                return False, _("Failed to update session save settings"), None
            settings = {
                'shutdown': session_data.get('policy', 'manual') == 'shutdown',
                'autosave': int(session_data.get('autosave', '0') or 0),
            }
            return True, _("Session save settings updated"), settings

    def autosave_running_session(self, now=None):
        """Save the running SquashFS session when its configured interval is due."""
        metadata = self._normalize_metadata(self._read_sessions_metadata())
        session_id = metadata.get('running')
        session_data = metadata.get('sessions', {}).get(session_id or '', {})
        if not session_id or session_data.get('mode') != 'squashfs':
            return False, True, _("No running SquashFS session requires periodic saving"), None
        try:
            interval = int(session_data.get('autosave', '0') or 0)
        except (TypeError, ValueError):
            return False, False, _("Invalid SquashFS automatic save interval"), None
        if interval == 0:
            return False, True, _("Periodic session saving is disabled"), None
        if interval not in self.SQUASHFS_AUTOSAVE_INTERVALS:
            return False, False, _("Invalid SquashFS automatic save interval"), None

        now = now or datetime.now(timezone.utc).replace(tzinfo=None)
        baseline = None
        saved = session_data.get('saved')
        if saved:
            try:
                baseline = datetime.strptime(saved, '%Y-%m-%dT%H:%M:%SZ')
            except (TypeError, ValueError):
                baseline = None
        try:
            with open('/proc/uptime', 'r', encoding='ascii') as uptime_file:
                uptime = float(uptime_file.read().split()[0])
            boot_started = now - timedelta(seconds=max(0.0, uptime))
            if baseline is None or baseline < boot_started:
                baseline = boot_started
        except (OSError, ValueError, IndexError):
            pass
        if baseline is not None and now < baseline + timedelta(minutes=interval):
            return False, True, _("Periodic session save is not due yet"), None
        success, message, capture = self.save_session(session_id)
        return True, success, message, capture

    def _replace_boot_state_session(self, expected_session, new_session):
        """Atomically retarget this boot's volatile SquashFS runtime authority."""
        path = self.BOOT_STATE_FILE
        directory = os.path.dirname(path)
        state_stat = os.stat(path, follow_symlinks=False)
        if (not stat.S_ISREG(state_stat.st_mode) or state_stat.st_uid != os.geteuid() or
                state_stat.st_nlink != 1 or stat.S_IMODE(state_stat.st_mode) & 0o022):
            raise OSError(_("Persistence runtime state is not trusted"))
        lines = []
        replaced = 0
        with open(path, 'r', encoding='utf-8') as state_file:
            for raw_line in state_file:
                line = raw_line.rstrip('\n')
                if line.startswith('session='):
                    if line != 'session={}'.format(expected_session):
                        raise OSError(_("Persistence runtime session changed"))
                    line = 'session={}'.format(new_session)
                    replaced += 1
                lines.append(line)
        if replaced != 1:
            raise OSError(_("Persistence runtime state is malformed"))
        fd, temporary = tempfile.mkstemp(prefix='.boot-state-', dir=directory)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as output:
                output.write('\n'.join(lines) + '\n')
                output.flush()
                os.fsync(output.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
            temporary = None
            directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)

    def handoff_running_squashfs(self, running_id, target_id):
        """Move this boot's save target to a SquashFS snapshot captured this boot."""
        try:
            running_id = self._validate_session_id(running_id)
            target_id = self._validate_session_id(target_id)
        except ValueError as error:
            return False, str(error)
        if running_id == target_id:
            return False, _("The running and target sessions are the same")
        with self._mutation_lock():
            metadata = self._normalize_metadata(self._read_sessions_metadata())
            if metadata.get('running') != running_id or metadata.get('default') != target_id:
                return False, _("The active SquashFS session is not available for handoff")
            running_data = metadata.get('sessions', {}).get(running_id)
            target_data = metadata.get('sessions', {}).get(target_id)
            if (not running_data or running_data.get('mode') != 'squashfs' or
                    not target_data or target_data.get('mode') != 'squashfs'):
                return False, _("SquashFS handoff requires running and active SquashFS sessions")
            boot_id = self._current_boot_id()
            if not boot_id or target_data.get('capture_boot_id') != boot_id:
                return False, _("The active SquashFS session was not captured during this boot")
            valid, message = self._validate_squashfs_activation(target_id, target_data)
            if not valid:
                return False, message
            valid, message = self._validate_squashfs_runtime(running_id)
            if not valid:
                return False, message

            self._replace_boot_state_session(running_id, target_id)
            metadata['running'] = target_id
            target_data['state'] = 'dirty'
            try:
                if not self._write_sessions_metadata(metadata):
                    raise OSError(_("Failed to update session metadata"))
            except BaseException:
                try:
                    self._replace_boot_state_session(target_id, running_id)
                except OSError:
                    pass
                raise
        return True, _("Running session moved to session {}").format(target_id)

    def _sync_session_directory(self, directory_fd):
        """Synchronize a numbered session directory where the filesystem permits it."""
        try:
            os.fsync(directory_fd)
        except OSError as error:
            unsupported = (errno.EINVAL, errno.ENOTSUP,
                           getattr(errno, 'EOPNOTSUPP', errno.ENOTSUP))
            if error.errno not in unsupported:
                raise

    def _stop_capture_process(self, process):
        """Terminate and reap the complete savechanges process group."""
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        deadline = time.monotonic() + 12.0
        group_alive = True
        while time.monotonic() < deadline:
            process.poll()
            try:
                os.killpg(process.pid, 0)
            except ProcessLookupError:
                group_alive = False
            if process.poll() is not None and not group_alive:
                break
            time.sleep(0.05)
        if group_alive:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                group_alive = False
        kill_deadline = time.monotonic() + 2.0
        while group_alive and time.monotonic() < kill_deadline:
            try:
                os.killpg(process.pid, 0)
            except ProcessLookupError:
                group_alive = False
                break
            time.sleep(0.05)
        process.wait()

    @staticmethod
    def _strict_json_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON field")
            result[key] = value
        return result

    @staticmethod
    def _reject_json_constant(value):
        raise ValueError("invalid JSON constant: {}".format(value))

    def _run_savechanges(self, output_path, work_parent, progress_callback=None):
        """Run exact capture and return its validated machine result."""
        command = self.SAVECHANGES_COMMAND
        try:
            command_stat = os.stat(command, follow_symlinks=False)
        except OSError as error:
            raise OSError(_("Trusted savechanges command is unavailable: {}").format(error))
        if (not stat.S_ISREG(command_stat.st_mode) or
                command_stat.st_uid != os.geteuid() or
                stat.S_IMODE(command_stat.st_mode) & 0o022 or
                not os.access(command, os.X_OK)):
            raise OSError(_("Trusted savechanges command is unavailable"))

        argv = [
            command, '--json', '--profile', 'exact', '--work-parent',
            work_parent, output_path,
        ]
        environment = {
            'PATH': '/usr/sbin:/usr/bin:/sbin:/bin',
            'LC_ALL': 'C.UTF-8',
            'LANG': 'C.UTF-8',
        }
        events = []
        with tempfile.TemporaryFile() as error_file:
            process = subprocess.Popen(
                argv, stdout=subprocess.PIPE, stderr=error_file,
                universal_newlines=True, start_new_session=True, env=environment)
            try:
                for line in process.stdout:
                    if not line.strip():
                        raise ValueError("savechanges emitted an empty machine-output line")
                    event = json.loads(
                        line, object_pairs_hook=self._strict_json_object,
                        parse_constant=self._reject_json_constant)
                    if not isinstance(event, dict):
                        raise ValueError("savechanges emitted an invalid event")
                    events.append(event)
                    if event.get('type') == 'phase' and progress_callback is not None:
                        progress_callback(event.get('phase'))
                process.wait()
            except BaseException:
                self._stop_capture_process(process)
                raise
            if process.returncode == 130:
                raise OSError(_("Session save was cancelled"))
            if process.returncode != 0:
                error_file.seek(0)
                detail = error_file.read(65536).decode('utf-8', 'replace').strip()
                message = _("savechanges failed with status {}").format(process.returncode)
                if detail:
                    message = "{}: {}".format(message, detail.splitlines()[-1])
                raise OSError(message)
        if len(events) != len(self.SAVECHANGES_PHASES) + 1:
            raise ValueError("savechanges emitted an incomplete result")
        phases = events[:-1]
        if ([event.get('phase') for event in phases] != list(self.SAVECHANGES_PHASES)
                or any(event.get('type') != 'phase' for event in phases)):
            raise ValueError("savechanges emitted an invalid phase sequence")

        result = events[-1]
        required_values = {
            'type': 'result',
            'product_kind': 'minios-tool-result',
            'schema_version': 1,
            'tool': 'savechanges',
            'operation': 'capture-module',
            'output': output_path,
            'profile': 'exact',
        }
        if any(result.get(key) != value for key, value in required_values.items()):
            raise ValueError("savechanges emitted an invalid result")
        if result.get('union_backend') not in ('aufs', 'overlayfs'):
            raise ValueError("savechanges emitted an invalid union backend")
        for field in ('compressed_size', 'uncompressed_size', 'entry_count'):
            value = result.get(field)
            if type(value) is not int or value < (1 if field == 'compressed_size' else 0):
                raise ValueError("savechanges emitted an invalid sizing result")
        footprint = result.get('extraction_footprint')
        footprint_fields = {
            'product_kind', 'schema_version', 'regular_file_bytes',
            'regular_file_inodes', 'directory_count', 'symlink_count',
            'symlink_target_bytes', 'whiteout_count', 'inode_count',
            'directory_entry_count', 'filename_bytes',
            'hardlink_reference_count', 'xattr_count', 'xattr_name_bytes',
            'xattr_value_bytes', 'compressor', 'block_size',
        }
        if not isinstance(footprint, dict) or set(footprint) != footprint_fields:
            raise ValueError("savechanges emitted an invalid extraction footprint")
        if (footprint['product_kind'] != 'minios-extraction-footprint' or
                type(footprint['schema_version']) is not int or
                footprint['schema_version'] != 1 or
                not isinstance(footprint['compressor'], str) or
                footprint['compressor'] not in ('zstd', 'gzip', 'lzo', 'xz')):
            raise ValueError("savechanges emitted an invalid extraction footprint")
        count_fields = footprint_fields - {
            'product_kind', 'schema_version', 'compressor', 'block_size',
        }
        if (any(type(footprint[field]) is not int or footprint[field] < 0
                for field in count_fields) or
                type(footprint['block_size']) is not int or
                footprint['block_size'] < 4096 or
                footprint['block_size'] > 1024 * 1024 or
                footprint['block_size'] & (footprint['block_size'] - 1) or
                footprint['directory_count'] < 1 or
                footprint['inode_count'] < 1 or
                footprint['regular_file_bytes'] != result['uncompressed_size'] or
                footprint['directory_entry_count'] != result['entry_count']):
            raise ValueError("savechanges emitted an invalid extraction footprint")
        regular_inodes = footprint['regular_file_inodes']
        directories = footprint['directory_count']
        symlinks = footprint['symlink_count']
        whiteouts = footprint['whiteout_count']
        hardlinks = footprint['hardlink_reference_count']
        if ((hardlinks and not regular_inodes) or
                footprint['directory_entry_count'] != (
                    directories - 1 + regular_inodes + hardlinks + symlinks + whiteouts) or
                footprint['inode_count'] != (
                    directories + regular_inodes + symlinks + whiteouts) or
                footprint['filename_bytes'] < footprint['directory_entry_count'] or
                footprint['symlink_target_bytes'] < symlinks or
                footprint['xattr_name_bytes'] < footprint['xattr_count']):
            raise ValueError("savechanges emitted an invalid extraction footprint")
        if not isinstance(result.get('sha256'), str) or not re.fullmatch(
                r'[0-9a-f]{64}', result['sha256']):
            raise ValueError("savechanges emitted an invalid digest")
        identity = result.get('output_identity')
        if not isinstance(identity, dict):
            raise ValueError("savechanges emitted an invalid output identity")
        for field in ('device', 'inode'):
            value = identity.get(field)
            if type(value) is not int or value < (1 if field == 'inode' else 0):
                raise ValueError("savechanges emitted an invalid output identity")
        return result

    def _validate_squashfs_runtime(self, session_id):
        """Bind saving to this boot's successful durable persistence activation."""
        if self.custom_sessions_dir is not None:
            return False, _("SquashFS saving is unavailable for a custom sessions directory")
        try:
            state_stat = os.stat(self.BOOT_STATE_FILE, follow_symlinks=False)
            if (not stat.S_ISREG(state_stat.st_mode) or
                    state_stat.st_uid != os.geteuid() or
                    state_stat.st_nlink != 1 or stat.S_IMODE(state_stat.st_mode) & 0o022):
                return False, _("Persistence runtime state is not trusted")
            state = {}
            with open(self.BOOT_STATE_FILE, 'r', encoding='utf-8') as state_file:
                for raw_line in state_file:
                    line = raw_line.rstrip('\n')
                    if not line or '\r' in line or '=' not in line:
                        return False, _("Persistence runtime state is malformed")
                    key, value = line.split('=', 1)
                    if key not in (
                            'boot_id', 'boot_level', 'mode', 'session', 'durable',
                            'writable', 'sessions_device', 'sessions_inode',
                            'active_generation') or key in state:
                        return False, _("Persistence runtime state is malformed")
                    state[key] = value
            with open(self.BOOT_ID_FILE, 'r', encoding='ascii') as boot_id_file:
                boot_id = boot_id_file.read().strip()
            required_state = {
                'boot_id': boot_id, 'boot_level': 'ok', 'mode': 'squashfs',
                'session': session_id, 'durable': '1', 'writable': '1',
                'active_generation': 'current',
            }
            if any(state.get(key) != value for key, value in required_state.items()):
                return False, _("The requested SquashFS session is not active in this boot")

            selected_stat = os.stat(self.sessions_dir, follow_symlinks=False)
            matching_roots = []
            for path in self.SESSION_PATHS:
                try:
                    root_stat = os.stat(path, follow_symlinks=False)
                except OSError:
                    continue
                matching_roots.append((root_stat.st_dev, root_stat.st_ino))
            selected_identity = (selected_stat.st_dev, selected_stat.st_ino)
            if (selected_identity not in matching_roots or
                    any(identity != selected_identity for identity in matching_roots)):
                return False, _("Selected persistence storage is ambiguous")
            if (state.get('sessions_device') != str(selected_stat.st_dev) or
                    state.get('sessions_inode') != str(selected_stat.st_ino)):
                return False, _("Selected persistence storage changed after activation")

            filesystem, error = self._detect_filesystem_type()
            if (error or not filesystem or filesystem.get('is_readonly') or
                    filesystem.get('type') not in self.DURABLE_SESSION_FILESYSTEMS):
                return False, _("Selected persistence storage is not durably writable")
            if filesystem.get('type') not in self.EXACT_STAGING_FILESYSTEMS:
                return False, _(
                    "Selected persistence storage cannot preserve exact capture metadata")
            return True, None
        except OSError as error:
            return False, _("Persistence runtime state is unavailable: {}").format(error)

    def _artifact_digest(self, directory_fd, name):
        digest = hashlib.sha256()
        artifact_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW,
                              dir_fd=directory_fd)
        try:
            while True:
                block = os.read(artifact_fd, 1024 * 1024)
                if not block:
                    break
                digest.update(block)
        finally:
            os.close(artifact_fd)
        return digest.hexdigest()

    def _validate_squashfs_capture(self, directory_fd, name, result):
        """Verify the exact-capture artifact before publishing it."""
        captured = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        result_identity = (
            result['output_identity']['device'],
            result['output_identity']['inode'],
        )
        if (not stat.S_ISREG(captured.st_mode) or captured.st_nlink != 1 or
                captured.st_uid != os.geteuid() or
                (captured.st_dev, captured.st_ino) != result_identity or
                captured.st_size != result['compressed_size']):
            raise OSError(_("Captured SquashFS identity or size changed"))
        module_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW,
                            dir_fd=directory_fd)
        try:
            if os.read(module_fd, 4) != b'hsqs':
                raise OSError(_("Captured file is not a SquashFS image"))
            os.fsync(module_fd)
        finally:
            os.close(module_fd)
        if self._artifact_digest(directory_fd, name) != result['sha256']:
            raise OSError(_("Captured SquashFS digest does not match its result"))
        return result_identity

    def _remove_captured_file(self, directory_fd, name, identity=None):
        """Remove only the expected private capture output."""
        try:
            current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        if (not stat.S_ISREG(current.st_mode) or current.st_uid != os.geteuid() or
                current.st_nlink != 1):
            return
        if identity is not None and (current.st_dev, current.st_ino) != identity:
            return
        os.unlink(name, dir_fd=directory_fd)
        self._sync_session_directory(directory_fd)

    def save_session(self, session_id, finalize_shutdown=False, progress_callback=None):
        """Save the running SquashFS session through the MiniOS Tools backend."""
        if self.custom_sessions_dir is not None:
            return False, _("SquashFS saving is unavailable for a custom sessions directory"), None
        try:
            session_id = self._validate_session_id(session_id)
        except ValueError as error:
            return False, str(error), None

        command = self.SQUASHFS_SAVE_COMMAND
        try:
            command_stat = os.stat(command, follow_symlinks=False)
        except OSError as error:
            return False, _("SquashFS save backend is unavailable: {}").format(error), None
        if (not stat.S_ISREG(command_stat.st_mode) or
                command_stat.st_uid != os.geteuid() or
                stat.S_IMODE(command_stat.st_mode) & 0o022 or
                not os.access(command, os.X_OK)):
            return False, _("SquashFS save backend is unavailable"), None

        argv = [command, session_id, '--json']
        if progress_callback is not None:
            argv.append('--progress')
        if finalize_shutdown:
            argv.append('--shutdown-finalize')
        environment = {
            'PATH': '/usr/sbin:/usr/bin:/sbin:/bin',
            'LC_ALL': 'C.UTF-8',
            'LANG': 'C.UTF-8',
        }
        final_result = None
        with tempfile.TemporaryFile() as error_file:
            process = subprocess.Popen(
                argv, stdout=subprocess.PIPE, stderr=error_file,
                universal_newlines=True, start_new_session=True, env=environment)
            try:
                for line in process.stdout:
                    if not line.strip():
                        continue
                    event = json.loads(line)
                    if event.get('type') == 'phase':
                        if progress_callback is not None:
                            progress_callback(event.get('phase'))
                    else:
                        final_result = event
                process.wait()
            except BaseException:
                self._stop_capture_process(process)
                raise
            error_file.seek(0)
            stderr = error_file.read(65536).decode('utf-8', 'replace').strip()

        if not isinstance(final_result, dict):
            return False, stderr or _("SquashFS save backend returned no result"), None
        success = bool(final_result.get('success')) and process.returncode == 0
        message = final_result.get('message') or stderr or (
            _("Session {} saved successfully").format(session_id) if success else
            _("Failed to save session {} ").format(session_id).strip())
        capture = final_result.get('capture') if success else None
        return success, str(message), capture

    def create_session(self, session_mode="native", size_mb=None, password=None,
                       policy=None, autosave=0):
        """Create a new session."""
        if not self.sessions_dir:
            return False, _("Sessions directory not found")
        
        # Validate session mode
        valid_modes = ["native", "squashfs", "dynfilefs", "raw", "luks"]
        if session_mode not in valid_modes:
            return False, _("Invalid session mode. Must be one of: {}").format(", ".join(valid_modes))
        squashfs_policy = policy or 'shutdown'
        if session_mode == 'squashfs' and squashfs_policy not in ('manual', 'shutdown'):
            return False, _("SquashFS save policy must be manual or shutdown")
        try:
            autosave = int(autosave or 0)
        except (TypeError, ValueError):
            return False, _("Invalid SquashFS automatic save interval")
        if session_mode == 'squashfs' and autosave not in self.SQUASHFS_AUTOSAVE_INTERVALS:
            return False, _("Invalid SquashFS automatic save interval")
        if session_mode != 'squashfs' and (policy is not None or autosave):
            return False, _("Save policy is available only for SquashFS sessions")
        
        # Check if sessions directory is writable
        dir_status = self.check_sessions_directory_status()
        if not dir_status.get('writable', False):
            error_msg = dir_status.get('error', _("Sessions directory is not writable"))
            return False, error_msg
        
        valid_target, target_error = self._validate_target_mode(session_mode, size_mb)
        if not valid_target:
            return False, target_error
        
        # Check dynfilefs availability for dynfilefs mode
        if session_mode == "dynfilefs" and not self._check_dynfilefs_available():
            return False, _("DynFileFS is not available on this system. Please install dynfilefs package.")

        # Check fixed/admitted sizes here. SquashFS capture performs its own
        # exact free-space admission in savechanges, so do not impose the
        # unrelated 4000 MB container default on snapshot creation.
        if session_mode != "squashfs":
            required_mb = size_mb if size_mb else self.DEFAULT_CONTAINER_SIZE_MB
            if session_mode == "dynfilefs":
                required_mb = min(required_mb, self.DYNFILEFS_INITIAL_MB)
            has_space, space_error = self._check_free_space(self.sessions_dir, required_mb)
            if not has_space:
                return False, space_error

        session_path = None
        squashfs_result = None
        try:
            new_id, session_path = self._reserve_session()
            
            # Initialize session based on mode
            if session_mode == "squashfs":
                new_name = '.changes.sb.new-{}'.format(os.urandom(16).hex())
                directory_fd = os.open(
                    session_path, os.O_RDONLY | os.O_DIRECTORY |
                    getattr(os, 'O_NOFOLLOW', 0))
                try:
                    new_path = os.path.join(session_path, new_name)
                    squashfs_result = self._run_savechanges(new_path, session_path)
                    self._validate_squashfs_capture(
                        directory_fd, new_name, squashfs_result)
                    self._sync_session_directory(directory_fd)
                    os.replace(new_name, 'changes.sb',
                               src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
                    self._sync_session_directory(directory_fd)
                finally:
                    os.close(directory_fd)

            elif session_mode == "dynfilefs":
                # Set default size if not specified
                if size_mb is None:
                    size_mb = self.DEFAULT_CONTAINER_SIZE_MB

                success, message = self._create_dynfilefs_session(session_path, size_mb)
                if not success:
                    # Clean up on failure
                    try:
                        shutil.rmtree(session_path)
                    except Exception:
                        pass
                    return False, message
            
            elif session_mode == "raw":
                # Create raw image file
                if size_mb is None:
                    size_mb = self.DEFAULT_CONTAINER_SIZE_MB
                
                image_file = os.path.join(session_path, "changes.img")
                try:
                    # Create image file with fallocate (works on both sparse and non-sparse filesystems)
                    size_bytes = size_mb * 1024 * 1024
                    result = subprocess.run(['fallocate', '-l', str(size_bytes), image_file],
                                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    if result.returncode != 0:
                        # Fallback to truncate if fallocate not available
                        with open(image_file, 'wb') as f:
                            f.truncate(size_bytes)

                    # Format with ext4
                    format_cmd = ['mke2fs', '-F', '-t', 'ext4', image_file]
                    format_result = subprocess.run(format_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    # Sync to ensure filesystem is written (important for FAT32/NTFS)
                    subprocess.run(['sync'], stdout=subprocess.PIPE, stderr=subprocess.PIPE)

                    if format_result.returncode != 0:
                        shutil.rmtree(session_path)
                        return False, _("Failed to format raw image file: {}").format(format_result.stderr.decode())
                        
                except Exception as e:
                    shutil.rmtree(session_path)
                    return False, _("Failed to create raw image file: {}").format(str(e))

            elif session_mode == 'luks':
                size_mb = size_mb or self.DEFAULT_CONTAINER_SIZE_MB
                # Build outside the reserved directory, then atomically publish it.
                staging_path = self._make_temp_dir()
                try:
                    self._create_luks_container(staging_path, size_mb, password)
                    os.rmdir(session_path)
                    os.rename(staging_path, session_path)
                except Exception:
                    shutil.rmtree(staging_path, ignore_errors=True)
                    shutil.rmtree(session_path, ignore_errors=True)
                    raise
            
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
                except Exception:
                    pass
            
            # Keep the metadata read-modify-write transaction under one lock.
            with self._mutation_lock():
                metadata = self._read_sessions_metadata()
                metadata.setdefault("sessions", {})
                session_record = {
                    "mode": session_mode, "version": version, "edition": edition,
                    "union": union
                }
                if session_mode == "squashfs":
                    session_record.update({
                        "policy": squashfs_policy,
                        "saved": datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
                        "digest": squashfs_result['sha256'],
                        "compressed": squashfs_result['compressed_size'],
                        "uncompressed": squashfs_result['uncompressed_size'],
                        "entries": squashfs_result['entry_count'],
                        "footprint": json.dumps(
                            squashfs_result['extraction_footprint'], sort_keys=True,
                            separators=(',', ':')),
                        "generation": 1,
                        "union": squashfs_result['union_backend'],
                    })
                    if autosave:
                        session_record['autosave'] = autosave
                    boot_id = self._current_boot_id()
                    if boot_id:
                        session_record['capture_boot_id'] = boot_id
                elif session_mode in ["dynfilefs", "raw", "luks"] and size_mb:
                    session_record["size"] = size_mb
                metadata["sessions"][new_id] = session_record
                metadata_updated = self._write_sessions_metadata(metadata)
            if metadata_updated:
                self._invalidate_size_cache(new_id)
                # Use safer string formatting to avoid potential translation issues
                try:
                    if session_mode == "dynfilefs":
                        message = _("Session {} created successfully (mode: {}, size: {}MB)").format(new_id, session_mode, size_mb)
                    elif session_mode in ("raw", "luks"):
                        message = _("Session {} created successfully (mode: {}, size: {}MB)").format(new_id, session_mode, size_mb)
                    else:
                        message = _("Session {} created successfully (mode: {})").format(new_id, session_mode)
                    return True, message
                except Exception:
                    # Fallback to simple English message if translation fails
                    if session_mode in ["dynfilefs", "raw", "luks"] and size_mb is not None:
                        return True, f"Session {new_id} created successfully (mode: {session_mode}, size: {size_mb}MB)"
                    else:
                        return True, f"Session {new_id} created successfully (mode: {session_mode})"
            else:
                # Clean up on metadata failure
                try:
                    shutil.rmtree(session_path)
                except Exception:
                    pass
                return False, _("Failed to update session metadata")
                
        except Exception as e:
            if session_path and os.path.exists(session_path):
                shutil.rmtree(session_path, ignore_errors=True)
            return False, _("Error creating session: {}").format(str(e))

    def delete_session(self, session_id, handoff=False):
        """Delete a session, optionally handing a RAM-backed SquashFS boot to its active snapshot."""
        if not self.sessions_dir:
            return False, _("Sessions directory not found")
            
        try:
            session_path = self._session_path(session_id, require_exists=True)
        except ValueError as e:
            return False, str(e)
        
        # Check if it's the current session
        current = self.get_current_session()
        if current and current['id'] == session_id:
            return False, _("Cannot delete currently active session")
        
        # A running unpacked SquashFS session can hand its save target to a
        # current-boot SquashFS snapshot that is already active for next boot.
        running = self.get_running_session()
        if running and running['id'] == session_id:
            if not handoff:
                return False, _("Cannot delete currently running session without SquashFS handoff")
            if not current or current['id'] == session_id:
                return False, _("Activate another SquashFS session before deleting the running session")
            try:
                success, message = self.handoff_running_squashfs(session_id, current['id'])
            except Exception as error:
                return False, _("Failed to hand off the running SquashFS session: {}").format(error)
            if not success:
                return False, message
        
        try:
            with self._mutation_lock():
                shutil.rmtree(session_path)
                metadata = self._read_sessions_metadata()
                if session_id in metadata.get("sessions", {}):
                    del metadata["sessions"][session_id]
                    if not self._write_sessions_metadata(metadata):
                        return False, _("Session deleted but failed to update session metadata")
            
            return True, _("Session {} deleted successfully").format(session_id)
        except Exception as e:
            return False, _("Error deleting session: {}").format(str(e))

    def cleanup_stale_temp_dirs(self, max_age_seconds=86400):
        """Clean up stale temporary directories in sessions folder"""
        if not self.sessions_dir or not os.path.exists(self.sessions_dir):
            return 0
            
        count = 0
        now = time.time()
        
        try:
            for item in os.listdir(self.sessions_dir):
                if item.startswith(".tmp_"):
                    path = os.path.join(self.sessions_dir, item)
                    if os.path.isdir(path):
                        try:
                            stat = os.stat(path)
                            if now - stat.st_mtime > max_age_seconds:
                                shutil.rmtree(path, ignore_errors=True)
                                count += 1
                        except OSError:
                            pass
        except OSError:
            pass
            
        return count

    def cleanup_old_sessions(self, days_threshold=30):
        """Clean up sessions older than specified days"""
        if days_threshold <= 0:
            return 0, [_('Days threshold must be greater than zero')]
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
        
        # Also cleanup stale temp dirs (older than 1 day)
        self.cleanup_stale_temp_dirs(max_age_seconds=86400)
        
        return deleted_count, errors

    def resize_session(self, session_id, new_size_mb, password=None):
        """Resize a session to new size"""
        if not self.sessions_dir:
            return False, _("Sessions directory not found")
            
        try:
            session_path = self._session_path(session_id, require_exists=True)
        except ValueError as e:
            return False, str(e)
        if new_size_mb <= 0:
            return False, _("Session size must be greater than zero")
        with self._mutation_lock():
            # Hold the lock across the data change and metadata commit.  A
            # resize cannot be atomic at the filesystem level, so callers
            # never see a completed data resize with stale metadata.
            metadata = self._read_sessions_metadata()
            session_data = metadata.get("sessions", {}).get(session_id, {})
            session_mode = session_data.get("mode", "unknown")
            if session_mode not in ["dynfilefs", "raw", "luks"]:
                return False, _("Resize is only supported for dynfilefs, raw, and LUKS mode sessions")
            valid_target, target_error = self._validate_target_mode(session_mode, new_size_mb)
            if not valid_target:
                return False, target_error
            running = self.get_running_session()
            if running and running['id'] == session_id:
                return False, _("Cannot resize currently running session")
            try:
                if session_mode == "dynfilefs":
                    return self._resize_dynfilefs_session(session_path, new_size_mb, session_id, metadata)
                if session_mode == 'luks':
                    return self._resize_luks_session(session_path, new_size_mb, session_id, metadata, password)
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
                
                # Check if virtual.dat was created/resized
                virtual_file = os.path.join(temp_mount, "virtual.dat")
                if not self._wait_for_mount(virtual_file):
                    process.terminate()
                    return False, _("Failed to create/resize virtual.dat file (timeout)")
                
                # Now we need to resize the filesystem inside virtual.dat
                # Resize the filesystem (will perform necessary checks)
                resize_cmd = ['resize2fs', '-f', virtual_file]
                resize_result = subprocess.run(resize_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                
                if resize_result.returncode != 0:
                    process.terminate()
                    return False, _("Failed to resize filesystem: {}").format(resize_result.stderr.decode())
                
                # Terminate the dynfilefs process (unmount)
                process.terminate()
                process.wait()
                
            finally:
                # Clean up: ensure unmount and remove temp directory
                subprocess.run(['fusermount', '-u', temp_mount], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                try:
                    os.rmdir(temp_mount)
                except Exception:
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
            original_size_bytes = os.path.getsize(image_file)
            current_size = original_size_bytes // (1024 * 1024)
            recorded_size = metadata.get("sessions", {}).get(session_id, {}).get("size")
            if isinstance(recorded_size, str):
                recorded_size = int(recorded_size)
            if current_size == new_size_mb and recorded_size != new_size_mb:
                # Recover an interrupted post-resize metadata commit without
                # touching an already-grown filesystem again.
                metadata["sessions"][session_id]["size"] = new_size_mb
                if self._write_sessions_metadata(metadata):
                    return True, _("Session {} resize metadata recovered").format(session_id)
                return False, _("Failed to recover resize metadata")
            if new_size_mb <= current_size:
                return False, _("New size must be larger than current size ({}MB)").format(current_size)
            
            # Truncate image file to new size
            new_size_bytes = new_size_mb * 1024 * 1024
            with open(image_file, 'r+b') as f:
                f.truncate(new_size_bytes)
                f.flush()
                os.fsync(f.fileno())
            
            # Resize the filesystem inside the image (will perform necessary checks)
            resize_cmd = ['resize2fs', '-f', image_file]
            resize_result = subprocess.run(resize_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            
            if resize_result.returncode != 0:
                # resize2fs failed before commit: restore the exact container
                # length so this operation is safely retryable.
                with open(image_file, 'r+b') as f:
                    f.truncate(original_size_bytes)
                    f.flush()
                    os.fsync(f.fileno())
                return False, _("Failed to resize filesystem: {}").format(resize_result.stderr.decode())
            
            # Update metadata with new size
            metadata["sessions"][session_id]["size"] = new_size_mb
            if not self._write_sessions_metadata(metadata):
                return False, _("Failed to update session metadata")
            
            return True, _("Session {} resized to {}MB successfully").format(session_id, new_size_mb)
            
        except Exception as e:
            return False, _("Failed to resize raw session: {}").format(str(e))

    def _resize_luks_session(self, session_path, new_size_mb, session_id, metadata, password):
        """Grow a LUKS file and its ext4 filesystem; shrinking is deliberately unsupported."""
        image_file = os.path.join(session_path, 'changes.luks')
        journal_path = os.path.join(session_path, '.luks-resize.json')
        if not os.path.exists(image_file):
            return False, _("LUKS container not found")
        original_size = os.path.getsize(image_file)
        current_size = original_size // (1024 * 1024)
        recorded_size = metadata.get('sessions', {}).get(session_id, {}).get('size')
        try:
            with open(journal_path, encoding='utf-8') as journal_file:
                pending_size = int(json.load(journal_file)['size'])
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            pending_size = None
        if pending_size and current_size >= pending_size and recorded_size != pending_size:
            # A previous filesystem grow completed but its metadata commit did
            # not.  The journal makes that committed data state recoverable.
            metadata['sessions'][session_id]['size'] = pending_size
            if self._write_sessions_metadata(metadata):
                try:
                    os.unlink(journal_path)
                except OSError:
                    pass
                return True, _("Session {} resize metadata recovered").format(session_id)
            return False, _("Failed to recover resize metadata")
        if current_size == new_size_mb and recorded_size != new_size_mb:
            metadata['sessions'][session_id]['size'] = new_size_mb
            if self._write_sessions_metadata(metadata):
                return True, _("Session {} resize metadata recovered").format(session_id)
            return False, _("Failed to recover resize metadata")
        if new_size_mb <= current_size:
            return False, _("New size must be larger than current size ({}MB)").format(current_size)
        filesystem_grown = False
        temporary_journal = None
        try:
            fd, temporary_journal = tempfile.mkstemp(prefix='.luks-resize-', dir=session_path)
            with os.fdopen(fd, 'w') as journal_file:
                json.dump({'size': new_size_mb}, journal_file)
                journal_file.flush()
                os.fsync(journal_file.fileno())
            os.replace(temporary_journal, journal_path)
            with open(image_file, 'r+b') as image:
                image.truncate(new_size_mb * 1024 * 1024)
                image.flush()
                os.fsync(image.fileno())
            with self._mount_luks(image_file, password, writable=True) as mount_point:
                # The mapper is mounted, so resize its filesystem device directly.
                device = subprocess.run(['findmnt', '-no', 'SOURCE', '--target', mount_point],
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True).stdout.decode().strip()
                result = subprocess.run(['resize2fs', '-f', device], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                if result.returncode:
                    raise OSError(result.stderr.decode(errors='replace'))
                filesystem_grown = True
            metadata['sessions'][session_id]['size'] = new_size_mb
            if not self._write_sessions_metadata(metadata):
                return False, _("Failed to update session metadata")
            try:
                os.unlink(journal_path)
            except OSError:
                pass
            return True, _("Session {} resized to {}MB successfully").format(session_id, new_size_mb)
        except Exception as error:
            # Never shrink a filesystem that may already have grown.
            if not filesystem_grown:
                with open(image_file, 'r+b') as image:
                    image.truncate(original_size)
                try:
                    os.unlink(journal_path)
                except OSError:
                    pass
            if temporary_journal and os.path.exists(temporary_journal):
                os.unlink(temporary_journal)
            return False, _("Failed to resize LUKS session: {}").format(str(error))

    def export_session(self, session_id, output_path, verify=True, password=None):
        """Export session to TAR.ZSTD archive (streaming)

        Args:
            session_id: ID of session to export
            output_path: Path to output file or directory
            verify: Verify archive after creation

        Returns:
            (success, message) tuple
        """
        # Get session info
        try:
            session_id = self._validate_session_id(session_id)
        except ValueError as e:
            return False, str(e)
        session_info = self._get_session_info(session_id)
        if not session_info:
            return False, _("Session #{} not found").format(session_id)
        if session_info.get('mode') == 'squashfs':
            return False, _("SquashFS export is unavailable until save support is complete")

        try:
            session_path = self._session_path(session_id, require_exists=True)
        except ValueError as e:
            return False, str(e)

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

        output_dir = os.path.dirname(output_file) or '.'
        try:
            output_stat = os.lstat(output_file)
            if not stat.S_ISREG(output_stat.st_mode):
                return False, _("Export output must be a regular file")
        except FileNotFoundError:
            pass

        # Check free disk space (estimate: session size + 50% for compression overhead)
        session_size_mb = session_info.get('size', 0) / (1024 * 1024)
        estimated_archive_mb = session_size_mb * 1.5
        has_space, space_error = self._check_free_space(output_dir, estimated_archive_mb)
        if not has_space:
            return False, space_error

        temp_output = None
        try:
            output_fd, temp_output = tempfile.mkstemp(prefix='.session-export-', suffix='.tar.zst', dir=output_dir)
            os.close(output_fd)
            # Use temp directory in RAM (default temp) for metadata
            with tempfile.TemporaryDirectory() as meta_dir:
                # Prepare metadata
                metadata = self._prepare_export_metadata(session_info)
                metadata_file = os.path.join(meta_dir, 'metadata.json')
                with open(metadata_file, 'w') as f:
                    json.dump(metadata, f, indent=2)

                # Create human-readable info
                info_file = os.path.join(meta_dir, 'session.info')
                self._create_session_info_file(session_info, info_file)

                # Mount session and stream to archive
                with self._mount_session_read(session_path, session_info['mode'], password=password) as source_dir:
                    # Exclude list
                    excludes = [
                        '--exclude=workdir',
                        '--exclude=.wh..wh.orph',
                        '--exclude=.wh..wh.plnk',
                        '--exclude=.wh..wh.aufs'
                    ]
                    
                    # Construct tar command
                    # We use transform to put metadata at root and files in data/
                    # Use 'S' flag to prevent transforming symlink targets
                    cmd = [
                        'tar', '-cf', temp_output,
                        '--use-compress-program=zstd -3 -T0',
                        '-C', meta_dir, 'metadata.json', 'session.info',
                        '--transform', 's,^,data/,S',
                    ] + excludes + [
                        '-C', source_dir, '.'
                    ]
                    
                    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    if result.returncode != 0:
                        raise Exception(result.stderr.decode())

            # Verify if requested
            if verify:
                if not self._verify_export(temp_output):
                    return False, _("Export verification failed")

            file_size = os.path.getsize(temp_output)
            pkexec_uid = os.environ.get('PKEXEC_UID')
            if pkexec_uid is not None:
                os.chown(temp_output, int(pkexec_uid), -1)
            # os.replace is atomic and never follows an existing destination
            # symlink; the archive becomes visible only after verification.
            os.replace(temp_output, output_file)
            temp_output = None
            return True, _("Session exported successfully to {} ({})").format(
                output_file, self._format_size(file_size))

        except Exception as e:
            # Clean up on error
            return False, _("Export failed: {}").format(str(e))
        finally:
            if temp_output and os.path.exists(temp_output):
                os.unlink(temp_output)

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
            # Extract only the upperdir content, not OverlayFS structure
            # Check for OverlayFS structure (changes directory)
            changes_dir = os.path.join(session_path, 'changes')
            if os.path.exists(changes_dir) and os.path.isdir(changes_dir):
                # OverlayFS: extract from changes directory
                source_dir = changes_dir
            else:
                # AUFS or direct: files are in session_path itself
                source_dir = session_path

            # Copy files using rsync, excluding OverlayFS/AUFS metadata
            os.makedirs(extract_path, exist_ok=True)

            # Use rsync with filters to exclude problematic files
            # Keep user .wh.* files (AUFS whiteouts for deleted files)
            rsync_cmd = [
                'rsync', '-aH',  # archive mode + preserve hard links
                '--exclude=workdir',              # OverlayFS work directory
                '--exclude=.wh..wh.orph',         # AUFS orphan directory
                '--exclude=.wh..wh.plnk',         # AUFS pseudo-link directory
                '--exclude=.wh..wh.aufs',         # AUFS metadata file
                '--prune-empty-dirs',
            ]

            # Add trailing slashes for directory contents
            rsync_cmd.append(source_dir.rstrip('/') + '/')
            rsync_cmd.append(extract_path.rstrip('/') + '/')

            try:
                result = subprocess.run(rsync_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

                if result.returncode != 0:
                    stderr_text = result.stderr.decode() if result.stderr else ""
                    raise Exception(f"rsync failed with code {result.returncode}: {stderr_text}")

            except Exception as e:
                raise Exception(f"Failed to copy session files: {e}")

            return extract_path

        elif mode == 'dynfilefs':
            # Mount dynfilefs and extract files
            return self._extract_from_dynfilefs(session_path, extract_path)

        elif mode == 'raw':
            # Mount raw image and extract files
            return self._extract_from_raw(session_path, extract_path)

        else:
            raise Exception(_("Unknown session mode: {}").format(mode))





    def _verify_export(self, archive_file):
        """Verify TAR.ZSTD archive integrity"""
        try:
            # Test archive integrity
            cmd = ['tar', '-tf', archive_file, '--use-compress-program=zstd -T0']
            result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

            if result.returncode != 0:
                return False

            # Verify metadata.json exists
            output = result.stdout.decode()
            if 'metadata.json' not in output or 'session.info' not in output:
                return False

            return True
        except Exception:
            return False

    def _validate_archive(self, archive_path):
        """Reject unsafe members and cap extraction before writing any data."""
        try:
            result = subprocess.run(
                ['tar', '-tvf', archive_path, '--use-compress-program=zstd -T0', '--numeric-owner'],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
            if result.returncode != 0:
                return False, _("Invalid or unreadable archive")
            total_size = 0
            for line in result.stdout.decode('utf-8', 'strict').splitlines():
                # GNU tar verbose listings start with the member type; the
                # size is the third field when --numeric-owner is specified.
                fields = line.split(maxsplit=5)
                if len(fields) < 6 or fields[0][0] not in '-dlh':
                    return False, _("Archive contains an unsupported member type")
                try:
                    member_size = int(fields[2])
                except ValueError:
                    return False, _("Archive contains invalid member metadata")
                total_size += member_size
                if total_size > self.MAX_ARCHIVE_EXTRACTED_BYTES:
                    return False, _("Archive expands beyond the allowed size")
                name = fields[5].split(' -> ', 1)[0].split(' link to ', 1)[0]
                normalized = os.path.normpath(name)
                if (name.startswith('/') or '..' in Path(name).parts
                        or normalized in ('.', '..') or normalized.startswith('../')):
                    return False, _("Archive contains an unsafe member path")
                if normalized not in ('metadata.json', 'session.info', 'data') and not normalized.startswith('data/'):
                    return False, _("Archive contains files outside its data directory")
                if fields[0][0] in 'lh' and (' -> ' in fields[5] or ' link to ' in fields[5]):
                    target = fields[5].split(' -> ', 1)[-1].split(' link to ', 1)[-1]
                    target_path = os.path.normpath(target)
                    if (target.startswith('/') or '..' in Path(target).parts
                            or target_path == '..' or target_path.startswith('../')):
                        return False, _("Archive contains an unsafe link target")
            return True, None
        except (OSError, UnicodeDecodeError):
            return False, _("Invalid or unreadable archive")

    def _validate_import_metadata(self, metadata):
        """Validate the small, untrusted metadata document before using it."""
        if not isinstance(metadata, dict) or not isinstance(metadata.get('session'), dict):
            return False
        session = metadata['session']
        return (session.get('mode') in ('native', 'dynfilefs', 'raw', 'luks')
                and all(isinstance(session.get(key), str) for key in ('version', 'edition', 'union')))

    def import_session(self, archive_path, auto_convert=False, force_mode=None,
                       verify=True, skip_compatibility_check=False, password=None):
        """Import session from TAR.ZSTD archive (streaming)

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

        try:
            valid_archive, archive_error = self._validate_archive(archive_path)
            if not valid_archive:
                return False, archive_error
            # Extract metadata directly from archive
            metadata = self._extract_metadata(archive_path)
            if not self._validate_import_metadata(metadata):
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

            valid_target, target_error = self._validate_target_mode(import_mode)
            if not valid_target:
                return False, target_error

            # Check free disk space
            session_size_bytes = metadata['session'].get('size', 4000 * 1024 * 1024)
            if isinstance(session_size_bytes, int) and session_size_bytes > 100000:
                required_mb = session_size_bytes / (1024 * 1024)
            else:
                required_mb = session_size_bytes if isinstance(session_size_bytes, int) else 4000
            has_space, space_error = self._check_free_space(self.sessions_dir, required_mb)
            if not has_space:
                return False, space_error

            # Reserve the destination before doing long-running extraction.
            new_id, session_path = self._reserve_session()
            
            # Determine size for container modes
            size_mb = None
            if import_mode in ['dynfilefs', 'raw', 'luks']:
                size_mb = max(100, int(required_mb))

            # Import directly using streaming
            try:
                with self._mount_session_write(session_path, import_mode, size_mb, password=password) as target_dir:
                    # The validated archive may only write data/ members.
                    cmd = [
                        'tar', '-xf', archive_path,
                        '--use-compress-program=zstd -T0',
                        '-C', target_dir,
                        '--no-same-owner', '--no-same-permissions',
                        '--delay-directory-restore',
                        '--strip-components=1',
                        '--wildcards', 'data/*'
                    ]
                    
                    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    if result.returncode != 0:
                        raise Exception(f"Extraction failed: {result.stderr.decode()}")

            except Exception as e:
                if os.path.exists(session_path):
                    shutil.rmtree(session_path)
                return False, _("Failed to import session data: {}").format(str(e))

            # Verify before committing metadata so failures never leave a
            # metadata entry pointing at a removed session directory.
            if verify:
                if not self._verify_session_integrity(new_id):
                    shutil.rmtree(session_path)
                    return False, _("Imported session failed verification")

            if not self._create_session_metadata(new_id, import_mode, metadata):
                shutil.rmtree(session_path, ignore_errors=True)
                return False, _("Failed to update session metadata")

            return True, _("Session imported successfully as #{}").format(new_id)

        except Exception as e:
            return False, _("Import failed: {}").format(str(e))

    def _extract_metadata(self, archive_path, tmpdir=None):
        """Extract and return metadata from archive (reads directly from archive)"""
        try:
            # Extract metadata.json to stdout
            cmd = ['tar', '-xO', '-f', archive_path,
                   '--use-compress-program=zstd',
                   'metadata.json']
            result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

            if result.returncode != 0:
                # Try with data/ prefix (unified format)
                cmd = ['tar', '-xO', '-f', archive_path,
                       '--use-compress-program=zstd',
                       'data/metadata.json']
                result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                
                if result.returncode != 0:
                    return None

            # Parse JSON from stdout
            return json.loads(result.stdout.decode('utf-8'))
        except Exception:
            return None

    def _extract_archive(self, archive_path, extract_path):
        """Extract full archive"""
        cmd = ['tar', '-xf', archive_path,
               '--use-compress-program=zstd -T0',
               '-C', extract_path]
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
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









    def _get_next_session_id(self):
        """Get next available session ID"""
        existing_ids = []
        for item in os.listdir(self.sessions_dir):
            path = os.path.join(self.sessions_dir, item)
            if item.isdigit() and os.path.isdir(path) and not os.path.islink(path):
                existing_ids.append(int(item))

        if not existing_ids:
            return 1

        return max(existing_ids) + 1

    def _create_session_metadata(self, session_id, mode, import_metadata):
        """Create metadata entry for imported session"""
        with self._mutation_lock():
            metadata = self._read_sessions_metadata()
            metadata.setdefault('sessions', {})
            session_data = {
                'mode': mode, 'version': import_metadata['session']['version'],
                'edition': import_metadata['session']['edition'],
                'union': import_metadata['session']['union']
            }
            if mode in ['dynfilefs', 'raw', 'luks']:
                size = import_metadata['session'].get('size', 4000)
                if isinstance(size, int):
                    session_data['size'] = max(100, int(size / (1024 * 1024))) if size > 100000 else max(100, size)
            metadata['sessions'][str(session_id)] = session_data
            return self._write_sessions_metadata(metadata)

    def _verify_session_integrity(self, session_id):
        """Verify session integrity"""
        session_path = os.path.join(self.sessions_dir, str(session_id))
        return os.path.exists(session_path) and os.path.isdir(session_path)

    def copy_session(self, session_id, to_mode=None, size_mb=None, password=None):
        """Copy session, optionally converting mode

        Args:
            session_id: Source session ID
            to_mode: Target mode (None = same as source)
            size_mb: Size for dynfilefs/raw modes

        Returns:
            (success, message) tuple
        """
        # Get source session
        try:
            session_id = self._validate_session_id(session_id)
        except ValueError as e:
            return False, str(e)
        source_session = self._get_session_info(session_id)
        if not source_session:
            return False, _("Source session not found")

        try:
            source_path = self._session_path(session_id, require_exists=True)
        except ValueError as e:
            return False, str(e)
        source_mode = source_session['mode']
        if source_mode == 'squashfs':
            return False, _("SquashFS copy is unavailable until save support is complete")

        # Determine target mode
        target_mode = to_mode if to_mode else source_mode
        valid_target, target_error = self._validate_target_mode(target_mode, size_mb)
        if not valid_target:
            return False, target_error
        if target_mode == 'luks' and size_mb is None and source_mode != 'luks':
            size_mb = self.DEFAULT_CONTAINER_SIZE_MB

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

        new_id, target_path = self._reserve_session()

        try:
            if source_mode == target_mode:
                # Direct copy
                success = self._copy_session_direct(source_path, target_path, source_mode)
            else:
                # Copy with conversion
                success = self._copy_session_with_conversion(
                    source_path, target_path, source_mode, target_mode, size_mb, password)

            if not success:
                if os.path.exists(target_path):
                    shutil.rmtree(target_path)
                return False, _("Failed to copy session data")

            with self._mutation_lock():
                metadata = self._read_sessions_metadata()
                metadata.setdefault('sessions', {})
                metadata['sessions'][str(new_id)] = {
                    'mode': target_mode, 'version': source_session['version'],
                    'edition': source_session['edition'], 'union': source_session['union']
                }
                if target_mode in ['dynfilefs', 'raw', 'luks']:
                    if size_mb:
                        metadata['sessions'][str(new_id)]['size'] = size_mb
                    elif source_mode == target_mode and source_session.get('total_size_mb'):
                        metadata['sessions'][str(new_id)]['size'] = source_session['total_size_mb']
                if not self._write_sessions_metadata(metadata):
                    raise OSError(_("Failed to update session metadata"))

            return True, _("Session copied successfully to #{}").format(new_id)

        except Exception as e:
            if os.path.exists(target_path):
                shutil.rmtree(target_path)
            return False, _("Copy failed: {}").format(str(e))

    def _copy_session_direct(self, source_path, target_path, mode):
        """Direct copy of session without conversion"""
        try:
            if mode == 'native':
                # Ensure target directory exists (including 'changes' subdirectory if needed)
                # For native copy, we are copying contents of source_path (which might be .../changes)
                # to target_path.
                
                # If source path ends with 'changes', we should probably ensure target has 'changes' too
                # But _copy_session_direct receives raw paths.
                # Let's look at how it's called.
                # copy_session calls it with (source_path, target_path, mode)
                # source_path is .../100, target_path is .../101
                
                # Wait, _copy_session_direct does NOT use _mount_session_read/write!
                # It operates on raw paths!
                
                # If mode is native:
                # source_path is .../100
                # target_path is .../101
                
                # We need to check if source has 'changes' subdir
                source_changes = os.path.join(source_path, 'changes')
                if os.path.exists(source_changes) and os.path.isdir(source_changes):
                    real_source = source_changes
                    real_target = os.path.join(target_path, 'changes')
                else:
                    real_source = source_path
                    real_target = target_path
                    
                os.makedirs(real_target, exist_ok=True)

                # Use rsync for native copy to preserve attributes and handle special files
                # Exclude workdir and AUFS/OverlayFS metadata
                cmd = [
                    'rsync', '-aH',
                    '--exclude=workdir',
                    '--exclude=.wh..wh.orph',
                    '--exclude=.wh..wh.plnk',
                    '--exclude=.wh..wh.aufs',
                    real_source.rstrip('/') + '/',
                    real_target.rstrip('/') + '/'
                ]
                
                result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                
                if result.returncode != 0:
                    print(f"DEBUG: rsync failed: {result.stderr.decode()}")
                    return False
                
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

            elif mode == 'luks':
                shutil.copy2(os.path.join(source_path, 'changes.luks'),
                             os.path.join(target_path, 'changes.luks'))
                return True

            return False
        except Exception as e:
            print(f"DEBUG: _copy_session_direct failed: {e}")
            import traceback
            traceback.print_exc()
            return False

    def _copy_session_with_conversion(self, source_path, target_path,
                                      source_mode, target_mode, size_mb=None, password=None):
        """Copy session with mode conversion"""
        try:
            # Direct copy using mounts
            with self._mount_session_read(source_path, source_mode, password=password) as source_dir:
                with self._mount_session_write(target_path, target_mode, size_mb, password=password) as target_dir:
                    # Copy files using rsync
                    cmd = [
                        'rsync', '-aH',
                        '--exclude=workdir',
                        '--exclude=.wh..wh.orph',
                        '--exclude=.wh..wh.plnk',
                        '--exclude=.wh..wh.aufs',
                        '--prune-empty-dirs',
                        source_dir.rstrip('/') + '/',
                        target_dir.rstrip('/') + '/'
                    ]
                    
                    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    if result.returncode != 0:
                        raise Exception(f"Rsync failed: {result.stderr.decode()}")
            
            return True

        except Exception as e:
            print(f"DEBUG: _copy_session_with_conversion failed: {e}")
            import traceback
            traceback.print_exc()
            return False

    def convert_session(self, session_id, target_mode, size_mb=None,
                       in_place=True, password=None):
        """Convert session storage mode

        Args:
            session_id: Session ID to convert
            target_mode: Target mode (native/dynfilefs/raw)
            size_mb: Size for dynfilefs/raw modes
            in_place: Convert in-place (vs create new session)

        Returns:
            (success, message) tuple
        """
        try:
            session_id = self._validate_session_id(session_id)
        except ValueError as e:
            return False, str(e)
        # --new-session must leave the original untouched and create a new ID.
        if not in_place:
            if password is None:
                return self.copy_session(session_id, to_mode=target_mode, size_mb=size_mb)
            return self.copy_session(session_id, to_mode=target_mode, size_mb=size_mb, password=password)

        # Get session
        session_info = self._get_session_info(session_id)
        if not session_info:
            return False, _("Session not found")

        source_mode = session_info['mode']
        if source_mode == 'squashfs':
            return False, _("SquashFS conversion is unavailable until save support is complete")

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

        valid_target, target_error = self._validate_target_mode(target_mode, size_mb)
        if not valid_target:
            return False, target_error

        # Set default size for dynfilefs/raw if not specified
        if target_mode in ['dynfilefs', 'raw', 'luks'] and not size_mb:
            # Try to use size from source session if available
            if session_info.get('total_size_mb'):
                size_mb = session_info['total_size_mb']
            elif source_mode == 'native' and session_info.get('size'):
                # For native sessions, use actual size + 100 MB
                size_mb = int(session_info['size'] / (1024 * 1024)) + 100
            else:
                size_mb = self.DEFAULT_CONTAINER_SIZE_MB

        session_path = self._session_path(session_id, require_exists=True)

        # Create new session with temporary name
        new_session_path = self._make_temp_dir()

        try:
            if not self._copy_session_with_conversion(session_path, new_session_path,
                                                       source_mode, target_mode, size_mb, password):
                raise OSError(_("Failed to copy session data"))

            # Keep a rollback copy until the durable metadata update succeeds.
            backup_path = self._make_temp_dir()
            os.rmdir(backup_path)
            with self._mutation_lock():
                os.rename(session_path, backup_path)
                os.rename(new_session_path, session_path)
                metadata = self._read_sessions_metadata()
                if session_id not in metadata.get('sessions', {}):
                    os.rename(session_path, new_session_path)
                    os.rename(backup_path, session_path)
                    raise OSError(_("Session metadata is missing"))
                metadata['sessions'][session_id]['mode'] = target_mode
                if target_mode in ['dynfilefs', 'raw', 'luks']:
                    metadata['sessions'][session_id]['size'] = size_mb
                else:
                    metadata['sessions'][session_id].pop('size', None)
                if not self._write_sessions_metadata(metadata):
                    os.rename(session_path, new_session_path)
                    os.rename(backup_path, session_path)
                    raise OSError(_("Failed to update session metadata"))
            shutil.rmtree(backup_path)

            return True, _("Session converted successfully from {} to {}").format(
                source_mode, target_mode)

        except Exception as e:
            # Clean up temp new session on error
            shutil.rmtree(new_session_path, ignore_errors=True)
            return False, _("Conversion failed: {}").format(str(e))



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
            'policy': session.get('policy'),
            'autosave': int(session.get('autosave') or 0),
            'saved': session.get('saved'),
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
        'policy': session.get('policy'),
        'autosave': int(session.get('autosave') or 0),
        'saved': session.get('saved'),
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

def parse_perch_size(value):
    """Parse perchsize-compatible decimal units into MB."""
    match = re.fullmatch(r'([0-9]+)\s*([mMgGtT]?[bB]?)?', value)
    if not match:
        raise argparse.ArgumentTypeError('size must be a positive MB, GB, or TB value')
    number, unit = int(match.group(1)), match.group(2).lower()
    multiplier = 1000000 if unit.startswith('t') else 1000 if unit.startswith('g') else 1
    size_mb = number * multiplier
    if not size_mb or size_mb > SessionManager.MAX_CONTAINER_SIZE_MB:
        raise argparse.ArgumentTypeError('size must be between 1MB and 1TB')
    return size_mb

def luks_password_from_args(args, confirm=False):
    """Read an optional passphrase without ever putting it in argv or metadata."""
    if getattr(args, 'password_stdin', False):
        password = sys.stdin.buffer.readline().rstrip(b'\r\n')
        if confirm:
            confirmation = sys.stdin.buffer.readline().rstrip(b'\r\n')
            if password != confirmation:
                raise ValueError(_('LUKS passphrases do not match.'))
    elif confirm:
        password = getpass.getpass(_('LUKS passphrase: ')).encode()
        confirmation = getpass.getpass(_('Confirm LUKS passphrase: ')).encode()
        if password != confirmation:
            raise ValueError(_('LUKS passphrases do not match.'))
    else:
        return None
    if not password:
        raise ValueError(_('LUKS passphrase must not be empty.'))
    return password



def main():
    """Main application entry point"""
    import sys

    # Pre-check for flags before parsing
    json_output = '--json' in sys.argv
    help_requested = any(arg in ('-h', '--help') for arg in sys.argv[1:])

    # Check for root privileges
    if os.geteuid() != 0 and not help_requested:
        error_msg = _("This tool requires root privileges. Please run with sudo or through pkexec.")
        if json_output:
            print(json.dumps({"success": False, "error": error_msg}), file=sys.stderr)
        else:
            print(error_msg, file=sys.stderr)
        sys.exit(1)

    luks_available = luks_runtime_available()
    session_modes = ['native', 'squashfs', 'dynfilefs', 'raw']
    if luks_available:
        session_modes.append('luks')

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
  activate SESSION_ID       Activate specified session
  save SESSION_ID           Save the running SquashFS session
  create [MODE] [SIZE]      Create new session using positional arguments
                              SquashFS: --policy manual|shutdown, --autosave MINUTES
  settings SESSION_ID       Configure SquashFS shutdown and periodic saving
  delete SESSION_ID         Delete specified session
  cleanup [--days N]        Delete old sessions (default: 30 days)
  status                    Check sessions directory status and permissions
  resize SESSION_ID SIZE    Grow a container session
  export SESSION_ID PATH    Export session to .tar.zst archive
  import ARCHIVE            Import session from .tar.zst archive
  copy SESSION_ID           Copy session (optional: --to-mode, --size)
  convert SESSION_ID MODE   Convert session to different mode (optional: --size)

SESSION MODES:
  native                    Direct filesystem changes (requires POSIX-compatible filesystem)
  squashfs                  Compressed snapshot of the current live changes
  dynfilefs                 Dynamic file system overlay (works on any filesystem)
  raw                       Raw disk image (works on any filesystem, 4000MB default)

COMMAND BEHAVIOR:
  • create without MODE: Uses native mode (may fail on FAT32/NTFS/exFAT)
  • create without SIZE: Uses 4000MB for fixed/container modes; SquashFS has no fixed size
  • SquashFS periodic saves accept 30, 60, 120, 240, or 480 minutes; 0 disables them
  • convert replaces the source by default; --new-session preserves it
  • cleanup without --days: Uses 30-day threshold for deletion
  • cleanup protects both active and running sessions from deletion
  • explicit delete can hand off a running RAM-backed SquashFS session to a current-boot active snapshot

EXAMPLES:

  Basic Usage:
    minios-session list                           List all available sessions
    minios-session active                         Show which session will boot next
    minios-session running                        Show currently running session
    minios-session info                           Show filesystem compatibility info

  Session Management:
    minios-session activate 2                     Set session #2 as default for next boot
    minios-session settings 2 --shutdown on --autosave 60  Configure SquashFS automatic saves
    minios-session delete 3                       Delete session #3 permanently
    minios-session delete 1 --handoff             Hand off running SquashFS to active snapshot and delete #1
    minios-session cleanup --days 30              Delete sessions older than 30 days
    minios-session cleanup                        Delete sessions older than 30 days (default)

  Creating Sessions:
    minios-session create                         Create native session (filesystem changes)
    minios-session create native                  Create native session explicitly
    minios-session create squashfs                Snapshot current live changes into a SquashFS session
    minios-session create dynfilefs               Create 4000MB dynfilefs session (default size)
    minios-session create dynfilefs 8000          Create 8000MB dynfilefs session
    minios-session create raw 2000                Create 2000MB raw disk image

  Session Operations:
    minios-session resize 2 4000                  Resize session #2 to 4000MB
    minios-session export 3 /tmp/backup.tar.zst   Export session #3 to archive
    minios-session import /tmp/backup.tar.zst     Import session from archive
    minios-session copy 2                         Copy session #2
    minios-session copy 2 --to-mode raw --size 3000  Copy session #2 as raw with 3000MB
    minios-session convert 3 dynfilefs --size 2000   Convert session #3 to dynfilefs with 2000MB

  Error Handling:
    minios-session create                         May fail on FAT32/NTFS/exFAT: "Use dynfilefs or raw mode"
    minios-session create raw 5000                Will fail on FAT32: MiniOS limits fixed containers to 4000MB

  JSON Output (for automation):
    minios-session --json list                    List sessions in JSON format
    minios-session active --json                  Get active session info as JSON
    minios-session info --json                    Get system info as JSON

  Custom Session Directory:
    minios-session --sessions-dir /mnt/usb/sessions list
    minios-session --sessions-dir /tmp/test create native

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
    if luks_available:
        parent_parser.add_argument('--password-stdin', action='store_true',
                                  help=_('Read a LUKS passphrase from standard input'))

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

    # Save command
    save_parser = subparsers.add_parser('save', help=_('Save a SquashFS session'), parents=[parent_parser])
    save_parser.add_argument('session_id', help=_('Running SquashFS session ID to save'))
    save_parser.add_argument('--progress', action='store_true',
                             help=_('Stream save progress events with JSON output'))
    save_parser.add_argument('--shutdown-finalize', action='store_true',
                             help=argparse.SUPPRESS)

    # Create command
    create_parser = subparsers.add_parser('create', help=_('Create a new session'), parents=[parent_parser])
    create_parser.add_argument('mode', nargs='?', choices=session_modes,
                              default='native', help=_('Session mode (default: native)'))
    create_parser.add_argument('size', nargs='?', type=parse_perch_size, metavar='SIZE',
                              help=_('Size in MB, GB, or TB for container modes (default: 4000MB)'))
    create_parser.add_argument('--policy', choices=('manual', 'shutdown'),
                              help=_('SquashFS shutdown save policy (default: shutdown)'))
    create_parser.add_argument('--autosave', type=int,
                              choices=SessionManager.SQUASHFS_AUTOSAVE_INTERVALS,
                              default=0,
                              help=_('Periodic SquashFS save interval in minutes (0 disables it)'))

    # Save settings
    settings_parser = subparsers.add_parser(
        'settings', help=_('Configure SquashFS automatic saving'), parents=[parent_parser])
    settings_parser.add_argument('session_id', help=_('SquashFS session ID'))
    settings_parser.add_argument('--shutdown', choices=('on', 'off'),
                                 help=_('Save automatically at shutdown'))
    settings_parser.add_argument('--autosave', type=int,
                                 choices=SessionManager.SQUASHFS_AUTOSAVE_INTERVALS,
                                 help=_('Periodic save interval in minutes (0 disables it)'))

    # Systemd periodic-save worker
    autosave_parser = subparsers.add_parser(
        'autosave', help=argparse.SUPPRESS, parents=[parent_parser])

    # Delete command
    delete_parser = subparsers.add_parser('delete', help=_('Delete a session'), parents=[parent_parser])
    delete_parser.add_argument('session_id', help=_('Session ID to delete'))
    delete_parser.add_argument('--handoff', action='store_true',
                               help=_('Move a running RAM-backed SquashFS session to the active snapshot before deletion'))

    # Cleanup command
    cleanup_parser = subparsers.add_parser('cleanup', help=_('Clean up old sessions'), parents=[parent_parser])
    cleanup_parser.add_argument('--days', type=int, default=30, 
                               help=_('Delete sessions older than N days (default: 30)'))

    # Status command
    status_parser = subparsers.add_parser('status', help=_('Check sessions directory status'), parents=[parent_parser])

    # Resize command
    resize_parser = subparsers.add_parser('resize', help=_('Resize a session'), parents=[parent_parser])
    resize_parser.add_argument('session_id', help=_('Session ID to resize'))
    resize_parser.add_argument('size', type=parse_perch_size, metavar='SIZE', help=_('New size in MB, GB, or TB'))

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
    import_parser.add_argument('--force-mode', choices=session_modes,
                               help=_('Force specific session mode'))
    import_parser.add_argument('--no-verify', action='store_true', help=_('Skip integrity verification'))
    import_parser.add_argument('--skip-compatibility-check', action='store_true',
                               help=_('Skip compatibility checks'))

    # Copy command
    copy_parser = subparsers.add_parser('copy', help=_('Copy a session'), parents=[parent_parser])
    copy_parser.add_argument('session_id', help=_('Session ID to copy'))
    copy_parser.add_argument('--to-mode', choices=session_modes,
                            help=_('Convert to different mode (optional)'))
    copy_parser.add_argument('--size', type=parse_perch_size, metavar='SIZE',
                            help=_('Size for a container target, in MB, GB, or TB'))

    # Convert command
    convert_parser = subparsers.add_parser('convert', help=_('Convert session mode'), parents=[parent_parser])
    convert_parser.add_argument('session_id', help=_('Session ID to convert'))
    convert_parser.add_argument('target_mode', choices=session_modes,
                               help=_('Target mode'))
    convert_parser.add_argument('--size', type=parse_perch_size, metavar='SIZE',
                               help=_('Size for a container target, in MB, GB, or TB'))
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
        if args.command == 'autosave':
            sys.exit(0)
        # ``status`` must be able to report an unavailable persistence store.
        # RAM-only boots intentionally have no changes directory when persistence
        # was not requested, so absence is a valid status rather than a CLI error.
        if args.command != 'status':
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
        try:
            password = luks_password_from_args(args, confirm=args.mode == 'luks')
        except ValueError as error:
            success, message = False, str(error)
        else:
            success, message = manager.create_session(
                args.mode, args.size, password=password, policy=args.policy,
                autosave=args.autosave)
        if args.json:
            result = {"success": success, "message": message}
            print(json.dumps(result))
        else:
            print(message)
        sys.exit(0 if success else 1)

    elif args.command == 'settings':
        shutdown = None if args.shutdown is None else args.shutdown == 'on'
        success, message, settings = manager.set_squashfs_save_settings(
            args.session_id, shutdown=shutdown, autosave=args.autosave)
        if args.json:
            result = {"success": success, "message": message}
            if settings is not None:
                result["settings"] = settings
            print(json.dumps(result))
        else:
            print(message)
            if success and settings is not None:
                print(_("Save at shutdown: {}").format(
                    _("Yes") if settings['shutdown'] else _("No")))
                print(_("Periodic save: {} minutes").format(settings['autosave'])
                      if settings['autosave'] else _("Periodic save: Off"))
        sys.exit(0 if success else 1)

    elif args.command == 'autosave':
        performed, success, message, capture = manager.autosave_running_session()
        if args.json:
            result = {"success": success, "performed": performed, "message": message}
            if capture is not None:
                result["capture"] = capture
            print(json.dumps(result))
        elif performed or not success:
            print(message)
        sys.exit(0 if success else 1)

    elif args.command == 'save':
        if args.progress and not args.json:
            print(_("--progress requires --json"), file=sys.stderr)
            sys.exit(1)

        def emit_progress(phase):
            if args.progress:
                print(json.dumps({"type": "phase", "phase": phase}), flush=True)

        success, message, capture = manager.save_session(
            args.session_id, finalize_shutdown=args.shutdown_finalize,
            progress_callback=emit_progress if args.progress else None)
        if args.json:
            result = {"success": success, "message": message}
            if capture is not None:
                result["capture"] = capture
            print(json.dumps(result), flush=True)
        else:
            print(message)
        sys.exit(0 if success else 1)

    elif args.command == 'delete':
        success, message = manager.delete_session(args.session_id, handoff=args.handoff)
        if args.json:
            result = {"success": success, "message": message}
            print(json.dumps(result))
        else:
            print(message)
        sys.exit(0 if success else 1)

    elif args.command == 'cleanup':
        if args.days <= 0:
            message = _("Days threshold must be greater than zero")
            if args.json:
                print(json.dumps({"success": False, "message": message}))
            else:
                print(message, file=sys.stderr)
            sys.exit(1)
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
        success, message = manager.resize_session(args.session_id, args.size,
                                                  password=luks_password_from_args(args))
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
        success, message = manager.export_session(args.session_id, args.output_path, verify=verify,
                                                  password=luks_password_from_args(args))
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
            skip_compatibility_check=args.skip_compatibility_check,
            password=luks_password_from_args(args)
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
            size_mb=args.size,
            password=luks_password_from_args(args)
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
            in_place=in_place,
            password=luks_password_from_args(args)
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
