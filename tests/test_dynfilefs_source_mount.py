import subprocess
from unittest.mock import Mock, patch

import pytest

import minios_session as cli


HELP = subprocess.CompletedProcess(
    [], 1, stdout=b'--info --no-auto-snapshots', stderr=b'')


def info(output, code=0, stderr=b''):
    return subprocess.CompletedProcess([], code, stdout=output, stderr=stderr)


@pytest.fixture
def session(tmp_path):
    source = tmp_path / '1'
    source.mkdir()
    (source / 'changes.dat').write_bytes(b'original metadata')
    (source / 'changes.dat.0').write_bytes(b'original data')
    return source


def make_manager():
    return cli.SessionManager.__new__(cli.SessionManager)


@pytest.fixture(autouse=True)
def installed_dynfilefs():
    with patch.object(cli, 'require_dynfilefs_executable', return_value='dynfilefs'):
        yield


@pytest.mark.parametrize('version', (400, 500))
@pytest.mark.parametrize('split', (2, 3))
@pytest.mark.parametrize('encryption', ('none', 'luks'))
def test_read_only_never_mounts_legacy(session, version, split, encryption):
    manager = make_manager()
    manager._make_session_mount_point = Mock()
    source = session / 'changes.dat'
    output = 'version:{}\nsplitMiB:{}\n'.format(version, split).encode()
    with patch.object(cli.subprocess, 'Popen') as spawn, \
            patch.object(cli.subprocess, 'run', side_effect=[HELP, info(output)]) as run:
        with pytest.raises(cli.DynFileFSSourceMountError, match='even-MiB'):
            with manager._mount_session_read(str(session), 'dynfilefs', encryption=encryption):
                pytest.fail('A legacy format was mounted')

    assert [entry[0][0] for entry in run.call_args_list] == [
        ['dynfilefs'], ['dynfilefs', '--info', '-f', str(source)]]
    spawn.assert_not_called()
    manager._make_session_mount_point.assert_not_called()
    assert source.read_bytes() == b'original metadata'
    assert (session / 'changes.dat.0').read_bytes() == b'original data'


@pytest.mark.parametrize('output', (
    b'not a report', b'version:700\n', b'version:600\nversion:400\n',
))
def test_read_only_fails_closed_when_format_is_unclear(session, output):
    manager = make_manager()
    with patch.object(cli.subprocess, 'run', side_effect=[HELP, info(output)]), \
            patch.object(cli.subprocess, 'Popen') as spawn:
        with pytest.raises(cli.DynFileFSSourceMountError, match='Cannot verify'):
            with manager._mount_session_read(str(session), 'dynfilefs'):
                pytest.fail('An unknown format was mounted')
    spawn.assert_not_called()


def test_old_backend_is_probed_without_a_storage_path(session):
    manager = make_manager()
    old = subprocess.CompletedProcess([], 1, stdout=b'dynfilefs 4.6.0', stderr=b'')
    with patch.object(cli.subprocess, 'run', return_value=old) as run, \
            patch.object(cli.subprocess, 'Popen') as spawn:
        with pytest.raises(cli.DynFileFSSourceMountError, match='current DynFileFS'):
            with manager._mount_session_read(str(session), 'dynfilefs'):
                pytest.fail('An outdated helper was used to mount the source')
    assert [entry[0][0] for entry in run.call_args_list] == [['dynfilefs']]
    spawn.assert_not_called()


def test_logical_copy_propagates_legacy_error_without_opening_target(session, tmp_path):
    manager = make_manager()
    manager._mount_session_write = Mock()
    with patch.object(cli.subprocess, 'run', side_effect=[HELP, info(b'version:500\n')]), \
            patch.object(cli.subprocess, 'Popen') as spawn:
        with pytest.raises(cli.DynFileFSSourceMountError, match='legacy DynFileFS'):
            manager._copy_session_with_conversion(
                str(session), str(tmp_path / '2'), 'dynfilefs', 'native')
    spawn.assert_not_called()
    manager._mount_session_write.assert_not_called()


@pytest.mark.parametrize('writable', (False, True))
def test_existing_mount_uses_plain_mount_for_writes_and_disables_auto_snapshots_for_reads(
        session, tmp_path, writable):
    manager = make_manager()
    backend = str(tmp_path / 'backend')
    filesystem = str(tmp_path / 'filesystem')
    source = str(session / 'changes.dat')
    manager._make_session_mount_point = Mock(side_effect=[backend, filesystem])
    manager._wait_for_mount = Mock(return_value=True)
    manager._safe_unmount = Mock(return_value=True)
    manager._safe_fusermount = Mock(return_value=True)
    manager._cleanup_process = Mock()
    manager._safe_rmtree = Mock()
    # Ignore extra known or future fields in the current helper's report.
    report = info(b'version:600\ncurrentParent:000000000000abcd\nautomaticSnapshots:true\n')

    with patch.object(cli.subprocess, 'Popen') as spawn, \
            patch.object(cli.subprocess, 'run', side_effect=[info(b'')] if writable else
                         [HELP, report, info(b'')]) as run:
        with manager._mount_session_read(str(session), 'dynfilefs', writable=writable) as view:
            assert view == filesystem

    mount_command = ['dynfilefs', '-f', source, '-m', backend]
    if not writable:
        mount_command += ['--no-auto-snapshots']
    assert spawn.call_args[0][0] == mount_command
    commands = [entry[0][0] for entry in run.call_args_list]
    assert len(commands) == (1 if writable else 3)
    assert commands[-1] == [
        'mount', '-o', 'loop' if writable else 'loop,ro,noload',
        backend + '/virtual.dat', filesystem]


def test_writable_mount_shows_backend_error_if_conversion_is_refused(session, tmp_path):
    manager = make_manager()
    manager._make_session_mount_point = Mock(return_value=str(tmp_path / 'backend'))
    manager._safe_fusermount = Mock()
    manager._cleanup_process = Mock()
    manager._safe_rmtree = Mock()
    process = Mock()
    process.poll.return_value = 1
    process.communicate.return_value = (b'', b'Format-500 conversion requires an even split size in MiB.\n')
    with patch.object(cli.subprocess, 'Popen', return_value=process), \
            patch.object(cli.subprocess, 'run') as run:
        with pytest.raises(OSError, match='even split size'):
            with manager._mount_session_read(str(session), 'dynfilefs', writable=True):
                pytest.fail('A refused conversion appeared mounted')
    run.assert_not_called()


def test_encrypted_source_uses_read_only_loop_and_mapper_without_ext4_replay(tmp_path):
    manager = make_manager()
    image = str(tmp_path / 'virtual.dat')
    filesystem = str(tmp_path / 'filesystem')
    manager._luks_mapper_name = Mock(return_value='test-mapper')
    manager._cryptsetup = Mock()
    manager._make_session_mount_point = Mock(return_value=filesystem)
    manager._safe_unmount = Mock(return_value=True)
    manager._safe_rmtree = Mock()

    with patch.object(cli.subprocess, 'run',
                      side_effect=[info(b'/dev/loop7\n')] + [info(b'')] * 3) as run:
        with manager._mount_luks(image, b'secret', False, session_path=str(tmp_path)):
            pass

    commands = [entry[0][0] for entry in run.call_args_list]
    assert commands[0] == [
        'losetup', '--find', '--show', '--read-only', '--', image]
    assert commands[1] == [
        'mount', '-o', 'ro,noload', '/dev/mapper/test-mapper', filesystem]
    manager._cryptsetup.assert_called_once_with([
        'cryptsetup', 'open', '--type', 'luks', '--key-file', '-', '--readonly',
        '/dev/loop7', 'test-mapper'], b'secret')


def test_usage_counts_recovery_companions_not_arbitrary_files(session):
    for filename in ('changes.dat.log', 'changes.dat.work', 'changes.dat.next'):
        (session / filename).write_bytes(b'X' * 8192)
    (session / 'changes.dat.unrelated').write_bytes(b'X' * 32768)
    expected = sum(path.stat().st_blocks * 512 for path in session.iterdir()
                   if path.name != 'changes.dat.unrelated')
    assert make_manager()._get_dynfilefs_size(str(session)) == expected


def test_usage_includes_format_604_snapshot_records_not_other_files(session):
    for name in ('changes.dat.txn', 'changes.dat.txn.lock'):
        (session / name).write_bytes(b'X' * 8192)
    history = session / 'changes.dat.snapshots'
    history.mkdir()
    (history / '0000000000000001.snap').write_bytes(b'X' * 32768)
    (history / 'orphan').write_bytes(b'X' * 65536)
    (session / 'changes.dat.unrelated').write_bytes(b'X' * 65536)
    (history / '0000000000000002.snap').symlink_to(history / 'orphan')
    expected = sum(path.stat().st_blocks * 512 for path in session.iterdir()
                   if path.is_file() and path.name != 'changes.dat.unrelated')
    expected += (history / '0000000000000001.snap').stat().st_blocks * 512
    assert make_manager()._get_dynfilefs_size(str(session)) == expected
