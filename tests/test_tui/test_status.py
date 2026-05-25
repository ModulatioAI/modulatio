"""Tests for slice #21 — Status tab + activity log widget.

Consumes slice #17's ``activity_callback``. The Status tab renders a
team-aggregate activity log plus per-role panels (leader, coordinator,
drafter, qc, researcher). During a stub kickoff, events from the six
orchestrator phase transitions flow into the corresponding widgets.

Widget design: ``ActivityLog`` is a ``Static`` with a backing ``events``
list + an optional ``filter_role``. Tests assert on ``events`` directly
rather than on rendered text — the widget's truth is the list; the
rendered string is best-effort.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from modulatio import vault
from modulatio.types import ActivityEvent


# ─── Fixture: isolate vault ─────────────────────────────────────────────────


@pytest.fixture
def tui_vault(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    return tmp_path


# ─── ActivityLog widget (unit-ish — constructs + stores events) ─────────────


def test_activity_log_stores_events_in_append_order():
    """Backing list is the source of truth for tests + later widgets.
    Append-order preserved so chronology is reconstructable."""
    from datetime import datetime

    from modulatio.tui.widgets.activity_log import ActivityLog

    log = ActivityLog()
    e1 = ActivityEvent(
        agent_id="producer", role="drafter", phase="task_dispatched",
        task_id="T-1", timestamp=datetime(2026, 4, 21, 19, 0, 0),
    )
    e2 = ActivityEvent(
        agent_id="qc", role="qc", phase="qc_started",
        task_id="T-1", timestamp=datetime(2026, 4, 21, 19, 0, 5),
    )
    log.add_event(e1)
    log.add_event(e2)

    assert log.events == [e1, e2]


def test_activity_log_filter_role_retains_only_matching_events():
    """A role-filtered log ignores events for other roles. This lets the
    Status tab show each role in its own panel using the same widget class."""
    from datetime import datetime

    from modulatio.tui.widgets.activity_log import ActivityLog

    log = ActivityLog(filter_role="qc")
    leader_e = ActivityEvent(
        agent_id="leader", role="leader", phase="leader_verify_started",
        task_id=None, timestamp=datetime(2026, 4, 21, 19, 0, 0),
    )
    qc_e = ActivityEvent(
        agent_id="qc", role="qc", phase="qc_verdict",
        task_id="T-1", timestamp=datetime(2026, 4, 21, 19, 0, 5),
    )
    log.add_event(leader_e)
    log.add_event(qc_e)

    assert log.events == [qc_e]  # leader filtered out


# ─── Status tab replaces placeholder ────────────────────────────────────────


async def test_status_tab_is_no_longer_a_placeholder(tui_vault):
    """Slice #20 rendered a 'coming in slice #21' placeholder under
    tab-status. This slice replaces it with the real tab containing the
    team-aggregate log + 5 per-role logs."""
    from modulatio.tui.app import ModulatioApp
    from modulatio.tui.widgets.activity_log import ActivityLog

    app = ModulatioApp(project_code="TST", stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        # Team log exists.
        assert app.query_one("#status-team-log", ActivityLog) is not None
        # Per-role logs exist for the 5 default roles. Step 0 round-2
        # M2 (Lovecraft audit, 2026-05-16): "coordinator" was swapped
        # for "planner" — the orchestrator no longer emits
        # role="coordinator" events, so the old row was always empty.
        for role in ("leader", "planner", "drafter", "qc", "researcher"):
            assert app.query_one(f"#status-role-{role}", ActivityLog) is not None


# ─── Stub kickoff populates Status widgets ──────────────────────────────────


async def test_stub_kickoff_populates_team_log_with_six_phase_events(tui_vault):
    """End-to-end wire: ModulatioApp creates a callback that routes events
    into the team log + the matching per-role log. A stub kickoff exercises
    all 6 phases (task_dispatched, qc_started, qc_verdict, task_completed,
    leader_verify_started, leader_verify_ended), all of which land on the
    team log."""
    from textual.widgets import TextArea

    from modulatio.tui.app import ModulatioApp
    from modulatio.tui.widgets.activity_log import ActivityLog

    app = ModulatioApp(project_code="TST", stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one("#prompt-input", TextArea).text = "stub objective"
        await pilot.click("#prompt-kickoff")
        await pilot.pause()

        team_log = app.query_one("#status-team-log", ActivityLog)
        phases_seen = {e.phase for e in team_log.events}
    expected = {
        "task_dispatched",
        "task_completed",
        "qc_started",
        "qc_verdict",
        "leader_verify_started",
        "leader_verify_ended",
    }
    missing = expected - phases_seen
    assert not missing, f"missing phases in team log: {missing}; saw: {phases_seen}"


async def test_stub_kickoff_routes_events_to_matching_role_log(tui_vault):
    """Leader events only land on the leader role log; qc events only
    on the qc role log; drafter events only on the drafter role log.
    Events for a role whose panel doesn't exist are silently dropped."""
    from textual.widgets import TextArea

    from modulatio.tui.app import ModulatioApp
    from modulatio.tui.widgets.activity_log import ActivityLog

    app = ModulatioApp(project_code="TST", stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one("#prompt-input", TextArea).text = "stub objective"
        await pilot.click("#prompt-kickoff")
        await pilot.pause()

        leader_events = list(
            app.query_one("#status-role-leader", ActivityLog).events
        )
        qc_events = list(
            app.query_one("#status-role-qc", ActivityLog).events
        )
        drafter_events = list(
            app.query_one("#status-role-drafter", ActivityLog).events
        )

    assert len(leader_events) >= 2
    assert all(e.role == "leader" for e in leader_events)

    assert len(qc_events) >= 2
    assert all(e.role == "qc" for e in qc_events)

    assert len(drafter_events) >= 2
    assert all(e.role == "drafter" for e in drafter_events)


async def test_activity_log_renders_idle_marker_when_empty(tui_vault):
    """Before any kickoff, each role panel renders an idle placeholder so
    the user sees 'nothing happening' rather than a blank rectangle."""
    from modulatio.tui.app import ModulatioApp
    from modulatio.tui.widgets.activity_log import ActivityLog

    app = ModulatioApp(project_code="TST", stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        leader_log = app.query_one("#status-role-leader", ActivityLog)
        # No kickoff yet → the widget text says idle. Static exposes its
        # rendered content via the private _content attribute; the public
        # contract is just that something user-readable signals idleness.
        rendered = str(leader_log.render()).lower()
        assert "idle" in rendered
