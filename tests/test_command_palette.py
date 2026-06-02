"""Tests for #27 — F1 command modal → Textual command palette.

The original slice #27 was a custom F1 modal; F1 got reassigned to
Leader pane focus in #31a so a different mechanism is needed. Textual
ships a command palette already (default key Ctrl+P / Cmd+P) — this
slice adds a Modulatio-specific Provider that exposes tab-switch
commands through it.

Tests focus on the data + wiring (the _TAB_COMMANDS table and the
COMMANDS set on ModulatioApp). The live Hit-yielding behaviour
exercises Textual's matcher infrastructure, which is upstream-tested
and only callable inside a running app context.
"""
from __future__ import annotations

from modulatio.tui.command_palette import _TAB_COMMANDS, ModulatioCommands


# ─── Tab-command data ───────────────────────────────────────────────────────


def test_tab_commands_covers_every_real_v2_tab():
    """Every primary v2 tab should be addressable via the palette."""
    tab_ids = {tab_id for _, tab_id, _ in _TAB_COMMANDS}
    assert "tab-prompt" in tab_ids
    assert "tab-tickets" in tab_ids
    assert "tab-artifacts" in tab_ids
    assert "tab-config" in tab_ids  # unified models + agents configurator
    assert "tab-skills" in tab_ids
    assert "tab-memory" in tab_ids
    assert "tab-cron" in tab_ids


def test_tab_commands_have_label_and_help():
    """Every entry has a non-empty label, tab id, and help text."""
    for label, tab_id, help_text in _TAB_COMMANDS:
        assert label
        assert tab_id.startswith("tab-")
        assert help_text


def test_tab_commands_labels_unique():
    """No duplicate labels — palette filtering would surface dupes
    confusingly."""
    labels = [label for label, _, _ in _TAB_COMMANDS]
    assert len(labels) == len(set(labels))


# ─── App registration ──────────────────────────────────────────────────────


def test_app_commands_includes_modulatio_provider():
    """ModulatioApp.COMMANDS contains ModulatioCommands so the palette
    surfaces our actions on Ctrl+P."""
    from modulatio.tui.app import ModulatioApp

    assert ModulatioCommands in ModulatioApp.COMMANDS


def test_app_commands_preserves_textual_defaults():
    """We add to the default COMMANDS rather than replacing — Textual's
    built-in commands (theme picker, help, etc.) stay accessible."""
    from textual.app import App

    from modulatio.tui.app import ModulatioApp

    # Every default Textual provider should still be registered.
    for provider in App.COMMANDS:
        assert provider in ModulatioApp.COMMANDS
