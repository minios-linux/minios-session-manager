from pathlib import Path
from unittest.mock import patch


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


def test_default_window_width_fits_complete_session_rows():
    assert 'self.window.set_default_size(680, 500)' in SOURCE


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
    assert 'Gtk.Button(label=_("Save Now"))' not in SOURCE
    assert 'Gtk.MenuItem.new_with_mnemonic(_("_Save Now"))' in SOURCE
    assert 'squashfs_options.show_all()' in SOURCE
    assert 'squashfs_options.hide()' in SOURCE
    assert 'squashfs_options.set_sensitive(' not in SOURCE
    assert 'save_now_item.set_visible(is_squashfs)' in SOURCE
    assert 'save_settings_item.set_visible(is_squashfs)' in SOURCE
    assert 'is_squashfs and running)' in SOURCE
    assert "getattr(row, 'configuration_supported', True)" in SOURCE
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


def test_privileged_commands_skip_pkexec_for_root():
    from minios_session_manager import _privileged_command

    with patch('minios_session_manager.os.geteuid', return_value=0):
        assert _privileged_command(['minios-session', 'list']) == [
            'minios-session', 'list']
    with patch('minios_session_manager.os.geteuid', return_value=1000):
        assert _privileged_command(['minios-session', 'list']) == [
            'pkexec', 'minios-session', 'list']


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


def test_backend_recommends_optional_dynblk_stack():
    control = (ROOT / "debian/control").read_text(encoding="utf-8")
    backend_stanza = control.split("Package: minios-session\n", 1)[1].split("\nPackage: ", 1)[0]
    assert "Recommends: dynblk (>= 1.0.0), dynblk-dkms (>= 1.0.0)," in backend_stanza
    assert "dynblk" not in backend_stanza.split("Recommends:", 1)[0].split("Depends:", 1)[1]


def test_split_packages_have_disjoint_payloads_and_exact_backend_dependency():
    backend = set((ROOT / "debian/minios-session.install").read_text(
        encoding="utf-8").splitlines())
    frontend = set((ROOT / "debian/minios-session-manager.install").read_text(
        encoding="utf-8").splitlines())
    control = (ROOT / "debian/control").read_text(encoding="utf-8")

    assert backend.isdisjoint(frontend)
    assert "usr/bin/minios-session" in backend
    assert "usr/bin/minios-session-manager" in frontend
    assert "minios-session (= ${binary:Version})" in control
    assert "Breaks: minios-session-manager (<< 1.3.0)" in control
    assert "Replaces: minios-session-manager (<< 1.3.0)" in control


def test_persistence_alert_autostart_is_safe_after_package_removal():
    desktop = (ROOT / "share/autostart/minios-persistence-alert.desktop").read_text(
        encoding="utf-8")
    assert "Exec=minios-persistence-alert\n" in desktop
    assert "TryExec=minios-persistence-alert\n" in desktop


def test_loading_overlay_stays_hidden_after_initial_refresh():
    assert "self.loading_box.set_no_show_all(True)" in SOURCE
    assert SOURCE.index("self.loading_box.set_no_show_all(True)") < SOURCE.index(
        "self.loading_box.set_visible(False)")
    assert "self.loading_box.set_state('running')" in SOURCE


def test_healthy_banners_and_loading_footer_state_are_centralized():
    assert "self.sessions_status_banner.set_visible(intent != 'success')" in SOURCE
    assert "self._loading_visible = True" in SOURCE
    assert "self._loading_visible = False" in SOURCE
    assert "available = not getattr(self, '_loading_visible', False)" in SOURCE


def test_ram_only_status_explains_missing_persistent_storage():
    assert "Persistent sessions are unavailable" in SOURCE
    assert "Sessions directory not found" not in SOURCE


def test_dynblk_gui_is_capability_driven_and_resizable():
    assert "compatible_modes = ['native', 'dynfilefs', 'raw']" in SOURCE
    assert "for mode in ('raw', 'dynfilefs', 'dynblk', 'vmdk')" in SOURCE
    assert "if session_mode in ('dynblk', 'vmdk'):" in SOURCE
    assert "encryption_combo.append('luks', _(\"LUKS2\"))" in SOURCE
    assert '_(' + '"LUKS Mode"' + ')' not in SOURCE
    assert '_("DynBlk Mode")' in SOURCE
    assert '_("DynBlk compression:")' in SOURCE
    assert "add_class('field-description')" in SOURCE
    assert 'Thin container: default 16 GiB, backend maximum {} MiB' in SOURCE
    assert 'Thin container: backing storage grows on demand' in SOURCE
    assert 'size_info_label.set_sensitive(' not in SOURCE


def test_dynblk_completion_is_capability_gated():
    completion = (ROOT / "completion/minios-session").read_text(encoding="utf-8")
    assert 'minios-initramfs-dynblk' in completion
    assert 'modinfo dynblk >/dev/null 2>&1' in completion
    assert 'session_modes+=" dynblk"' in completion
    assert '_minios_session_dynblk_compressions' in completion
    assert '--show-depends "crypto-$codec"' in completion
    assert 'dynblk_compression_values="$(_minios_session_dynblk_compressions)"' in completion
    assert '--compression' in completion
    assert "grep -Fqx 'luks-layer-v1'" in completion
    assert 'session_modes+=" luks"' not in completion


def test_luks_is_documented_as_layered_encryption():
    documentation = "\n".join(
        (ROOT / name).read_text(encoding="utf-8")
        for name in (
            "README.md",
            "manpages/en/minios-session.1",
            "manpages/en/minios-session-manager.1",
        )
    )
    assert "changes.luks" not in documentation
    assert "perchmode=luks" not in documentation
    assert "luks-layer-v1" in documentation
    assert "clone" in documentation.lower()


def test_gui_exposes_physical_clone_separately_from_copy():
    assert 'C_lone Session' in SOURCE
    assert "args = ['clone', self.selected_session_id, '--json']" in SOURCE


def test_manpage_sources_use_package_title():
    for name in ("minios-session-manager.1", "minios-session.1"):
        first_line = (ROOT / "manpages/en" / name).read_text(
            encoding="utf-8").splitlines()[0]
        assert '"MiniOS Session Manager" "User Commands"' in first_line


def test_translated_manpages_preserve_cli_command_names():
    command_signatures = (
        r"\fBlist\fP",
        r"\fBactive\fP",
        r"\fBrunning\fP",
        r"\fBinfo\fP",
        r"\fBactivate \fP",
        r"\fBsave \fP",
        r"\fBmount \fP",
        r"\fBchange\-passphrase \fP",
        r"\fBcreate [",
        r"\fBsettings \fP",
        r"\fBdelete \fP",
        r"\fBreclaim \fP",
        r"\fBresize \fP",
        r"\fBexport \fP",
        r"\fBimport \fP",
        r"\fBcopy \fP",
        r"\fBclone \fP",
        r"\fBconvert \fP",
        r"\fBcleanup [",
        r"\fBstatus\fP",
    )
    for language in ("de", "es", "fr", "id", "it", "pt", "pt_BR", "ru"):
        manpage = ROOT / "manpages" / language / "minios-session.{}.1".format(
            language)
        contents = manpage.read_text(encoding="utf-8")
        for signature in command_signatures:
            assert signature in contents, "{} is missing from {}".format(
                signature, manpage)


def test_luks_passphrase_dialog_has_content_margins():
    start = SOURCE.index("def _prompt_luks_passphrase")
    end = SOURCE.index("def _get_session_mode", start)
    dialog_source = SOURCE[start:end]
    for edge in ("start", "end", "top", "bottom"):
        assert "content.set_margin_{}(12)".format(edge) in dialog_source
