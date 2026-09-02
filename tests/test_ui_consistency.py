from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "lib/minios_session_manager.py").read_text(encoding="utf-8")
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

def test_loading_overlay_stays_hidden_after_initial_refresh():
    assert "self.loading_box.set_no_show_all(True)" in SOURCE
    assert SOURCE.index("self.loading_box.set_no_show_all(True)") < SOURCE.index(
        "self.loading_box.set_visible(False)")
