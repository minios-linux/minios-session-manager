"""A new target session and its boot selection are published together."""
import json
from unittest.mock import patch

from minios_session import SessionManager


def manager_with_existing_session(tmp_path):
    (tmp_path / '1').mkdir()
    metadata = {'default': '1', 'running': '1', 'sessions': {'1': {'mode': 'native'}}}
    (tmp_path / 'session.json').write_text(json.dumps(metadata))
    manager = SessionManager(custom_sessions_dir=str(tmp_path))
    return manager, metadata


def test_create_activate_preserves_running_and_existing_sessions(tmp_path):
    manager, previous = manager_with_existing_session(tmp_path)
    with patch.object(manager, 'check_sessions_directory_status', return_value={'writable': True}), \
         patch.object(manager, '_validate_target_mode', return_value=(True, None)), \
         patch.object(manager, '_check_free_space', return_value=(True, None)):
        success, message = manager.create_session('native', activate=True)
    assert success, message
    metadata = json.loads((tmp_path / 'session.json').read_text())
    new_id = metadata['default']
    assert new_id != previous['default']
    assert (tmp_path / new_id).is_dir()
    assert metadata['running'] == '1'
    assert metadata['sessions']['1'] == previous['sessions']['1']
    assert ('default=' + new_id + '\n') in (tmp_path / 'session.conf').read_text()


def test_failed_creation_does_not_change_default(tmp_path):
    manager, previous = manager_with_existing_session(tmp_path)
    with patch.object(manager, 'check_sessions_directory_status', return_value={'writable': True}), \
         patch.object(manager, '_validate_target_mode', return_value=(True, None)), \
         patch.object(manager, '_check_free_space', return_value=(False, 'full')):
        success, message = manager.create_session('native', activate=True)
    assert success is False
    assert json.loads((tmp_path / 'session.json').read_text()) == previous
    assert sorted(p.name for p in tmp_path.iterdir() if p.is_dir()) == ['1']


def test_native_creation_has_no_container_sized_space_reservation(tmp_path):
    manager, _ = manager_with_existing_session(tmp_path)
    with patch.object(manager, 'check_sessions_directory_status', return_value={'writable': True}), \
         patch.object(manager, '_validate_target_mode', return_value=(True, None)), \
         patch.object(manager, '_check_free_space', return_value=(False, 'stop')) as check:
        manager.create_session('native', activate=True)
    assert check.call_args[0] == (str(tmp_path), 1)
