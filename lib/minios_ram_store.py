"""Resolve the original store of a RAM session without relying on /dev names."""

import contextlib
import ctypes
import fcntl
import hashlib
import importlib.util
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone


class StoreUnavailable(OSError):
    def __init__(self, message, state='unavailable'):
        super().__init__(message)
        self.state = state


class CancelMarker:
    def __init__(self, path):
        self.descriptor = None
        self.path = path
        self.owner_pid = os.getppid()
        self.owner_start = process_start(self.owner_pid)
        if path is None:
            return
        path = os.path.abspath(path)
        self.path = path
        parent, name = os.path.split(path)
        if name != 'cancel' or os.path.realpath(parent) != parent:
            raise StoreUnavailable('Invalid save cancellation path')
        descriptor = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            info = os.fstat(descriptor)
            caller = int(os.environ.get('PKEXEC_UID', str(os.getuid())))
            if info.st_uid != caller or info.st_mode & 0o077:
                raise StoreUnavailable('Cancellation directory must be private and owned by the caller')
            libc = ctypes.CDLL(None, use_errno=True)
            buffer = ctypes.create_string_buffer(512)
            if libc.fstatfs(descriptor, ctypes.byref(buffer)) != 0:
                raise StoreUnavailable('Cannot inspect the cancellation filesystem')
            if ctypes.c_long.from_buffer(buffer).value != 0x01021994:
                raise StoreUnavailable('Cancellation directory must be on tmpfs')
            self.descriptor = descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def cancelled(self):
        if self.owner_start is not None and process_start(self.owner_pid) != self.owner_start:
            return True
        if self.descriptor is None:
            return False
        if os.fstat(self.descriptor).st_nlink == 0:
            return True
        try:
            os.stat('cancel', dir_fd=self.descriptor, follow_symlinks=False)
            return True
        except FileNotFoundError:
            return False

    def close(self):
        if self.descriptor is not None:
            os.close(self.descriptor)
            self.descriptor = None


def read_state(path, owner=0):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        current = os.fstat(descriptor)
        if (not stat.S_ISREG(current.st_mode) or current.st_uid != owner or
                current.st_nlink != 1 or current.st_mode & 0o022 or current.st_size > 16384):
            raise StoreUnavailable('RAM session origin is not trusted')
        with os.fdopen(descriptor, 'r', encoding='utf-8') as stream:
            descriptor = None
            result = {}
            for line in stream:
                key, separator, value = line.rstrip('\n').partition('=')
                if (not separator or not re.fullmatch(r'[a-z_]+', key) or
                        key in result or any(ord(c) < 32 for c in value)):
                    raise StoreUnavailable('RAM session origin is malformed')
                result[key] = value
        return result
    finally:
        if descriptor is not None:
            os.close(descriptor)


def read_origin(path, owner=0):
    result = read_state(path, owner)
    if not re.fullmatch(r'[A-Za-z0-9-]{1,80}', result.get('uuid', '')):
        raise StoreUnavailable('RAM session has no original filesystem UUID')
    if not re.fullmatch(r'[0-9]+', result.get('session', '')):
        raise StoreUnavailable('RAM session origin has no session number')
    relative = result.get('relative')
    if relative is None:
        # Older boot records stored a device-qualified session directory.
        path = result.get('store', '')
        if path.startswith('/dev/mapper/'):
            relative = '/'.join(path.split('/')[4:])
        elif path.startswith('/dev/disk/by-label/'):
            relative = '/'.join(path.split('/')[5:])
        elif path.startswith('/dev/'):
            relative = '/'.join(path.split('/')[3:])
    result['relative'] = relative_path(relative or 'minios/changes')
    return result


def load_copy_backend():
    path = '/usr/lib/minios-tools/minios_ram_save.py'
    current = os.lstat(path)
    if (not stat.S_ISREG(current.st_mode) or current.st_uid != 0 or
            current.st_mode & 0o022):
        raise StoreUnavailable('RAM save backend is not trusted')
    spec = importlib.util.spec_from_file_location('minios_ram_save', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def relative_path(path):
    if not isinstance(path, str) or not path or path.startswith('/'):
        raise StoreUnavailable('Invalid store-relative path')
    if any(part in ('', '.', '..') for part in path.split('/')) or any(
            ord(character) < 32 for character in path):
        raise StoreUnavailable('Invalid store-relative path')
    return path


def store_path(root, relative):
    path = root
    for component in relative_path(relative).split('/'):
        path = os.path.join(path, component)
        current = os.lstat(path)
        if not stat.S_ISDIR(current.st_mode):
            raise StoreUnavailable('Store path contains a link or a non-directory')
    return path


class OriginalStore:
    def __init__(self, origin, work_parent='/run/minios-persistence/ram-save', runner=subprocess.run):
        self.origin = origin
        self.work_parent = work_parent
        self.runner = runner

    def command(self, *arguments, check=True):
        result = self.runner(list(arguments), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             universal_newlines=True, timeout=15)
        if check and result.returncode:
            raise StoreUnavailable(result.stderr.strip() or 'Original storage is unavailable')
        return result

    def devices(self, uuid):
        if not re.fullmatch(r'[A-Za-z0-9-]{1,80}', uuid or ''):
            return []
        result = self.command('blkid', '-c', '/dev/null', '-t', 'UUID=' + uuid,
                              '-o', 'device', check=False)
        if result.returncode not in (0, 2):
            raise StoreUnavailable('Cannot inspect original storage devices')
        devices = set()
        for path in result.stdout.splitlines():
            if path.startswith('/dev/') and not any(c.isspace() for c in path):
                devices.add(os.path.realpath(path))
        return sorted(devices)

    def locate_device(self):
        devices = self.devices(self.origin['uuid'])
        if len(devices) > 1:
            raise StoreUnavailable('Several devices have the original store UUID', 'ambiguous')
        if devices:
            return devices[0], False
        media = self.devices(self.origin.get('media_uuid'))
        if len(media) > 1:
            raise StoreUnavailable('Several devices have the original media UUID', 'ambiguous')
        if media:
            return media[0], True
        raise StoreUnavailable('The original storage device is not connected', 'missing')

    def status(self):
        try:
            device, _ = self.locate_device()
            read_only = self.command('blockdev', '--getro', device).stdout.strip() == '1'
            with self.mounted() as path:
                space = os.statvfs(path)
                available = space.f_bavail * space.f_frsize
                read_only = read_only or self.source_readonly
            return {'state': 'readonly' if read_only else 'available',
                    'device': device, 'writable': not read_only,
                    'free_bytes': available, 'message': ''}
        except (OSError, ValueError) as error:
            return {'state': getattr(error, 'state', 'unavailable'),
                    'writable': False, 'message': str(error)}

    def _plugin_file(self, root):
        config = os.path.join(store_path(root, 'ventoy'), 'ventoy.json')
        if os.path.getsize(config) > 1024 * 1024 or os.path.islink(config):
            raise StoreUnavailable('Invalid Ventoy persistence configuration')
        with open(config, 'r', encoding='utf-8') as stream:
            configuration = json.load(stream)
        if not isinstance(configuration, dict):
            raise StoreUnavailable('Invalid Ventoy persistence configuration')
        matches = {}
        entries = configuration.get('persistence', [])
        if not isinstance(entries, list):
            raise StoreUnavailable('Invalid Ventoy persistence configuration')
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            backends = entry.get('backend', [])
            if isinstance(backends, str):
                backends = [backends]
            if not isinstance(backends, list):
                continue
            for name in backends:
                if not isinstance(name, str):
                    continue
                try:
                    relative = relative_path(name.lstrip('/'))
                    parent, basename = os.path.split(relative)
                    directory = store_path(root, parent) if parent else root
                    path = os.path.join(directory, basename)
                    info = os.lstat(path)
                except (FileNotFoundError, NotADirectoryError, StoreUnavailable):
                    continue
                if not stat.S_ISREG(info.st_mode):
                    continue
                probe = self.command('blkid', '-p', '-s', 'UUID', '-o', 'value',
                                     path, check=False)
                if probe.returncode == 0 and probe.stdout.strip() == self.origin['uuid']:
                    matches[(info.st_dev, info.st_ino)] = path
        if len(matches) != 1:
            raise StoreUnavailable('The original Ventoy store is missing or ambiguous')
        return next(iter(matches.values()))

    def _options(self, source, writable, file_backend=False):
        probe = ['blkid'] + (['-p'] if file_backend else [])
        filesystem = self.command(*(probe + ['-s', 'TYPE', '-o', 'value', source])).stdout.strip()
        options = ('rw' if writable else 'ro') + ',nosuid,nodev,noexec'
        if not writable:
            options += {'ext3': ',noload', 'ext4': ',noload',
                        'xfs': ',norecovery', 'btrfs': ',nologreplay',
                        'f2fs': ',norecovery'}.get(filesystem, '')
        return ('loop,' if file_backend else '') + options

    def _mount_source(self, source, destination, writable, file_backend=False):
        if not file_backend:
            existing = self.command('findmnt', '-J', '-S', source,
                                    '-o', 'TARGET,OPTIONS,FSROOT', check=False)
            if existing.returncode not in (0, 1):
                raise StoreUnavailable('Cannot inspect existing original storage mounts')
            if existing.returncode == 0:
                entries = json.loads(existing.stdout).get('filesystems', [])
                for entry in entries:
                    if entry.get('fsroot') != '/':
                        continue
                    target = entry.get('target')
                    if not isinstance(target, str) or not os.path.isabs(target):
                        raise StoreUnavailable('Invalid original storage mount path')
                    descriptor = os.open(target, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                    try:
                        if os.fstat(descriptor).st_dev != os.stat(source).st_rdev:
                            continue
                        read_only = bool(os.fstatvfs(descriptor).f_flag & os.ST_RDONLY)
                        if writable and read_only:
                            raise StoreUnavailable('Original storage is mounted read-only', 'readonly')
                        self.source_readonly = self.source_readonly or read_only
                        # Bind the open filesystem root, even if its public path is renamed.
                        path = '/proc/{}/fd/{}'.format(os.getpid(), descriptor)
                        self.command('mount', '--bind', path, destination)
                        try:
                            self.command('mount', '-o', 'remount,bind,' +
                                         ('rw' if writable else 'ro') + ',nosuid,nodev,noexec', destination)
                        except BaseException:
                            self.command('umount', destination)
                            raise
                        return
                    finally:
                        os.close(descriptor)
        self.command('mount', '-o', self._options(source, writable, file_backend), source, destination)

    @contextlib.contextmanager
    def mounted(self, writable=False):
        os.makedirs(self.work_parent, mode=0o700, exist_ok=True)
        parent = os.lstat(self.work_parent)
        if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.geteuid() or
                parent.st_mode & 0o077):
            raise StoreUnavailable('Private store mount directory is not trusted')
        descriptor = os.open(os.path.join(self.work_parent, '.mount.lock'),
                             os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            info = os.fstat(descriptor)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or
                    info.st_nlink != 1 or info.st_mode & 0o077):
                raise StoreUnavailable('Private store mount lock is not trusted')
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise StoreUnavailable('Original storage is in use by another RAM save operation', 'busy')
            # The thaw guard retains this lock until a killed writer's ext4 is thawed.
            for name in sorted(os.listdir(self.work_parent)):
                if not re.fullmatch(r'(backend|store)-[A-Za-z0-9_-]+', name):
                    continue
                path = os.path.join(self.work_parent, name)
                current = os.lstat(path)
                if not stat.S_ISDIR(current.st_mode):
                    raise StoreUnavailable('Private store mount path is not a directory')
                mounted = self.command('findmnt', '-n', '--mountpoint', path, '-o', 'TARGET', check=False)
                if mounted.returncode == 0:
                    self.command('umount', path)
                elif mounted.returncode != 1:
                    raise StoreUnavailable('Cannot inspect a previous private store mount')
                os.rmdir(path)
            with self._mounted(writable) as store:
                yield store
        finally:
            os.close(descriptor)

    @contextlib.contextmanager
    def _mounted(self, writable):
        self.source_readonly = False
        device, plugin = self.locate_device()
        if writable and self.command('blockdev', '--getro', device).stdout.strip() == '1':
            raise StoreUnavailable('Original storage is read-only', 'readonly')
        root = tempfile.mkdtemp(prefix='store-', dir=self.work_parent)
        mounts = []
        try:
            self._mount_source(device, root, writable)
            mounts.append(root)
            source = root
            if plugin:
                backing = self._plugin_file(root)
                source = tempfile.mkdtemp(prefix='backend-', dir=self.work_parent)
                try:
                    self._mount_source(backing, source, writable, True)
                except BaseException:
                    os.rmdir(source)
                    raise
                mounts.append(source)
            yield store_path(source, self.origin['relative'])
        finally:
            # Do not remove directories or report a clean detach after a busy unmount.
            for path in reversed(mounts):
                self.command('umount', path)
                os.rmdir(path)
            if not mounts:
                os.rmdir(root)


def ram_save_status(boot_state_file):
    try:
        directory = os.path.dirname(boot_state_file)
        origin = read_origin(os.path.join(directory, 'ram-origin'))
        boot = read_state(boot_state_file)
        status = OriginalStore(origin).status()
        supported = boot.get('mode') in RamSessionSave.BACKENDS
        if supported:
            load_copy_backend()
        status.update({'supported': supported, 'session': boot.get('session'),
                       'uuid': origin['uuid'], 'relative': origin['relative']})
        status['save_available'] = supported and status['writable'] and boot.get('boot_level') == 'ok'
        saved_path = os.path.join(directory, 'ram-save-state.json')
        if os.path.isfile(saved_path):
            saved = read_private_json(saved_path)
            if saved.get('boot_id') == boot.get('boot_id'):
                status['saved'] = saved.get('saved')
                status['target_session'] = saved.get('target')
        operation_path = os.path.join(directory, 'ram-save-operation.json')
        if os.path.isfile(operation_path):
            operation = read_private_json(operation_path)
            if (operation.get('boot_id') == boot.get('boot_id') and
                    operation.get('session') == boot.get('session')):
                if operation.get('status') == 'saving':
                    started = process_start(operation.get('pid'))
                    if started is not None and started == operation.get('process_start'):
                        status['saving'] = True
                        status['save_available'] = False
                        status['operation'] = operation
                    else:
                        status['error'] = 'Session saving was interrupted'
                elif operation.get('status') == 'failed':
                    status['error'] = operation.get('message', 'Session saving failed')
        return status
    except Exception as error:
        return {'state': 'unavailable', 'supported': False, 'writable': False,
                'save_available': False, 'message': str(error)}


def process_start(pid):
    if type(pid) is not int or pid < 1:
        return None
    try:
        with open('/proc/{}/stat'.format(pid), 'r', encoding='ascii') as stream:
            fields = stream.read().rsplit(')', 1)[1].split()
        return fields[19] if fields[0] != 'Z' else None
    except (OSError, IndexError):
        return None


def write_private_json(directory, name, value):
    fd, temporary = tempfile.mkstemp(prefix='.' + name + '-', dir=directory)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, os.path.join(directory, name))
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def file_manifest(directory):
    result = {}
    for root, directories, files in os.walk(directory, followlinks=False):
        for name in directories:
            if not stat.S_ISDIR(os.lstat(os.path.join(root, name)).st_mode):
                raise StoreUnavailable('Session contains a linked directory')
        for name in files:
            path = os.path.join(root, name)
            relative = os.path.relpath(path, directory)
            relative_path(relative)
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            digest = hashlib.sha256()
            try:
                info = os.fstat(descriptor)
                if not stat.S_ISREG(info.st_mode):
                    raise StoreUnavailable('Session contains a non-regular file')
                while True:
                    data = os.read(descriptor, 1024 * 1024)
                    if not data:
                        break
                    digest.update(data)
                result[relative] = digest.hexdigest()
            finally:
                os.close(descriptor)
    return result


def read_private_json(path, private=True):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, 'r', encoding='utf-8') as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or
                (private and (info.st_uid != 0 or info.st_mode & 0o022)) or
                info.st_nlink != 1 or info.st_size > 16 * 1024 * 1024):
            raise StoreUnavailable('RAM save state is not trusted')
        return json.load(stream)


def read_boot_manifest(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    result = {}
    with os.fdopen(descriptor, 'r', encoding='utf-8') as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or
                info.st_mode & 0o022 or info.st_nlink != 1 or info.st_size > 16 * 1024 * 1024):
            raise StoreUnavailable('Original session inventory is not trusted')
        for line in stream:
            match = re.fullmatch(r'([0-9a-f]{64})  \./([^\r\n]+)\n?', line)
            if not match:
                raise StoreUnavailable('Original session inventory is malformed')
            name = relative_path(match.group(2))
            if name in result:
                raise StoreUnavailable('Original session inventory contains duplicate paths')
            result[name] = match.group(1)
    return result


class RamSessionSave:
    CONTAINERS = ('raw', 'dynfilefs', 'dynblk', 'vmdk')
    BACKENDS = CONTAINERS + ('squashfs',)
    UPPER_PATH = '/run/initramfs/memory/changes'

    def __init__(self, manager, tools=None, resolver=None):
        self.manager = manager
        self.rundir = os.path.dirname(manager.BOOT_STATE_FILE)
        self.origin = read_origin(os.path.join(self.rundir, 'ram-origin'))
        self.tools = tools or load_copy_backend()
        self.resolver = resolver or OriginalStore(self.origin)

    def _runtime(self, session):
        if self.manager.custom_sessions_dir is not None:
            raise StoreUnavailable('RAM saving requires the running session store')
        state = read_state(self.manager.BOOT_STATE_FILE)
        with open(self.manager.BOOT_ID_FILE, 'r', encoding='ascii') as stream:
            boot_id = stream.read().strip()
        if any(state.get(key) != value for key, value in {
                'boot_id': boot_id, 'boot_level': 'ok', 'session': session,
                'durable': '0', 'writable': '1'}.items()):
            raise StoreUnavailable('The requested RAM session is not active in this boot')
        info = os.stat(self.manager.sessions_dir, follow_symlinks=False)
        if (state.get('sessions_device') != str(info.st_dev) or
                state.get('sessions_inode') != str(info.st_ino)):
            raise StoreUnavailable('RAM session storage changed after activation')
        if state.get('mode') not in self.BACKENDS or state.get('active_generation') != 'current':
            raise StoreUnavailable('The active RAM session cannot be saved')
        if state['mode'] in ('dynblk', 'vmdk'):
            backend = self.manager._dynblk_status(state.get('dynblk_device', 'none'))
            if (backend.get('storage_format') != state['mode'] or backend.get('fenced') is not False or
                    backend.get('read_only') is not False or
                    backend.get('cache') not in ('writeback', 'writethrough', 'none', 'directsync')):
                raise StoreUnavailable('The running DynBlk attachment cannot provide an ordered snapshot')
        return state

    def _baseline(self, state, as_new=False):
        if as_new:
            return {'boot_id': state['boot_id'], 'session': state['session'],
                    'target': None, 'manifest': {}, 'metadata': {}}
        path = os.path.join(self.rundir, 'ram-save-state.json')
        if os.path.exists(path):
            baseline = read_private_json(path)
            if baseline.get('boot_id') != state['boot_id'] or baseline.get('session') != state['session']:
                raise StoreUnavailable('RAM save state belongs to another session')
            return baseline
        is_new = (self.origin.get('new_session') == 'true' or
                  self.origin.get('backend') == 'native' or
                  self.origin['session'] != state['session'])
        baseline = {'boot_id': state['boot_id'], 'session': state['session'],
                    'target': None, 'manifest': {}, 'metadata': {}}
        if not is_new:
            origin_manager = type(self.manager)(custom_sessions_dir=self.rundir)
            origin_manager.sessions_file = os.path.join(self.rundir, 'origin-session.conf')
            origin_manager.session_format = 'conf'
            metadata = origin_manager._normalize_metadata(origin_manager._read_sessions_metadata())
            baseline.update({
                'target': self.origin['session'],
                'manifest': read_boot_manifest(os.path.join(self.rundir, 'origin-files.sha256')),
                'metadata': metadata['sessions'].get(self.origin['session'], {}),
            })
        return baseline

    def _remember(self, baseline):
        write_private_json(self.rundir, 'ram-save-state.json', baseline)

    def _check_target(self, target, metadata, baseline):
        session = baseline['target']
        if session is None:
            return
        path = target._session_path(session, require_exists=True)
        if (metadata['sessions'].get(session) != baseline['metadata'] or
                file_manifest(path) != baseline['manifest']):
            raise StoreUnavailable('The original session changed; save the RAM session as a new session', 'conflict')

    def _copy_files(self, source, candidate, notify, marker, skip=()):
        inventory = []
        for root, directories, files in os.walk(source, followlinks=False):
            for name in directories:
                if not stat.S_ISDIR(os.lstat(os.path.join(root, name)).st_mode):
                    raise StoreUnavailable('Session contains a linked directory')
            relative = os.path.relpath(root, source)
            folder = candidate if relative == '.' else os.path.join(candidate, relative)
            os.makedirs(folder, mode=0o700, exist_ok=True)
            for name in files:
                if relative == '.' and (name == '.ram-save-owner' or name in skip):
                    continue
                path = os.path.join(root, name)
                info = os.lstat(path)
                if not stat.S_ISREG(info.st_mode):
                    raise StoreUnavailable('Session contains a non-regular file')
                inventory.append((path, os.path.relpath(path, source), info.st_size))
        total = sum(item[2] for item in inventory)
        space = os.statvfs(candidate)
        if space.f_bavail * space.f_frsize < total + 64 * 1024 * 1024:
            raise StoreUnavailable('Insufficient space to retain the previous session during saving')
        results = {}
        done = 0
        last = [0.0]
        def copied(count, size):
            now = time.monotonic()
            if count == size or now - last[0] >= .25:
                notify({'type': 'progress', 'copied_bytes': done + count, 'total_bytes': total})
                last[0] = now
        notify('copy')
        for path, relative, size in inventory:
            results[relative] = self.tools.copy_container_file(
                path, os.path.join(candidate, relative), copied, marker.cancelled)
            done += size
        return results, total

    def _capture_squashfs(self, source, candidate, fields, baseline, state, notify, marker):
        results, total = self._copy_files(source, candidate, notify, marker, skip=('changes.sb',))
        output = os.path.join(candidate, 'changes.sb')
        generation = max(int(fields.get('generation', '0')),
                         int(baseline['metadata'].get('generation', '0'))) + 1
        def capture_phase(phase):
            if phase not in ('publish', 'complete'):
                notify(phase)
        try:
            capture = self.manager._run_savechanges(output, self.rundir, capture_phase,
                                                     cancel_file=marker.path)
        except OSError as error:
            space_failure = any(text in str(error) for text in (
                'insufficient private temporary space', 'insufficient private temporary inodes',
                'insufficient private module space after staging'))
            filesystem = self.resolver.command('findmnt', '-n', '-T', candidate, '-o', 'FSTYPE').stdout.strip()
            if (not space_failure or os.path.lexists(output) or
                    filesystem not in self.manager.EXACT_STAGING_FILESYSTEMS):
                raise
            capture = self.manager._run_savechanges(output, candidate, capture_phase,
                                                     cancel_file=marker.path)
        descriptor = os.open(candidate, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            self.manager._validate_squashfs_capture(descriptor, 'changes.sb', capture)
        finally:
            os.close(descriptor)
        for key in list(fields):
            if key.startswith('old_'):
                fields.pop(key)
        fields.update({'generation': str(generation), 'digest': capture['sha256'],
                       'compressed': str(capture['compressed_size']),
                       'uncompressed': str(capture['uncompressed_size']),
                       'entries': str(capture['entry_count']),
                       'footprint': json.dumps(capture['extraction_footprint'], sort_keys=True,
                                               separators=(',', ':')),
                       'union': capture['union_backend'], 'capture_boot_id': state['boot_id']})
        results['changes.sb'] = {'sha256': capture['sha256'], 'size': capture['compressed_size']}
        return results, total + capture['compressed_size']

    def save(self, session, progress=None, cancel_path=None, as_new=False, finalize_shutdown=False):
        state = self._runtime(session)
        marker = CancelMarker(cancel_path)
        operation = {'boot_id': state['boot_id'], 'session': session, 'pid': os.getpid(),
                     'process_start': process_start(os.getpid()), 'status': 'saving',
                     'phase': 'prepare'}
        def notify(event):
            if isinstance(event, dict):
                operation.update({key: event[key] for key in ('copied_bytes', 'total_bytes') if key in event})
            else:
                operation['phase'] = event
            write_private_json(self.rundir, 'ram-save-operation.json', operation)
            if progress:
                progress(event)
            elif event == 'freeze':
                print('Session writes are paused while the container is copied.', file=sys.stderr, flush=True)
        try:
            result = self._save(session, notify, marker, as_new, finalize_shutdown)
            operation['status'] = 'done'
            write_private_json(self.rundir, 'ram-save-operation.json', operation)
            return result
        except BaseException as error:
            if (finalize_shutdown and isinstance(error, StoreUnavailable) and
                    error.state in ('missing', 'readonly', 'ambiguous')):
                operation.update({'status': 'skipped', 'message': str(error)})
                write_private_json(self.rundir, 'ram-save-operation.json', operation)
                return {'session_id': session, 'skipped': True, 'reason': str(error)}
            cancelled = isinstance(error, (self.tools.SaveCancelled, KeyboardInterrupt))
            operation.update({'status': 'cancelled' if cancelled else 'failed',
                              'message': str(error) or 'Session saving was interrupted'})
            write_private_json(self.rundir, 'ram-save-operation.json', operation)
            raise
        finally:
            marker.close()

    def _save(self, session, progress, marker, as_new, finalize_shutdown):
        state = self._runtime(session)
        baseline = self._baseline(state, as_new)
        source = self.manager._session_path(session, require_exists=True)
        source_metadata = self.manager._normalize_metadata(self.manager._read_sessions_metadata())
        fields = dict(source_metadata['sessions'][session])
        if (fields.get('mode') != state['mode'] or
                fields.get('encryption', 'none') != state.get('encryption', 'none')):
            raise StoreUnavailable('Running session metadata does not match its backend')
        if finalize_shutdown and (state['mode'] != 'squashfs' or fields.get('policy', 'manual') != 'shutdown'):
            raise StoreUnavailable('This RAM session is not configured for shutdown saving', 'unsupported')
        notify = progress or (lambda event: None)
        notify('prepare')
        with self.resolver.mounted(writable=True) as store:
            target = type(self.manager)(custom_sessions_dir=store)
            with target._mutation_lock(), contextlib.ExitStack() as leases:
                for name in os.listdir(store):
                    if re.fullmatch(r'\.ram-save-[A-Za-z0-9_-]+', name):
                        journal = os.path.join(store, name)
                        if (os.path.isfile(os.path.join(journal, 'ready')) and
                                os.path.isfile(os.path.join(journal, 'committed'))):
                            result_path = os.path.join(journal, 'result.json')
                            if os.path.isfile(result_path):
                                recovered = read_private_json(result_path, private=False)
                                if (recovered.get('boot_id') == state['boot_id'] and
                                        recovered.get('session') == session):
                                    current = target._normalize_metadata(target._read_sessions_metadata())
                                    self._check_target(target, current, recovered)
                                    self._remember(recovered)
                                    if not as_new:
                                        baseline = recovered
                        self.tools.recover_container_transaction(store, journal)
                metadata = target._normalize_metadata(target._read_sessions_metadata())
                if not as_new:
                    self._check_target(target, metadata, baseline)
                destination = (str(target._get_next_session_id()) if as_new or baseline['target'] is None
                               else baseline['target'])
                if baseline['target'] is not None and not as_new:
                    leases.enter_context(target._session_lease(destination))
                transaction = self.tools.ContainerTransaction(store, destination)
                try:
                    candidate = transaction.prepare()
                    if state['mode'] == 'squashfs':
                        results, total = self._capture_squashfs(
                            source, candidate, fields, baseline, state, notify, marker)
                    else:
                        upper = self.UPPER_PATH
                        descriptor = os.open(upper, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                        try:
                            device = os.fstat(descriptor).st_dev
                            identity = '{}:{}'.format(os.major(device), os.minor(device))
                            # DynFileFS stacks ext4 over its FUSE mount at the same path.
                            mounts = self.resolver.command('findmnt', '-n', '--mountpoint', upper,
                                                           '-o', 'FSTYPE,OPTIONS,MAJ:MIN').stdout.splitlines()
                            mounted = [line.split() for line in mounts
                                       if len(line.split()) == 3 and line.split()[2] == identity]
                            if (len(mounted) != 1 or mounted[0][0] != 'ext4' or
                                    'rw' not in mounted[0][1].split(',')):
                                raise StoreUnavailable('The active container is not a writable ext4 filesystem')
                            notify('freeze')
                            with self.tools.FrozenFilesystem(descriptor, cancelled=marker.cancelled):
                                results, total = self._copy_files(source, candidate, notify, marker)
                        finally:
                            os.close(descriptor)
                    notify('verify')
                    for name, result in results.items():
                        self.tools.verify_container_file(os.path.join(candidate, name), result)
                    for root, _, _ in os.walk(candidate, topdown=False):
                        self.tools.sync_directory(root)
                    if marker.cancelled():
                        raise self.tools.SaveCancelled('Session saving was cancelled')
                    if not as_new:
                        self._check_target(target, metadata, baseline)
                    saved = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
                    fields.update({'state': 'clean', 'saved': saved})
                    metadata['sessions'][destination] = fields
                    if not metadata.get('default'):
                        metadata['default'] = destination
                    if metadata.get('running') == destination:
                        metadata.pop('running', None)
                    notify('publish')
                    manifest = {name: result['sha256'] for name, result in results.items()}
                    manifest['.ram-save-owner'] = hashlib.sha256(
                        (transaction.token + '\n').encode('ascii')).hexdigest()
                    baseline.update({'target': destination, 'manifest': manifest,
                                     'metadata': fields, 'saved': saved})
                    self.tools.write_record(os.path.join(transaction.path, 'result.json'),
                                            json.dumps(baseline) + '\n')
                    if marker.cancelled():
                        raise self.tools.SaveCancelled('Session saving was cancelled')
                    transaction.publish(lambda: target._write_sessions_metadata(metadata))
                    try:
                        self._remember(baseline)
                    except BaseException as error:
                        transaction.publication_uncertain = True
                        raise StoreUnavailable('Session was saved, but its RAM save state could not be updated: {}'.format(error))
                    notify('complete')
                    return {'session_id': session, 'target_session_id': destination,
                            'saved': saved, 'bytes': total}
                finally:
                    transaction.cleanup()
