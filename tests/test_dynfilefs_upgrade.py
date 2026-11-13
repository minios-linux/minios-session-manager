import contextlib
import copy
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
import minios_session as cli


def configured(tmp_path, encryption='none'):
    source = tmp_path / '1'
    source.mkdir()
    (source/'changes.dat').write_bytes(b'original400')
    manager = cli.SessionManager.__new__(cli.SessionManager)
    manager.sessions_dir = str(tmp_path)
    manager._mutation_lock = contextlib.nullcontext
    manager._detached_mount_exists = lambda path: False
    manager.get_running_session = lambda: None
    data = {'default': '1', 'sessions': {'1': {
        'mode': 'dynfilefs', 'version': '6.0', 'edition': 'standard',
        'union': 'overlayfs', 'size': '32', 'encryption': encryption}}}
    manager._read_sessions_metadata = lambda: copy.deepcopy(data)

    def write_metadata(value):
        data.clear()
        data.update(copy.deepcopy(value))
        return True

    manager._write_sessions_metadata = write_metadata

    def reserve(acquire_lease=False):
        dest = tmp_path/'2'
        dest.mkdir()
        return '2', str(dest), contextlib.ExitStack()

    manager._reserve_session = Mock(side_effect=reserve)
    return manager, data


def backend(args, **kwargs):
    if args == ['dynfilefs']:
        return SimpleNamespace(returncode=1, stdout=b'--info --upgrade400', stderr=b'')
    if args[1] == '--info':
        version = 600 if Path(args[-1]).parent.name == 'verified' else 400
        return SimpleNamespace(returncode=0, stderr=b'',
                               stdout=f'version:{version}\nsize:33554432\n'.encode())
    assert args[1] == '--upgrade400'
    dest = Path(args[-1])
    dest.mkdir()
    (dest/'changes.dat').write_bytes(b'DYNFS600')
    (dest/'changes.dat.0').write_bytes(b'verified-payload')
    return SimpleNamespace(returncode=0, stdout=b'', stderr=b'')


@pytest.mark.parametrize('encryption', ['none', 'luks'])
def test_upgrades_into_new_session_without_activation_or_keys(tmp_path, encryption):
    manager, metadata = configured(tmp_path, encryption)
    with patch.object(cli.subprocess, 'run', side_effect=backend) as calls:
        ok, message, details = manager.upgrade_dynfilefs_session('1')
    assert ok, message
    assert details['new_session'] == '2' and details['verified']
    assert not details['activated'] and metadata['default'] == '1'
    assert metadata['sessions']['2'] == metadata['sessions']['1']
    assert (tmp_path/'1/changes.dat').read_bytes() == b'original400'
    assert (tmp_path/'2/changes.dat').read_bytes() == b'DYNFS600'
    assert all(call.args[0][0] == 'dynfilefs' for call in calls.call_args_list)


def test_running_source_never_reserves_destination(tmp_path):
    manager, _ = configured(tmp_path)
    manager.get_running_session = lambda: {'id': '1'}
    ok, _, _ = manager.upgrade_dynfilefs_session('1')
    assert not ok
    manager._reserve_session.assert_not_called()


def test_wrong_format_is_not_reinterpreted(tmp_path):
    manager, _ = configured(tmp_path)
    manager._inspect_dynfilefs = lambda _: {'version': 600, 'size': 33554432}
    ok, _, _ = manager.upgrade_dynfilefs_session('1')
    assert not ok
    manager._reserve_session.assert_not_called()


def test_failed_conversion_keeps_source_and_default(tmp_path):
    manager, metadata = configured(tmp_path)
    def fail(args, **kwargs):
        if len(args) > 1 and args[1] == '--upgrade400':
            return SimpleNamespace(returncode=1, stdout=b'', stderr=b'No space left')
        return backend(args, **kwargs)
    with patch.object(cli.subprocess, 'run', side_effect=fail):
        ok, _, _ = manager.upgrade_dynfilefs_session('1')
    assert not ok and not (tmp_path/'2').exists()
    assert metadata['default'] == '1' and list(metadata['sessions']) == ['1']
    assert (tmp_path/'1/changes.dat').read_bytes() == b'original400'


def test_uncertain_metadata_commit_keeps_verified_storage(tmp_path):
    manager, _ = configured(tmp_path)
    manager._write_sessions_metadata = Mock(side_effect=OSError('fsync uncertain'))
    with patch.object(cli.subprocess, 'run', side_effect=backend):
        ok, _, details = manager.upgrade_dynfilefs_session('1')
    assert not ok and details['registration_uncertain']
    assert (tmp_path/'2/changes.dat').read_bytes() == b'DYNFS600'
    assert (tmp_path/'1/changes.dat').read_bytes() == b'original400'


def test_usage_counts_recovery_companions_not_arbitrary_files(tmp_path):
    manager, _ = configured(tmp_path)
    directory = tmp_path/'1'
    for filename in ('changes.dat.0', 'changes.dat.log', 'changes.dat.work', 'changes.dat.next'):
        (directory/filename).write_bytes(b'X'*8192)
    (directory/'changes.dat.unrelated').write_bytes(b'X'*32768)
    expected = sum(p.stat().st_blocks*512 for p in directory.iterdir()
                   if p.name != 'changes.dat.unrelated')
    assert manager._get_dynfilefs_size(str(directory)) == expected


def test_old_backend_is_probed_without_a_storage_path(tmp_path):
    manager, _ = configured(tmp_path)
    old = SimpleNamespace(returncode=1, stdout=b'dynfilefs 4.6.0', stderr=b'')
    with patch.object(cli.subprocess, 'run', return_value=old) as calls:
        ok, _, _ = manager.upgrade_dynfilefs_session('1')
    assert not ok
    assert len(calls.call_args_list) == 1
    assert calls.call_args.args[0] == ['dynfilefs']
    manager._reserve_session.assert_not_called()
