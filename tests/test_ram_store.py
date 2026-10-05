import contextlib
import json
import os
from types import SimpleNamespace

import pytest

from minios_ram_store import OriginalStore, StoreUnavailable, read_origin, store_path


def test_origin_uses_uuid_and_relative_path_instead_of_a_stale_device(tmp_path):
    path = tmp_path / 'origin'
    path.write_text('mode=trim\nsession=1\nuuid=ABCD-1234\nstore=/dev/mapper/old/minios/changes\n')
    path.chmod(0o600)
    origin = read_origin(str(path), owner=os.getuid())
    assert origin['relative'] == 'minios/changes'
    calls = []
    def runner(arguments, **kwargs):
        calls.append(arguments)
        return SimpleNamespace(returncode=0, stdout='/dev/sdz1\n', stderr='')
    assert OriginalStore(origin, runner=runner).locate_device() == ('/dev/sdz1', False)
    assert calls == [['blkid', '-c', '/dev/null', '-t', 'UUID=ABCD-1234', '-o', 'device']]


@pytest.mark.parametrize('extra', ('uuid=other\n', 'relative=../outside\n', 'relative=/outside\n'))
def test_origin_rejects_ambiguous_or_escaping_fields(tmp_path, extra):
    path = tmp_path / 'origin'
    path.write_text('session=1\nuuid=ABCD-1234\n' + extra)
    path.chmod(0o600)
    with pytest.raises(StoreUnavailable):
        read_origin(str(path), owner=os.getuid())


def test_store_path_does_not_follow_directory_links(tmp_path):
    (tmp_path / 'minios').symlink_to('/tmp')
    with pytest.raises(StoreUnavailable):
        store_path(str(tmp_path), 'minios/changes')


def test_duplicate_uuid_is_not_resolved_by_picking_the_first_device():
    def runner(arguments, **kwargs):
        return SimpleNamespace(returncode=0, stdout='/dev/sda1\n/dev/sdb1\n', stderr='')
    with pytest.raises(StoreUnavailable, match='Several devices'):
        OriginalStore({'uuid': 'ABCD-1234'}, runner=runner).locate_device()


def test_plugin_lookup_uses_outer_uuid_and_checks_inner_filesystem(tmp_path):
    (tmp_path / 'ventoy').mkdir()
    backend = tmp_path / 'ventoy' / 'session.dat'
    backend.write_bytes(b'fixture')
    (tmp_path / 'ventoy' / 'ventoy.json').write_text(json.dumps({
        'persistence': [{'backend': ['/missing.dat', '/ventoy/session.dat']}] }))
    def runner(arguments, **kwargs):
        if 'UUID=outer-uuid' in arguments:
            return SimpleNamespace(returncode=0, stdout='/dev/sdb1\n', stderr='')
        if '-p' in arguments:
            return SimpleNamespace(returncode=0, stdout='inner-uuid\n', stderr='')
        return SimpleNamespace(returncode=2, stdout='', stderr='')
    store = OriginalStore({'uuid': 'inner-uuid', 'media_uuid': 'outer-uuid'}, runner=runner)
    assert store.locate_device() == ('/dev/sdb1', True)
    assert store._plugin_file(str(tmp_path)) == str(backend)


def test_mount_failure_cleans_only_the_private_directory(tmp_path):
    calls = []
    def runner(arguments, **kwargs):
        calls.append(arguments)
        if arguments[0] == 'blkid':
            return SimpleNamespace(returncode=0, stdout='/dev/sdz1\n', stderr='')
        if arguments[0] == 'blockdev':
            return SimpleNamespace(returncode=0, stdout='0\n', stderr='')
        return SimpleNamespace(returncode=1, stdout='', stderr='read-only device')
    work = tmp_path / 'private'
    store = OriginalStore({'uuid': 'ABCD-1234', 'relative': 'minios/changes'},
                          str(work), runner)
    with pytest.raises(StoreUnavailable):
        with store.mounted(writable=True):
            pytest.fail('unavailable store was mounted')
    assert [path.name for path in work.iterdir()] == ['.mount.lock']
    assert not any(call[0] == 'umount' for call in calls)


def test_private_mounts_are_serialized_and_busy_status_does_not_unmount(tmp_path, monkeypatch):
    work = tmp_path / 'private'
    store = OriginalStore({'uuid': 'test'}, str(work))
    @contextlib.contextmanager
    def mounted(writable):
        yield str(tmp_path)
    monkeypatch.setattr(store, '_mounted', mounted)
    with store.mounted():
        with pytest.raises(StoreUnavailable, match='another RAM save operation') as error:
            with store.mounted(writable=True):
                pytest.fail('overlapping private mount was allowed')
        assert error.value.state == 'busy'
    with store.mounted(writable=True) as path:
        assert path == str(tmp_path)


def test_orphan_plugin_mount_is_detached_before_its_outer_store(tmp_path, monkeypatch):
    work = tmp_path / 'private'
    work.mkdir(mode=0o700)
    for name in ('store-old', 'backend-old'):
        (work / name).mkdir()
    calls = []
    def runner(arguments, **kwargs):
        calls.append(arguments)
        return SimpleNamespace(returncode=0, stdout=arguments[-1] + '\n', stderr='')
    store = OriginalStore({'uuid': 'test'}, str(work), runner)
    @contextlib.contextmanager
    def mounted(writable):
        yield str(tmp_path)
    monkeypatch.setattr(store, '_mounted', mounted)
    with store.mounted():
        assert [path.name for path in work.iterdir()] == ['.mount.lock']
    assert [call[1] for call in calls if call[0] == 'umount'] == [
        str(work / 'backend-old'), str(work / 'store-old')]


def test_existing_rw_store_is_borrowed_as_a_private_readonly_bind(tmp_path, monkeypatch):
    calls = []
    def runner(arguments, **kwargs):
        calls.append(arguments)
        document = {'filesystems': [{'target': str(tmp_path), 'fsroot': '/', 'options': 'rw'}]}
        return SimpleNamespace(returncode=0, stdout=json.dumps(document), stderr='')
    real_stat = os.stat
    monkeypatch.setattr(os, 'stat', lambda path, *args, **kwargs:
                        SimpleNamespace(st_rdev=real_stat(tmp_path).st_dev) if path == '/dev/store'
                        else real_stat(path, *args, **kwargs))
    store = OriginalStore({'uuid': 'test'}, runner=runner)
    store.source_readonly = False
    store._mount_source('/dev/store', '/private', False)
    assert calls[1][:2] == ['mount', '--bind']
    assert calls[1][2].startswith('/proc/{}/fd/'.format(os.getpid()))
    assert calls[2] == ['mount', '-o', 'remount,bind,ro,nosuid,nodev,noexec', '/private']
    assert store.source_readonly is False


def test_existing_readonly_store_is_not_remounted_rw(tmp_path, monkeypatch):
    calls = []
    def runner(arguments, **kwargs):
        calls.append(arguments)
        return SimpleNamespace(returncode=0, stdout=json.dumps({'filesystems': [
            {'target': str(tmp_path), 'fsroot': '/', 'options': 'ro'}]}), stderr='')
    real_stat = os.stat
    monkeypatch.setattr(os, 'stat', lambda path, *args, **kwargs:
                        SimpleNamespace(st_rdev=real_stat(tmp_path).st_dev) if path == '/dev/store'
                        else real_stat(path, *args, **kwargs))
    monkeypatch.setattr(os, 'fstatvfs', lambda descriptor: SimpleNamespace(f_flag=os.ST_RDONLY))
    store = OriginalStore({'uuid': 'test'}, runner=runner)
    store.source_readonly = False
    with pytest.raises(StoreUnavailable, match='mounted read-only') as error:
        store._mount_source('/dev/store', '/private', True)
    assert error.value.state == 'readonly'
    assert len(calls) == 1
