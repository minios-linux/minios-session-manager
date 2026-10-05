from minios_session import SessionManager
from minios_persistence_alert import squashfs_session_id
from minios_persistence_alert import session_tray_icon, tray_session_id
from minios_session_ui import save_progress


def test_ram_session_never_calls_disk_save_backend(tmp_path):
    manager = SessionManager.__new__(SessionManager)
    manager.BOOT_STATE_FILE = str(tmp_path / 'boot-state')
    (tmp_path / 'ram-origin').write_text('mode=trim\nsession=1\n')
    success, message, capture = manager.save_session('1')
    assert not success
    assert capture is None
    assert message
    attempted, success, message, capture = manager.autosave_running_session()
    assert not attempted
    assert success


def test_squashfs_tray_does_not_offer_disk_save_for_volatile_store():
    state = {'boot_level': 'ok', 'mode': 'squashfs', 'session': '1', 'durable': '0'}
    assert squashfs_session_id(state) is None
    state['durable'] = '1'
    assert squashfs_session_id(state) == '1'


def test_gui_warns_even_when_ram_store_is_writable():
    from minios_session_manager import SessionManagerGUI
    gui = SessionManagerGUI.__new__(SessionManagerGUI)
    gui.sessions_status = {'found': True, 'ram_backed': True}
    gui.sessions_writable = True
    intent, message = gui._sessions_status_presentation()
    assert intent == 'warning'
    assert 'RAM' in message


def test_ram_tray_keeps_the_session_visible_without_its_device():
    state = {'boot_level': 'ok', 'mode': 'raw', 'session': '2', 'durable': '0',
             'ram': '1', 'store_state': 'missing', 'save_available': '0'}
    assert tray_session_id(state) == '2'
    assert session_tray_icon(state) == 'media-eject'
    state.update(store_state='available', save_available='1')
    assert session_tray_icon(state) == 'document-save'
    state['saving'] = '1'
    assert session_tray_icon(state) == 'view-refresh'
    state.update(saving='0', save_error='copy failed')
    assert session_tray_icon(state) == 'dialog-error'


def test_save_progress_uses_only_valid_measured_counts():
    assert save_progress({'copied_bytes': 25, 'total_bytes': 100}) == .25
    for event in ('freeze', {'copied_bytes': True, 'total_bytes': 100},
                  {'copied_bytes': 101, 'total_bytes': 100},
                  {'copied_bytes': 0, 'total_bytes': 0}):
        assert save_progress(event) is None
