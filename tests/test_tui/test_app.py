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
from modulatio import config, setup_state
from modulatio.tui.app import ModulatioApp
from modulatio import cron
from datetime import datetime, timezone
from modulatio.tui.app import _relaunch_if_restart


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


# ═══ fold: test_tui_app_resweep_r3.py ═══
# Round-3 re-sweep regression for ``modulatio.tui.app`` (1 finding).
#
# ``on_worker_state_changed`` only branched on
# ``WorkerState.SUCCESS`` and ``WorkerState.ERROR`` for both the ``kickoff`` and
# ``converse`` worker groups. ``WorkerState.CANCELLED`` was silently dropped, so
# neither ``_on_kickoff_done`` nor a converse-lane settle ran when a worker was
# cancelled (app teardown, or Textual cancelling an exclusive worker). For a
# cancelled KICKOFF that meant the elapsed-time ``set_interval`` kept ticking,
# ``_kickoff_pending`` stayed armed, and the launch guard (``_kickoff_tick`` live)
# locked out every future kickoff with "a job is already running". For a cancelled
# CONVERSE with no replacement worker the leader-status spinner hung on
# "thinking" forever.
#
# The fix adds a ``CANCELLED`` branch to both groups: kickoff routes through
# ``_on_kickoff_done(None, None)`` (the full teardown); converse routes through a
# new ``_on_converse_cancelled`` that settles the lanes WITHOUT posting a spurious
# "(cancelled)" chat reply, and is guarded so it never clobbers a replacement
# converse worker's fresh "thinking" status.


PROJECT_CODE = "R3RES"


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "config"
    monkeypatch.setattr(config, "CONFIG_DIR", cfg_dir)
    monkeypatch.setattr(config, "DEFAULTS_FILE", cfg_dir / "defaults.json")
    monkeypatch.setattr(setup_state, "SETUP_STATE_FILE", cfg_dir / "setup-state.json")
    config.save_defaults({"vault_root": str(tmp_path / "vault")})
    config.reload()
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path / "vault")
    yield


class _FakeWorker:
    """Minimal stand-in for a Textual ``Worker`` — ``on_worker_state_changed``
    only ever reads ``.group``, ``.result``, ``.error`` off the worker and
    ``.state`` off the event."""

    def __init__(self, group, state, result=None, error=None):
        self.group = group
        self.state = state
        self.result = result
        self.error = error


class _FakeStateChanged:
    def __init__(self, worker, state):
        self.worker = worker
        self.state = state


def _cancelled_event(group):
    from textual.worker import WorkerState

    worker = _FakeWorker(group, WorkerState.CANCELLED)
    return _FakeStateChanged(worker, WorkerState.CANCELLED)


# ─── kickoff: a cancelled worker must tear down the timer + guards ───────────


@pytest.mark.asyncio
async def test_cancelled_kickoff_tears_down_timer_and_guards():
    """A CANCELLED kickoff worker must run the same teardown as a finished one:
    stop + drop ``_kickoff_tick``, clear ``_kickoff_pending``, re-enable the
    button. Otherwise the timer ticks forever and the launch guard permanently
    locks out new kickoffs."""
    from textual.worker import WorkerState

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()

        # Simulate a kickoff in flight: live tick + armed startup guard.
        stopped = {"n": 0}

        class _Tick:
            def stop(self_inner):
                stopped["n"] += 1

        app._kickoff_tick = _Tick()
        app._kickoff_pending = True
        app._kickoff_started_at = 0.0
        app._kickoff_mode = "stub"

        app.on_worker_state_changed(_cancelled_event("kickoff"))
        await pilot.pause()

        assert stopped["n"] == 1, "cancelled kickoff must stop the elapsed tick"
        assert getattr(app, "_kickoff_tick", None) is None, (
            "cancelled kickoff must drop the tick handle so the launch guard "
            "(_kickoff_tick live) no longer locks out new runs"
        )
        assert app._kickoff_pending is False, (
            "cancelled kickoff must clear the in-flight startup guard"
        )
        # Sanity: with the guards cleared a fresh kickoff is no longer refused.
        assert WorkerState.CANCELLED  # state member exists (guards the import)


@pytest.mark.asyncio
async def test_cancelled_kickoff_does_not_raise_without_result():
    """The CANCELLED kickoff teardown is driven with ``result=None`` — the
    done-handler must tolerate a missing result dict (no KeyError) and reach a
    clean settled state."""
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()
        app._kickoff_pending = True
        # No _kickoff_tick / _kickoff_started_at set — teardown must be tolerant.
        app.on_worker_state_changed(_cancelled_event("kickoff"))
        await pilot.pause()
        assert app._kickoff_pending is False


# ─── converse: a cancelled worker must settle the leader lane ────────────────


@pytest.mark.asyncio
async def test_cancelled_converse_settles_leader_lane_when_no_replacement():
    """A CANCELLED converse worker with NO replacement in flight must settle the
    leader-status lane off "thinking" (set_idle), instead of leaving the spinner
    hung forever."""
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()

        status = app._lane_status("stream-leader-status")
        assert status is not None
        # Arm the spinner as if a converse worker were thinking.
        status.set_activity("leader_thinking")
        assert status._verb is not None

        app.on_worker_state_changed(_cancelled_event("converse"))
        await pilot.pause()

        assert status._verb is None and status._error is None, (
            "cancelled converse with no replacement must settle the leader lane "
            "(spinner off), not leave it stuck on 'thinking'"
        )


@pytest.mark.asyncio
async def test_cancelled_converse_does_not_post_chat_reply():
    """The cancel teardown must NOT inject a "(cancelled)" message into the
    Leader chat — that would be spurious noise in the common case where a second
    message simply replaced the first."""
    from modulatio.tui.widgets.stream_view import StreamView

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()

        posted = []
        view = app.query_one("#stream-leader", StreamView)
        view.add_leader_message = lambda msg: posted.append(msg)  # type: ignore

        app.on_worker_state_changed(_cancelled_event("converse"))
        await pilot.pause()

        assert posted == [], (
            "cancelled converse must settle silently, not post a chat reply"
        )


# ═══ fold: test_tui_app_low_audit.py ═══
# LOW-severity audit regressions for ``modulatio.tui.app`` (findings #85, #86).
#
# #85 — ``switch_tab:cron:<code>`` silently dropped the project filter: the
# ``_apply_side_effect`` third-part handler only honored ``memory``; the cron
# filter was parsed off the side-effect string and then ignored, so ``/cron STA``
# landed on an unfiltered tab.
#
# #86 — ``_on_converse_done`` force-settled the TEAM spinner to ``set_done`` even
# when a *separate* kickoff job (button/F5) was still in flight on its own worker,
# lying about the live floor. It must only force-settle when nothing is running.






# ─── #85: cron project filter must actually apply ───────────────────────────


@pytest.mark.asyncio
async def test_switch_tab_cron_routes_code_to_focus_project():
    """``/cron STA`` → side-effect ``switch_tab:cron:STA`` must forward the
    project code to the cron screen instead of dropping it. app.py routes the
    code to ``CronScreen.focus_project`` (sibling of ``MemoryScreen.focus_agent``).
    Spy on the REAL method (save/restore — never ``del`` it, that would strip the
    method off the class for the rest of the session)."""
    cron.add(name="a", schedule="weekly mon 09:00", project_code="STA", objective="x")

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        from textual.widgets import TabbedContent

        from modulatio.tui.screens.cron import CronScreen

        captured: list[str] = []
        original = CronScreen.focus_project

        def _spy(self, code):
            captured.append(code)
            return original(self, code)

        CronScreen.focus_project = _spy  # type: ignore[assignment]
        try:
            app._apply_side_effect("switch_tab:cron:STA")
            # focus_project is invoked via call_after_refresh — pump the loop.
            for _ in range(6):
                await pilot.pause()
        finally:
            CronScreen.focus_project = original  # type: ignore[assignment]

        assert app.query_one("#app-tabs", TabbedContent).active == "tab-cron"
        assert captured == ["STA"], "the project code must reach the cron screen"


@pytest.mark.asyncio
async def test_cron_focus_project_unknown_code_falls_back_to_all():
    """#85: ``focus_project`` with a blank or unknown code must fall back to All
    projects (``_project_filter is None``) so the Select stays on a valid option."""
    cron.add(name="a", schedule="weekly mon 09:00", project_code="STA", objective="x")

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        from modulatio.tui.screens.cron import CronScreen

        screen = app.query_one(CronScreen)
        screen.focus_project("STA")
        assert screen._project_filter == "STA"
        screen.focus_project("NOPE")  # unknown code
        assert screen._project_filter is None
        screen.focus_project("")  # blank
        assert screen._project_filter is None
        await pilot.pause()


@pytest.mark.asyncio
async def test_switch_tab_cron_without_focus_method_does_not_crash():
    """When the cron screen has no ``focus_project`` yet, ``/cron STA`` must
    still switch tabs cleanly (no crash, code routing is a best-effort no-op)."""
    cron.add(name="a", schedule="weekly mon 09:00", project_code="STA", objective="x")

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        from textual.widgets import TabbedContent

        app._apply_side_effect("switch_tab:cron:STA")
        await pilot.pause()
        await pilot.pause()

        assert app.query_one("#app-tabs", TabbedContent).active == "tab-cron"


# ─── #86: converse-done must not clobber a running kickoff's spinner ─────────


@pytest.mark.asyncio
async def test_converse_done_does_not_settle_running_kickoff_spinner():
    """A kickoff is still in flight (``_running_job_orchestrator`` non-None);
    ``_on_converse_done`` must NOT force the TEAM spinner to done."""
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()

        # TEAM lane shows a running job.
        app._set_lane_status("stream-team-status", "modulating")
        await pilot.pause()
        team = app._lane_status("stream-team-status")
        assert team is not None and team._done is False

        # Pretend a kickoff worker is in flight.
        app._any_job_in_flight = lambda: True  # type: ignore[method-assign]

        app._on_converse_done("hi there")
        await pilot.pause()

        # Spinner must NOT have been force-settled — the kickoff is still live.
        assert team._done is False


@pytest.mark.asyncio
async def test_converse_done_settles_spinner_when_idle():
    """With nothing running, ``_on_converse_done`` still settles the TEAM
    spinner (the belt for the converse run_job error path is preserved)."""
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()

        app._set_lane_status("stream-team-status", "modulating")
        await pilot.pause()
        team = app._lane_status("stream-team-status")
        assert team is not None and team._done is False

        # No job in flight.
        app._any_job_in_flight = lambda: False  # type: ignore[method-assign]

        app._on_converse_done("hi there")
        await pilot.pause()

        assert team._done is True


# ─── pre-ship #F8a: STOP must signal EVERY active orchestrator ───────────────


class _FakeOrch:
    """A stand-in orchestrator: ``_kickoff_active`` says it's running, and an
    ``abort_event`` whose ``.set()`` we can observe."""

    def __init__(self, active: bool = True):
        self._kickoff_active = active

        class _Ev:
            def __init__(self):
                self.was_set = False

            def set(self):
                self.was_set = True

        self.abort_event = _Ev()


@pytest.mark.asyncio
async def test_stop_signals_both_concurrent_orchestrators():
    """F8 with BOTH a converse-driven run_job and a direct kickoff in flight
    must signal ``abort_event`` on BOTH — not just the first one found."""
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()

        conv = _FakeOrch(active=True)
        kick = _FakeOrch(active=True)
        app._conv_orch = conv  # type: ignore[attr-defined]
        app._kickoff_orch = kick  # type: ignore[attr-defined]

        app.action_stop_job()

        assert conv.abort_event.was_set is True
        assert kick.abort_event.was_set is True


@pytest.mark.asyncio
async def test_stop_skips_inactive_orchestrator():
    """An orchestrator that exists but is NOT running (``_kickoff_active`` is
    False) must not be signaled; only the live one is."""
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()

        idle = _FakeOrch(active=False)
        live = _FakeOrch(active=True)
        app._conv_orch = idle  # type: ignore[attr-defined]
        app._kickoff_orch = live  # type: ignore[attr-defined]

        app.action_stop_job()

        assert idle.abort_event.was_set is False
        assert live.abort_event.was_set is True


@pytest.mark.asyncio
async def test_stop_noop_when_nothing_running():
    """F8 with no live orchestrators must be a clean no-op (never raises)."""
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()
        # No _conv_orch / _kickoff_orch attributes at all.
        app.action_stop_job()  # must not raise


# ─── pre-ship #F8b: kickoff startup window must hold the TEAM spinner ─────────


@pytest.mark.asyncio
async def test_kickoff_pending_keeps_converse_done_from_settling():
    """During a kickoff's startup window the worker is scheduled but
    ``_kickoff_orch`` hasn't come up yet, so ``_active_job_orchestrators`` is
    empty. The ``_kickoff_pending`` flag must still make ``_any_job_in_flight``
    True so a concurrent converse-done does NOT falsely settle the spinner."""
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()

        app._set_lane_status("stream-team-status", "modulating")
        await pilot.pause()
        team = app._lane_status("stream-team-status")
        assert team is not None and team._done is False

        # Startup window: pending set, but no orchestrator exposed yet.
        app._kickoff_pending = True  # type: ignore[attr-defined]
        assert getattr(app, "_kickoff_orch", None) is None
        assert app._active_job_orchestrators() == []
        assert app._any_job_in_flight() is True

        app._on_converse_done("hi there")
        await pilot.pause()

        # Must NOT have force-settled — the kickoff is still spinning up.
        assert team._done is False


# ═══ fold: test_tui_app_r2_audit.py ═══
# Round-2 audit regressions for ``modulatio.tui.app`` (3 findings).
#
# A double KICK OFF (two fast F5 presses, or
# F5 while the button-disable guard is bypassed) re-entered ``_run_kickoff`` and
# overwrote ``self._kickoff_tick`` with a fresh ``set_interval`` handle WITHOUT
# stopping the prior one, leaking a timer that ticks forever (its handle is gone,
# so ``_on_kickoff_done`` can never stop it). The fix guards ``_run_kickoff``:
# if a kickoff is already in flight (``_kickoff_tick`` live), the second launch is
# a no-op with a "job already running" status.
#
# ``/restart`` calls ``app.exit(return_code=42)``
# but the entry point ignored the code, so the TUI just quit with a misleading
# "Restarting..." message and never came back. The fix adds ``_relaunch_if_restart``
# which re-execs the interpreter on code 42 (and is a no-op otherwise).
#
# ``_agent_name``'s cache is documented "Cached per
# run" but was built once for the app's whole lifetime and never invalidated, so a
# roster change between runs (new agent / rename) resolved to a stale/empty name.
# The fix drops the cache on each ``kickoff_started`` activity event.


def _kickoff_started_event():
    """A minimal, fully-populated ``kickoff_started`` activity event."""
    from modulatio.types import ActivityEvent

    return ActivityEvent(
        agent_id="orchestrator",
        role="orchestrator",
        phase="kickoff_started",
        task_id=None,
        timestamp=datetime.now(timezone.utc),
    )






# ─── Double KICK OFF must not leak a set_interval timer ──────────────────────


@pytest.mark.asyncio
async def test_double_kickoff_does_not_overwrite_running_tick():
    """A second ``_run_kickoff`` while a kickoff is already in flight must NOT
    replace ``_kickoff_tick`` (which would leak the prior, now-unstoppable
    interval). It short-circuits with a 'job already running' status instead."""
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()

        # Simulate a run already in flight: a live tick handle is the engine's
        # "kickoff is running" signal (set in _run_kickoff, torn down in
        # _on_kickoff_done). Use a sentinel so we can detect any overwrite.
        sentinel = object()
        app._kickoff_tick = sentinel

        # A second launch with a real objective must be refused, not start a
        # new interval that clobbers the handle.
        app._run_kickoff("write a second thing")
        await pilot.pause()

        assert app._kickoff_tick is sentinel, (
            "double kickoff overwrote the live tick handle — the prior "
            "set_interval timer is now leaked (unstoppable)"
        )


@pytest.mark.asyncio
async def test_kickoff_starts_tick_when_none_running():
    """Sanity: with no run in flight (no ``_kickoff_tick``), a stub kickoff
    DOES install a tick handle — the guard only blocks a *second* launch."""
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()
        assert getattr(app, "_kickoff_tick", None) is None

        # ``set_interval`` runs synchronously inside ``_run_kickoff`` (before the
        # background worker is scheduled), so the handle is present the instant
        # the call returns — assert before pumping the loop to avoid racing the
        # fast stub worker's ``_on_kickoff_done`` teardown.
        app._run_kickoff("write a thing")
        assert getattr(app, "_kickoff_tick", None) is not None

        # Let the worker drain so the run tears down cleanly.
        for _ in range(8):
            await pilot.pause()


# ─── /restart must actually relaunch on return_code 42 ───────────────────────


class _FakeApp:
    def __init__(self, return_code):
        self.return_code = return_code


def test_relaunch_if_restart_reexecs_on_code_42(monkeypatch):
    """``_relaunch_if_restart`` must re-exec the interpreter when the app exited
    with the restart sentinel (42). Without this, ``/restart`` just quit."""
    calls = []

    def _fake_execv(path, argv):
        calls.append((path, list(argv)))

    monkeypatch.setattr("os.execv", _fake_execv)

    _relaunch_if_restart(_FakeApp(42))

    assert len(calls) == 1, "code 42 must trigger exactly one os.execv re-exec"
    path, argv = calls[0]
    import sys

    assert path == sys.executable
    assert argv[0] == sys.executable


def test_relaunch_if_restart_noop_on_normal_exit(monkeypatch):
    """A plain quit (code 0 / non-42) must NOT re-exec."""
    calls = []
    monkeypatch.setattr("os.execv", lambda *a: calls.append(a))

    _relaunch_if_restart(_FakeApp(0))
    _relaunch_if_restart(_FakeApp(1))
    _relaunch_if_restart(_FakeApp(None))

    assert calls == [], "only the restart sentinel (42) may re-exec"


@pytest.mark.asyncio
async def test_restart_side_effect_exits_with_code_42():
    """The ``/restart`` command's side-effect must call ``exit(return_code=42)``
    — the sentinel that ``_relaunch_if_restart`` keys on. Spy on ``exit`` so we
    observe the code without actually tearing down the test app."""
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()
        captured = {}

        def _spy_exit(*args, return_code=None, **kwargs):
            captured["return_code"] = return_code

        app.exit = _spy_exit  # type: ignore[assignment]
        app._apply_side_effect("restart_tui")
        await pilot.pause()
        assert captured.get("return_code") == 42


# ─── _agent_name cache must be per-run, not per-app-lifetime ─────────────────


@pytest.mark.asyncio
async def test_agent_name_cache_invalidated_on_new_run():
    """``_agent_name`` is documented 'Cached per run'. A ``kickoff_started``
    event must drop the cache so a roster change between runs is reflected."""
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()

        # Prime the cache directly (simulating a first run that resolved names).
        app._agent_name_cache = {"ag-1": "Cowboy"}
        assert app._agent_name("ag-1") == "Cowboy"

        # A new run begins — the per-run cache must be invalidated.
        app._record_activity_impl(_kickoff_started_event())
        await pilot.pause()

        assert getattr(app, "_agent_name_cache", None) is None, (
            "_agent_name cache must be dropped at kickoff_started so a roster "
            "change between runs is not masked by stale entries"
        )


@pytest.mark.asyncio
async def test_agent_name_rebuilds_after_invalidation():
    """After invalidation, the next ``_agent_name`` lookup rebuilds from the
    current roster rather than serving stale state."""
    from modulatio import roster

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()

        # Stale cache from a prior run.
        app._agent_name_cache = {"ghost": "Stale"}

        # New run starts -> cache cleared.
        app._record_activity_impl(_kickoff_started_event())
        await pilot.pause()

        # The next lookup rebuilds from the (empty) live roster -> the stale
        # entry is gone.
        assert app._agent_name("ghost") == ""
        assert isinstance(roster.list_agents(PROJECT_CODE), list)


# ═══ fold: test_tui_app_resweep.py ═══
# Re-sweep (0.9.0 pre-ship) regression for ``modulatio.tui.app``.
#
# ``_on_kickoff_done`` settled the shared TEAM spinner
# unconditionally from the direct kickoff's OWN result. The engine explicitly
# supports a converse-driven ``run_job`` AND a direct kickoff being live at the
# same time (each on its own worker — see ``_active_job_orchestrators``). When a
# fast kickoff finished while a converse ``run_job`` was still in flight, the
# old code would ``set_done()`` / ``set_error()`` the TEAM spinner, lying about
# the still-running converse job. The fix mirrors the symmetric guard already in
# ``_on_converse_done``: settle the spinner only when ``not _any_job_in_flight()``.






class _FakeConverseOrch:
    """Stand-in for an Orchestrator running a converse-driven ``run_job``.

    ``_kickoff_active`` is the engine's "this orch has a job in flight" signal
    that ``_active_job_orchestrators`` reads."""

    def __init__(self, active: bool) -> None:
        self._kickoff_active = active


_GOOD_RESULT = {
    "mode": "stub",
    "goals": 1,
    "tasks": 1,
    "drafts": 1,
    "blocked_tasks": 0,
    "incomplete_goals": 0,
    "errors": 0,
}


@pytest.mark.asyncio
async def test_kickoff_done_does_not_settle_team_spinner_while_converse_job_in_flight():
    """A direct kickoff finishing must NOT settle the shared TEAM spinner when a
    separate converse-driven ``run_job`` is still live on its own worker — doing
    so would falsely read the running job as done."""
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()

        team = app._lane_status("stream-team-status")
        assert team is not None

        # Put the TEAM spinner into a clearly-running state.
        team.set_activity("decompose_started", "alice")
        assert team._verb is not None and not team._done

        # A converse-driven run_job is still in flight on its own worker.
        app._conv_orch = _FakeConverseOrch(active=True)
        # This kickoff's own orch is already inactive — the engine clears
        # ``_kickoff_active`` in its finally before the worker delivers the
        # result, and ``_kickoff_pending`` is cleared inside _on_kickoff_done.
        app._kickoff_orch = _FakeConverseOrch(active=False)
        app._kickoff_pending = True  # gets cleared by _on_kickoff_done

        app._on_kickoff_done(dict(_GOOD_RESULT), None)
        await pilot.pause()

        # The still-running converse job must keep the spinner running — NOT
        # forced to done/error by the unrelated kickoff's result.
        assert not team._done, (
            "kickoff settled the TEAM spinner to done while a converse run_job "
            "was still in flight"
        )
        assert team._error is None
        assert team._verb is not None


@pytest.mark.asyncio
async def test_kickoff_done_settles_team_spinner_when_nothing_else_running():
    """Sanity: with no other job in flight, a finished kickoff DOES settle the
    TEAM spinner from its own result (the guard only blocks the cross-job lie)."""
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()

        team = app._lane_status("stream-team-status")
        assert team is not None
        team.set_activity("decompose_started", "alice")

        # Nothing else live: no converse orch, no kickoff in startup window.
        app._conv_orch = None
        app._kickoff_orch = _FakeConverseOrch(active=False)
        app._kickoff_pending = True  # cleared by _on_kickoff_done

        app._on_kickoff_done(dict(_GOOD_RESULT), None)
        await pilot.pause()

        assert team._done, "clean kickoff failed to settle the idle TEAM spinner"


@pytest.mark.asyncio
async def test_kickoff_error_does_not_settle_team_spinner_while_other_job_in_flight():
    """The guard covers the error path too: a kickoff erroring out must not flip
    the shared spinner to ✗ while a separate job is still running."""
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()

        team = app._lane_status("stream-team-status")
        assert team is not None
        team.set_activity("decompose_started", "alice")

        app._conv_orch = _FakeConverseOrch(active=True)
        app._kickoff_orch = _FakeConverseOrch(active=False)
        app._kickoff_pending = True

        app._on_kickoff_done(None, RuntimeError("kickoff blew up"))
        await pilot.pause()

        assert team._error is None, (
            "kickoff error flipped the TEAM spinner to ✗ while another job ran"
        )
        assert not team._done
        assert team._verb is not None
