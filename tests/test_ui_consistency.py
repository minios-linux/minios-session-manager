from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "lib/minios_session_manager.py").read_text(encoding="utf-8")
ALERT_SOURCE = (ROOT / "lib/minios_persistence_alert.py").read_text(encoding="utf-8")
SHARED_UI_SOURCE = (ROOT / "lib/minios_session_ui.py").read_text(encoding="utf-8")
CSS = (ROOT / "share/styles/style.css").read_text(encoding="utf-8")


def session_status_branch():
    start = SOURCE.index("# Add CSS classes based on session status")
    end = SOURCE.index("main_box = Gtk.Box", start)
    return SOURCE[start:end]


def test_session_rows_use_shared_content_and_status_classes():
    assert SOURCE.count("add_class('manager-state-row-content')") == 1
    for status in ("active", "running", "available"):
        assert f"add_class('row-status-{status}')" in session_status_branch()

    assert "session-item" not in SOURCE
    assert "session-status-" not in SOURCE
    assert "min-height: 80px" not in CSS
    assert "padding: 12px 16px" not in CSS
    assert "border-left" not in CSS


def test_footer_buttons_use_compact_shared_application_height():
    assert ".manager-footer button" not in CSS


def test_session_list_extends_to_centered_footer_actions():
    assert 'toolbar_box.set_halign(Gtk.Align.CENTER)' in SOURCE
    assert 'toolbar_box.set_halign(Gtk.Align.END)' not in SOURCE
    assert 'toolbar_box.set_margin_top(' not in SOURCE
    assert '.manager-footer {' not in CSS


def test_active_session_status_has_precedence_over_running():
    branch = session_status_branch()

    assert branch.index("if is_active:") < branch.index("elif is_running:")
    assert branch.index("add_class('row-status-active')") < branch.index(
        "add_class('row-status-running')") < branch.index(
        "add_class('row-status-available')")


def test_running_badge_remains_independently_warning_colored():
    assert "running_label.get_style_context().add_class('badge-warning')" in SOURCE


def test_squashfs_create_policies_and_save_progress_are_exposed():
    assert '_("SquashFS Mode")' in SOURCE
    assert '_("Save automatically at shutdown (recommended)")' in SOURCE
    assert '_("Periodic save:")' in SOURCE
    assert '_("Every 30 minutes")' in SOURCE
    assert 'Gtk.Button(label=_("Save Now"))' in SOURCE
    assert "getattr(row, 'mode', 'unknown') == 'squashfs'" in SOURCE
    assert "getattr(row, 'is_running', False)" in SOURCE
    assert "self.sessions_writable and not active)" in SOURCE
    assert "'save', session_id, '--json', '--progress'" in SOURCE
    assert 'GLib.timeout_add(500, show_progress_if_needed)' in SOURCE
    assert '_("Saving Session")' in SOURCE


def test_session_frontends_share_save_phases_and_notifications():
    assert SOURCE.count("from minios_session_ui import") == 1
    assert ALERT_SOURCE.count("from minios_session_ui import") == 1
    assert SOURCE.count("'prepare': _(") == 0
    assert ALERT_SOURCE.count('"prepare": _("') == 0
    assert SHARED_UI_SOURCE.count("'prepare': _(") == 1
    assert "def send_desktop_notification" in SHARED_UI_SOURCE


def test_periodic_save_uses_shared_confirmation_dialog():
    assert "ask_confirmation)" in ALERT_SOURCE
    assert "return ask_confirmation(" in ALERT_SOURCE


def test_simple_archive_choosers_use_minios_gui_helpers():
    assert "choose_save_file(" in SOURCE
    assert "choose_open_file(" in SOURCE
    assert "Gtk.FileChooserDialog(" not in SOURCE


def test_repeated_background_work_uses_task_outcomes():
    assert "def complete(outcome):" in SOURCE
    assert "def finish_fetch(outcome):" in SOURCE
    assert "BackgroundTask(" in SOURCE
    assert SOURCE.count("threading.Thread(") == 1


def test_repeated_operation_presentations_use_shared_widgets():
    assert "self.loading_box = OperationView(" in SOURCE
    assert "progress_dialog = ProgressDialog(" in SOURCE
    assert "dialog = ProgressDialog(" in ALERT_SOURCE


def test_package_requires_minios_gui_1_4_api():
    control = (ROOT / "debian/control").read_text(encoding="utf-8")
    assert control.count("python3-minios-gui (>= 1.4.0)") == 2

def test_loading_overlay_stays_hidden_after_initial_refresh():
    assert "self.loading_box.set_no_show_all(True)" in SOURCE
    assert SOURCE.index("self.loading_box.set_no_show_all(True)") < SOURCE.index(
        "self.loading_box.set_visible(False)")
    assert "self.loading_box.set_state('running')" in SOURCE


def test_ram_only_status_explains_missing_persistent_storage():
    assert "Persistent sessions are unavailable" in SOURCE
    assert "Sessions directory not found" not in SOURCE


def test_manpage_versions_match_changelog():
    changelog = (ROOT / "debian/changelog").read_text(encoding="utf-8")
    version = re.search(r'^minios-session-manager \(([^)]+)\)', changelog).group(1)
    for name in ("minios-session-manager.1", "minios-session.1"):
        first_line = (ROOT / "debian" / name).read_text(
            encoding="utf-8").splitlines()[0]
        assert 'MiniOS Session Manager {}"'.format(version) in first_line
