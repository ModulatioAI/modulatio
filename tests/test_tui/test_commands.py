"""Slice 5: slash-command dispatcher tests."""

from __future__ import annotations


from modulatio.tui import commands as cmd_mod


# === dispatch parsing ===

def test_dispatch_returns_unhandled_for_non_slash():
    result = cmd_mod.dispatch("not a command")
    assert result.handled is False


def test_dispatch_unknown_command_returns_handled_with_error():
    result = cmd_mod.dispatch("/notreal")
    assert result.handled is True
    assert result.ok is False
    assert "Unknown command" in result.output


def test_dispatch_empty_slash_returns_handled_error():
    """`/` alone should not crash."""
    result = cmd_mod.dispatch("/")
    assert result.handled is True
    assert result.ok is False


def test_dispatch_strips_leading_whitespace():
    result = cmd_mod.dispatch("   /help")
    assert result.handled is True
    assert result.ok is True


# === opencode-parity harness commands ===

def test_models_opens_config_models():
    result = cmd_mod.dispatch("/models")
    assert result.ok is True and result.side_effect == "open_models"


def test_new_archives_and_resets():
    result = cmd_mod.dispatch("/new")
    assert result.ok is True and result.side_effect == "leader_new_conversation"


def test_editor_opens_external_editor():
    result = cmd_mod.dispatch("/editor")
    assert result.ok is True and result.side_effect == "open_editor"


def test_compact_is_a_friendly_deferred():
    result = cmd_mod.dispatch("/compact")
    assert result.handled is True and result.ok is False
    assert "roadmap" in result.output.lower()


# === Specific commands ===

def test_help_lists_all_handler_commands():
    result = cmd_mod.dispatch("/help")
    assert result.ok is True
    assert "/help" in result.output
    assert "/setup" in result.output
    assert "/memory" in result.output


def test_help_alias_question_mark():
    """`/?` should route to the same handler as `/help`."""
    result = cmd_mod.dispatch("/?")
    assert result.ok is True
    assert "/help" in result.output


def test_setup_returns_pointer_to_cli_command():
    result = cmd_mod.dispatch("/setup")
    assert result.ok is True
    assert "modulatio setup" in result.output
    assert result.side_effect == "open_setup_wizard"


def test_clear_emits_clear_response_side_effect():
    result = cmd_mod.dispatch("/clear")
    assert result.side_effect == "clear_response"


def test_memory_no_args_switches_to_tab():
    result = cmd_mod.dispatch("/memory")
    assert result.ok is True
    assert result.side_effect == "switch_tab:memory"


def test_memory_with_agent_focuses_agent():
    result = cmd_mod.dispatch("/memory writer-a")
    assert result.ok is True
    assert result.side_effect == "switch_tab:memory:writer-a"


def test_open_requires_argument():
    result = cmd_mod.dispatch("/open")
    assert result.ok is False
    assert "Usage" in result.output


def test_open_with_path_emits_side_effect():
    result = cmd_mod.dispatch("/open notes/leader.md")
    assert result.ok is True
    assert result.side_effect == "open_file:notes/leader.md"


# === Leader permission commands (two-lane Leader widen) ===

def test_rp_revokes_all_permissions():
    """`/rp` = revoke ALL Leader grants (the escape hatch)."""
    result = cmd_mod.dispatch("/rp")
    assert result.ok is True
    assert result.side_effect == "leader_revoke_permissions"


def test_work_requires_argument():
    result = cmd_mod.dispatch("/work")
    assert result.ok is False
    assert "Usage" in result.output


def test_work_with_path_emits_widen_side_effect():
    result = cmd_mod.dispatch("/work /home/cknox/projects/myapp")
    assert result.ok is True
    assert result.side_effect == "leader_work_here:/home/cknox/projects/myapp"


def test_work_preserves_backslash_path_verbatim():
    """Raw-remainder: a Windows-style path must survive shlex un-mangled."""
    result = cmd_mod.dispatch(r"/work C:\Users\me\proj")
    assert result.side_effect == r"leader_work_here:C:\Users\me\proj"


def test_refresh_emits_refresh_all_tabs():
    result = cmd_mod.dispatch("/refresh")
    assert result.side_effect == "refresh_all_tabs"


def test_restart_emits_restart_tui():
    result = cmd_mod.dispatch("/restart")
    assert result.side_effect == "restart_tui"


def test_version_returns_modulatio_string():
    result = cmd_mod.dispatch("/version")
    assert result.ok is True
    assert "Modulatio" in result.output


def test_bug_opens_the_report_form():
    """/bug routes to the bug-report modal via a side-effect."""
    result = cmd_mod.dispatch("/bug")
    assert result.handled is True
    assert result.side_effect == "open_bug_report"


# === Deferred slash-commands ===


def test_cron_active_in_slice_7():
    """Slice 7 promoted /cron from deferred placeholder to active handler."""
    result = cmd_mod.dispatch("/cron")
    assert result.handled is True
    assert result.ok is True
    assert result.side_effect == "switch_tab:cron"


def test_daemon_deferred_returns_friendly_error():
    result = cmd_mod.dispatch("/daemon")
    assert result.ok is False
    assert "slice 8" in result.output.lower()


def test_telegram_deferred_returns_friendly_error():
    result = cmd_mod.dispatch("/telegram")
    assert result.ok is False
    assert "slice 8" in result.output.lower()


# === list_commands ===

def test_list_commands_includes_deferred():
    cmds = cmd_mod.list_commands()
    shortcuts = {c.shortcut for c in cmds}
    assert "/help" in shortcuts
    assert "/cron" in shortcuts


def test_command_categories_grouping():
    """Help output groups commands by category."""
    result = cmd_mod.dispatch("/help")
    # Sample categories from registry
    for cat in ("System", "Navigation", "Help"):
        assert cat in result.output


# === Shell-quoting ===

def test_dispatch_handles_quoted_arguments():
    """`/open "notes with spaces.md"` should parse correctly."""
    result = cmd_mod.dispatch('/open "notes with spaces.md"')
    assert result.ok is True
    assert result.side_effect == "open_file:notes with spaces.md"


def test_open_preserves_windows_style_backslash_path():
    """Regression (r2 LOW): posix shlex.split silently ate backslashes,
    corrupting a Windows-style path. /open must keep them verbatim."""
    result = cmd_mod.dispatch(r"/open C:\Users\me\report.pdf")
    assert result.ok is True
    assert result.side_effect == r"open_file:C:\Users\me\report.pdf"


def test_open_preserves_relative_backslash_path():
    result = cmd_mod.dispatch(r"/open notes\sub\file.md")
    assert result.ok is True
    assert result.side_effect == r"open_file:notes\sub\file.md"


def test_open_single_quoted_path_strips_quotes():
    result = cmd_mod.dispatch("/open 'notes with spaces.md'")
    assert result.ok is True
    assert result.side_effect == "open_file:notes with spaces.md"


def test_open_blank_remainder_still_requires_argument():
    """Whitespace-only after /open must not become an empty-path open."""
    result = cmd_mod.dispatch("/open    ")
    assert result.ok is False
    assert "Usage" in result.output


def test_open_trims_surrounding_whitespace_on_path():
    result = cmd_mod.dispatch("/open   notes/leader.md  ")
    assert result.ok is True
    assert result.side_effect == "open_file:notes/leader.md"


def test_cls_dispatches_clear_active_tv():
    """W4: /cls clears the ACTIVE console TV (F4-aware) — display-only."""
    from modulatio.tui import commands
    result = commands.dispatch("/cls")
    assert result.handled and result.ok
    assert result.side_effect == "clear_active_tv"
