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


async def test_prompt_tab_has_chat_input(tui_vault):
    """The chat composer is the core Console affordance (jobs launch from it via
    /kickoff … /end; there is no separate kickoff button)."""
    from modulatio.tui.app import ModulatioApp
    from modulatio.tui.widgets.chat_input import ChatInput

    app = ModulatioApp(project_code="TST", stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.query_one("#prompt-input", ChatInput) is not None
        assert not app.query("#prompt-kickoff")  # the kickoff button is gone


async def test_stub_kickoff_updates_response_text(tui_vault):
    """Happy path: launch a job (the /kickoff path → _run_kickoff) → app's
    ``last_summary_text`` reflects the stub run result."""
    from modulatio.tui.app import ModulatioApp

    app = ModulatioApp(project_code="TST", stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._run_kickoff("Write a stub note on memory gardens")
        kickoff_workers = [w for w in app.workers if w.group == "kickoff"]
        await app.workers.wait_for_complete(kickoff_workers)
        await pilot.pause()

    assert app.last_summary_text, "kickoff should populate last_summary_text"
    # Stub run always completes with at least one goal + one draft.
    assert "goal" in app.last_summary_text.lower()
    assert "draft" in app.last_summary_text.lower()


async def test_refresh_all_tabs_invokes_panel_on_show(tui_vault):
    """Regression: ``/refresh`` (side-effect ``refresh_all_tabs``) must
    actually re-run each tab panel's ``on_show`` hook. The panels are
    ``Vertical`` subclasses, NOT Textual ``Screen`` widgets — the old
    ``query("Screen")`` matched only the app's screen container and
    refreshed nothing. We spy on a real panel's ``on_show`` and assert
    the refresh reaches it."""
    from modulatio.tui.app import ModulatioApp
    from modulatio.tui.screens.memory import MemoryScreen

    app = ModulatioApp(project_code="TST", stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()

        # The dispatcher resolves ``on_show`` from the panel *class*; spy on
        # the class hook so we observe the refresh reaching a real panel.
        marker: list[type] = []
        real = MemoryScreen.on_show

        def _cls_spy(self, _real=real):  # noqa: ANN001
            marker.append(MemoryScreen)
            return _real(self)

        MemoryScreen.on_show = _cls_spy  # type: ignore[method-assign]
        try:
            app._apply_side_effect("refresh_all_tabs")
            await pilot.pause()
        finally:
            MemoryScreen.on_show = real  # type: ignore[method-assign]

        assert marker, "refresh_all_tabs did not invoke any panel's on_show"


async def test_empty_objective_does_not_run_kickoff(tui_vault):
    """An empty objective shouldn't silently kick off a no-op run — surface
    a nudge instead."""
    from modulatio.tui.app import ModulatioApp

    app = ModulatioApp(project_code="TST", stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._run_kickoff("")   # empty objective
        await pilot.pause()

    # No kickoff fired.
    assert "goal" not in app.last_summary_text.lower()
    # But some feedback text surfaced.
    assert app.last_summary_text


def test_format_verdict_signoff_surfaces_verdict_and_digest():
    """The sign-off must show the Leader's ACTUAL verdict + a PQR digest — the
    fix for "run completed but no leader sign-off" (the verdict was on disk but
    the stream showed only a stats line)."""
    from modulatio.tui.app import ModulatioApp

    out = ModulatioApp._format_verdict_signoff([
        {"goal_id": "P-G-001", "verdict": "on_the_fence",
         "report_body": "The deliverable is solid and ships.\n\nmore detail"},
    ])
    assert "on the fence" in out  # underscore rendered readable
    assert "P-G-001" in out
    assert "The deliverable is solid and ships." in out  # first paragraph digest
    assert "more detail" not in out  # only the first paragraph
    assert "Reports tab" in out


def test_format_verdict_signoff_dedups_to_last_per_goal_and_empty():
    """A redone goal appends more than once → only its LAST (settled) verdict
    shows. No verdicts → empty string (nothing appended to the message)."""
    from modulatio.tui.app import ModulatioApp

    assert ModulatioApp._format_verdict_signoff([]) == ""
    out = ModulatioApp._format_verdict_signoff([
        {"goal_id": "G1", "verdict": "disappointed", "report_body": "first try"},
        {"goal_id": "G1", "verdict": "satisfied", "report_body": "redone, good now"},
    ])
    assert "satisfied" in out and "redone, good now" in out
    assert "disappointed" not in out and "first try" not in out


# ─── The Leader's headline is honest about partial runs ─────────────────────


def _verdict_result(**kw) -> dict:
    base = dict(goals=2, tasks=21, drafts=0, errors=0,
                blocked_tasks=0, incomplete_goals=0, verdicts=[])
    base.update(kw)
    return base


async def _headline_for(result: dict, tui_vault) -> str:
    from modulatio.tui.app import ModulatioApp
    from modulatio.tui.widgets.stream_view import StreamView

    app = ModulatioApp(project_code="HDL", stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._post_leader_verdict(result, None)
        await pilot.pause()
        tv = app.query_one("#stream-leader", StreamView)
        return tv.messages[-1]


def test_blocked_reservations_excludes_superseded_tasks_of_completed_goals():
    """Escalation-orphan prune (TUI side): a blocked task under a COMPLETED goal
    was superseded — the goal's objective was met another way (e.g. the Leader
    re-planned around stuck work). It must NOT inflate the run's 'reservations'
    count and turn a satisfied run into 'finished with reservations'. Only a
    blocked task whose goal did NOT complete is a real reservation."""
    from types import SimpleNamespace as NS
    from modulatio.tui.app import _blocked_reservations
    from modulatio.types import GoalStatus, TaskStatus

    summary = NS(
        goals=[NS(id="G-1", status=GoalStatus.COMPLETED),
               NS(id="G-2", status=GoalStatus.BLOCKED)],
        tasks=[NS(status=TaskStatus.BLOCKED, goal_id="G-1"),    # superseded → excluded
               NS(status=TaskStatus.BLOCKED, goal_id="G-1"),    # superseded → excluded
               NS(status=TaskStatus.BLOCKED, goal_id="G-2"),    # real reservation
               NS(status=TaskStatus.COMPLETED, goal_id="G-1")],
    )
    assert _blocked_reservations(summary) == 1


async def test_partial_run_headline_owns_what_landed(tui_vault):
    """20 deliverables + 1 satisfied goal must NEVER read as 'Nothing usable
    landed' — the headline owns the reservations without erasing the wins."""
    msg = await _headline_for(_verdict_result(
        drafts=20, blocked_tasks=2, errors=15,
        verdicts=[{"goal_id": "G-2", "verdict": "satisfied",
                   "report_body": "delivered"}],
    ), tui_vault)
    assert "Nothing usable" not in msg
    assert "20 deliverable(s)" in msg
    assert "2 task(s) stayed blocked" in msg


async def test_zero_delivered_headline_stays_blunt(tui_vault):
    msg = await _headline_for(_verdict_result(drafts=0, blocked_tasks=1),
                              tui_vault)
    assert "Nothing usable landed" in msg


async def test_clean_run_headline_unchanged(tui_vault):
    msg = await _headline_for(_verdict_result(drafts=3), tui_vault)
    assert "Job's done" in msg
