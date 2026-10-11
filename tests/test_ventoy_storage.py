import json
import os
import sys
from unittest.mock import patch

import pytest

from minios_session import SessionManager, SessionStorageUnavailableError


@pytest.fixture
def ventoy_store(tmp_path, monkeypatch):
    marker = tmp_path / 'store-type'
    marker.write_text('ventoy-plugin\n')
    marker.chmod(0o644)
    monkeypatch.setattr(SessionManager, 'STORE_TYPE_FILE', str(marker))
    marker_inode = marker.stat().st_ino
    fstat = os.fstat

    def root_owned_marker(descriptor):
        current = fstat(descriptor)
        if current.st_ino == marker_inode:
            fields = list(current)
            fields[4] = 0
            return os.stat_result(fields)
        return current

    monkeypatch.setattr(os, 'fstat', root_owned_marker)
    return marker


def test_backend_blocks_custom_sessions_directory_before_any_writes(ventoy_store, tmp_path):
    with patch.object(SessionManager, '_ensure_cache_dir') as cache, \
            patch.object(SessionManager, '_detect_session_storage') as detect, \
            pytest.raises(SessionStorageUnavailableError, match='Ventoy'):
        SessionManager(custom_sessions_dir=str(tmp_path))
    cache.assert_not_called()
    detect.assert_not_called()


@pytest.mark.parametrize('command', ('status', 'list', 'create', 'autosave'))
def test_cli_rejects_ventoy_commands_and_custom_directory(ventoy_store, tmp_path,
                                                       monkeypatch, capsys, command):
    import minios_session

    monkeypatch.setattr(sys, 'argv', ['minios-session', command, '--json',
                                    '--sessions-dir', str(tmp_path)])
    with patch('minios_session.os.geteuid', return_value=0), \
            pytest.raises(SystemExit) as result:
        minios_session.main()
    assert result.value.code == 1
    response = json.loads(capsys.readouterr().out)
    assert response['success'] is False
    assert 'Ventoy' in response['error']
    assert not (tmp_path / 'session.conf').exists()


def test_gui_stops_before_creating_interface(ventoy_store):
    gui = pytest.importorskip('minios_session_manager')
    with patch.object(gui.SessionManagerGUI, 'create_interface') as interface, \
            pytest.raises(SessionStorageUnavailableError, match='Ventoy'):
        gui.SessionManagerGUI()
    interface.assert_not_called()


def test_missing_marker_preserves_normal_session_storage(tmp_path, monkeypatch):
    monkeypatch.setattr(SessionManager, 'STORE_TYPE_FILE', str(tmp_path / 'missing'))
    manager = SessionManager(custom_sessions_dir=str(tmp_path))
    assert manager.sessions_dir == str(tmp_path)


def test_guard_monitors_selected_image_not_iso_or_ram(tmp_path, monkeypatch):
    import minios_persistence_guard as guard

    backend = tmp_path / 'backend'
    backend.mkdir()
    current = backend.stat()
    boot_id = tmp_path / 'boot-id'
    boot_id.write_text('current-boot\n')
    state = {'mode': 'ventoy', 'boot_level': 'ok', 'writable': '1',
             'durable': '1', 'boot_id': 'current-boot',
             'sessions_device': str(current.st_dev),
             'sessions_inode': str(current.st_ino)}
    monkeypatch.setattr(guard, 'read_boot_state', lambda *args: state)
    monkeypatch.setattr(guard, 'BOOT_ID_FILE', str(boot_id))
    monkeypatch.setattr(guard, 'VENTOY_STORE_PATHS', (str(backend),))
    monkeypatch.setattr(os.path, 'ismount', lambda path: path == str(backend))
    paths = []

    def space(path):
        paths.append(path)
        assert path == str(backend)
        return 8 * guard.GiB, 16 * guard.GiB, 10000

    monkeypatch.setattr(guard, 'read_space', space)
    monitor = guard.PersistenceGuard()
    assert monitor.sessions_dir == str(backend)
    sample = monitor.sample(boot_warnings=[])
    assert sample['level'] == 'ok'
    assert sample['free_outer_bytes'] == 8 * guard.GiB
    assert paths == [str(backend), str(backend)]
    state['boot_id'] = 'previous-boot'
    assert monitor._detect_ventoy_store() is None
