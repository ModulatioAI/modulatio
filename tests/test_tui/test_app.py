"""Tests for slice #20 — TUI shell + Prompt tab + stub kickoff.

First launchable TUI. The package skeleton lands; the Prompt tab works
end-to-end in stub mode; other tabs render placeholders (not crashes);
commands registry is hardcoded and discoverable.

Textual test rhythm: ``App.run_test()`` returns a Pilot context manager
that drives key presses and mouse events on an in-memory terminal.
Widget state can be queried via ``app.query_one(selector)``; app-level
attributes we set inside handlers are queryable directly.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from modulatio import vault


# ─── Fixture: isolate vault for tests ───────────────────────────────────────


@pytest.fixture
def tui_vault(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    return tmp_path


# ─── Package-level smoke: imports + commands registry ───────────────────────


def test_tui_package_exposes_run_entry_point():
    """``modulatio-tui`` entry point in pyproject.toml points to
    ``modulatio.tui:run`` — the module must export a callable of that
    name or the entry point crashes."""
    from modulatio.tui import run

    assert callable(run)


def test_commands_registry_has_entries():
    """Slice #27 will build the F1 command reference modal against
    ``modulatio.tui.commands.COMMANDS``. This slice just pins that
    the registry exists and has at least one entry so #27 has a
    surface to consume."""
    from modulatio.tui.commands import COMMANDS

    assert len(COMMANDS) >= 1


def test_commands_entries_have_shape_for_f1_modal():
    """Each command carries enough to render a row in the F1 modal:
    keyboard shortcut, name, description, category."""
    from modulatio.tui.commands import COMMANDS

    for cmd in COMMANDS:
        assert hasattr(cmd, "shortcut")
        assert hasattr(cmd, "name")
        assert hasattr(cmd, "description")
        assert hasattr(cmd, "category")


# ─── Textual: app launches ───────────────────────────────────────────────────


async def test_app_launches_in_stub_mode(tui_vault):
    """Bare smoke — the App constructs and enters the Textual event
    loop without exceptions. If this fails, nothing else works."""
    from modulatio.tui.app import ModulatioApp

    app = ModulatioApp(project_code="TST", stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
    # No exception → pass.


async def test_app_exposes_expected_workspace_tabs(tui_vault):
    """The shell's core workspace tabs. Models + Agents are now the unified
    CONFIG configurator. Missing tabs = design regression."""
    from modulatio.tui.app import ModulatioApp

    app = ModulatioApp(project_code="TST", stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        expected = {
            "tab-prompt",
            "tab-tickets",
            "tab-config",  # unified models + agents configurator
            "tab-skills",
            "tab-artifacts",
        }
        for tab_id in expected:
            assert app.query(f"#{tab_id}"), f"missing tab {tab_id!r}"
        # the old standalone Models/Agents tabs are retired
        assert not app.query("#tab-models")
        assert not app.query("#tab-agents")


async def test_other_tabs_render_placeholders_without_crashing(tui_vault):
    """The 6 not-yet-built tabs display a 'coming in slice #X' placeholder.
    Crashing on tab click would block users from navigating at all."""
    from modulatio.tui.app import ModulatioApp

    app = ModulatioApp(project_code="TST", stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        # Navigate to each non-prompt tab — no exception on focus.
        for tab_id in ("tab-tickets", "tab-config", "tab-skills",
                       "tab-artifacts"):
            tab = app.query_one(f"#{tab_id}")
            assert tab is not None


# ─── Prompt tab: input + kickoff produce a completion ───────────────────────


async def test_prompt_tab_has_input_and_kickoff_button(tui_vault):
    """Input widget + kickoff button are the core Prompt-tab affordances."""
    from textual.widgets import Button

    from modulatio.tui.app import ModulatioApp

    app = ModulatioApp(project_code="TST", stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        from textual.widgets import TextArea
        assert app.query_one("#prompt-input", TextArea) is not None
        assert app.query_one("#prompt-kickoff", Button) is not None


async def test_stub_kickoff_updates_response_text(tui_vault):
    """Happy path: type objective → click kickoff → app's
    ``last_summary_text`` reflects the stub run result."""
    from textual.widgets import TabbedContent, TextArea

    from modulatio.tui.app import ModulatioApp

    app = ModulatioApp(project_code="TST", stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        # KICK OFF lives on the TEAM floor now — flip there, drop the objective.
        app.query_one("#console-streams", TabbedContent).active = "stream-team-pane"
        await pilot.pause()
        inp = app.query_one("#kickoff-objective", TextArea)
        inp.text = "Write a stub note on memory gardens"
        await pilot.click("#prompt-kickoff")
        await pilot.pause()

    assert app.last_summary_text, "kickoff should populate last_summary_text"
    # Stub run always completes with at least one goal + one draft.
    assert "goal" in app.last_summary_text.lower()
    assert "draft" in app.last_summary_text.lower()


async def test_empty_objective_does_not_run_kickoff(tui_vault):
    """Empty input shouldn't silently kick off a no-op run — surface
    a nudge to the user instead."""
    from textual.widgets import TabbedContent

    from modulatio.tui.app import ModulatioApp

    app = ModulatioApp(project_code="TST", stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one("#console-streams", TabbedContent).active = "stream-team-pane"
        await pilot.pause()
        # Objective box left blank.
        await pilot.click("#prompt-kickoff")
        await pilot.pause()

    # No kickoff fired.
    assert "goal" not in app.last_summary_text.lower()
    # But some feedback text surfaced.
    assert app.last_summary_text
