"""Creation/default publication on a custom session tree."""
import json
from unittest.mock import Mock

from minios_session import SessionManager


def writable_manager(tmp_path):
    """Create a target-session manager without depending on host mount flags."""
    manager = SessionManager(custom_sessions_dir=str(tmp_path))
    manager.check_sessions_directory_status = Mock(return_value={"writable": True})
    manager._validate_target_mode = Mock(return_value=(True, None))
    manager._check_free_space = Mock(return_value=(True, None))
    return manager


def test_create_and_activate_preserves_running_selector(tmp_path):
    manager = writable_manager(tmp_path)
    success, message = manager.create_session("native")
    assert success, message
    metadata = manager._read_sessions_metadata()
    assert metadata["default"] is None
    metadata["default"] = "1"
    metadata["running"] = "1"
    assert manager._write_sessions_metadata(metadata)
    success, message = manager.create_session("native", activate=True)
    assert success, message
    metadata = json.loads((tmp_path / "session.json").read_text())
    assert metadata["default"] == "2"
    assert metadata["running"] == "1"
    assert set(metadata["sessions"]) == {"1", "2"}
    conf = (tmp_path / "session.conf").read_text()
    assert "default=2\n" in conf and "running=1\n" in conf


def test_failed_activation_publication_preserves_old_default(tmp_path):
    manager = writable_manager(tmp_path)
    success, message = manager.create_session("native", activate=True)
    assert success, message
    original = (tmp_path / "session.json").read_bytes()
    manager._write_sessions_metadata = Mock(return_value=False)
    success, message = manager.create_session("native", activate=True)
    assert success is False
    assert not (tmp_path / "2").exists()
    assert (tmp_path / "1").is_dir()
    assert (tmp_path / "session.json").read_bytes() == original


def test_empty_native_session_needs_no_container_reservation(tmp_path):
    manager = writable_manager(tmp_path)
    manager._check_free_space = Mock(return_value=(True, None))
    success, message = manager.create_session("native", activate=True)
    assert success, message
    manager._check_free_space.assert_called_once_with(str(tmp_path), 1)
