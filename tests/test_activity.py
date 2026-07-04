"""Tests for slice #17 — Activity event emission.

Orchestrator emits ActivityEvent records at 6 phase transitions via an
optional ``activity_callback`` kwarg. CLI doesn't supply one (back-compat
preserved). TUI (slice #21) will subscribe.

Scope discipline: additive only. Omitting the callback preserves every
pre-#17 behavior. The 6 phase names are load-bearing — TUI widgets
will key off them.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from modulatio import vault
from modulatio.orchestration import Orchestrator
from modulatio.types import (
    ActivityEvent,
    Project,
)


PROJECT_CODE = "ACT"


@pytest.fixture
def project(tmp_path: Path, monkeypatch) -> Project:
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    vault.init_project(PROJECT_CODE, "Activity fixture", "one piece")
    return Project(
        code=PROJECT_CODE,
        name="Activity fixture",
        objective="one piece",
        leader_model="stub",
        wiki_path=str(tmp_path / PROJECT_CODE.lower()),
    )


def _leader_stub(prompt: str) -> str:
    if "LEADER GOAL VERIFICATION" in prompt:
        payload = {
            "verdict": "satisfied",
            "rationale": "stub",
            "report_body": "## Report\n\nBody.\n",
        }
        return f"```json\n{json.dumps(payload)}\n```"
    goals = [{
        "description": "Draft 1 piece",
        "success_criteria": "1 file",
        "evidence_required": [{"kind": "artifact", "description": "file exists"}],
    }]
    return f"```json\n{json.dumps(goals)}\n```"


def _planner_stub(prompt: str) -> str:
    tasks = [{
        "description": "Draft",
        "assignee_specialist": "drafter",
        "artifact_kind": "text",
        "evidence_required": [{"kind": "artifact", "description": "file exists"}],
    }]
    return f"```json\n{json.dumps(tasks)}\n```"


def _drafter_stub(prompt: str) -> str:
    return "Body.\n"


def _qc_stub(prompt: str) -> str:
    verdict = {"check": "present", "passed": True, "notes": "", "defect_type": None}
    return f"```json\n{json.dumps(verdict)}\n```"


# ─── Unit tests: ActivityEvent shape ────────────────────────────────────────

def test_activity_event_has_required_fields():
    """Baseline: ActivityEvent is constructable with the 5 fields the
    TUI keys off — agent_id, role, phase, task_id (optional), timestamp."""
    e = ActivityEvent(
        agent_id="producer",
        role="drafter",
        phase="task_dispatched",
        task_id="ACT-T-001",
        timestamp=datetime(2026, 4, 21, 19, 0, 0),
    )
    assert e.agent_id == "producer"
    assert e.role == "drafter"
    assert e.phase == "task_dispatched"
    assert e.task_id == "ACT-T-001"
    assert e.timestamp.year == 2026


def test_activity_event_allows_none_task_id():
    """Leader-verify events are goal-level; task_id=None is valid."""
    e = ActivityEvent(
        agent_id="leader",
        role="leader",
        phase="leader_verify_started",
        task_id=None,
        timestamp=datetime(2026, 4, 21, 19, 0, 0),
    )
    assert e.task_id is None


# ─── Integration: back-compat + callback wiring ─────────────────────────────

def test_omitting_callback_preserves_existing_behavior(project: Project):
    """The canonical back-compat guarantee: no callback = zero behavior
    change. If this breaks, slice #17 is breaking every pre-#17 caller."""
    runners = {
        "leader": _leader_stub,
        "planner": _planner_stub,
        "drafter": _drafter_stub,
        "qc": _qc_stub,
    }
    orch = Orchestrator(project, runners)
    summary = orch.kickoff("one piece")
    assert summary is not None
    # And nothing raised. That's the whole point.


def test_callback_fires_for_all_six_phases_in_stub_run(project: Project):
    """A single-task stub kickoff emits at least one event per phase:
    task_dispatched, task_completed, qc_started, qc_verdict,
    leader_verify_started, leader_verify_ended."""
    events: list[ActivityEvent] = []
    runners = {
        "leader": _leader_stub,
        "planner": _planner_stub,
        "drafter": _drafter_stub,
        "qc": _qc_stub,
    }
    orch = Orchestrator(project, runners, activity_callback=events.append)
    orch.kickoff("one piece")

    phases_seen = {e.phase for e in events}
    expected = {
        "task_dispatched",
        "task_completed",
        "qc_started",
        "qc_verdict",
        "leader_verify_started",
        "leader_verify_ended",
    }
    missing = expected - phases_seen
    assert not missing, f"missing phases: {missing}; saw: {phases_seen}"


def test_task_events_carry_task_id(project: Project):
    """task_dispatched / task_completed must carry the task id so TUI
    widgets can correlate per-task activity in the Status tab."""
    events: list[ActivityEvent] = []
    runners = {
        "leader": _leader_stub,
        "planner": _planner_stub,
        "drafter": _drafter_stub,
        "qc": _qc_stub,
    }
    orch = Orchestrator(project, runners, activity_callback=events.append)
    orch.kickoff("one piece")

    task_events = [e for e in events if e.phase in ("task_dispatched", "task_completed")]
    assert len(task_events) >= 2
    for e in task_events:
        assert e.task_id is not None
        assert e.task_id.startswith(PROJECT_CODE.upper())


def test_leader_verify_events_have_no_task_id_and_leader_role(project: Project):
    """Leader-verify is goal-level. task_id stays None; role stays 'leader'.
    TUI uses this to pin events to the team-aggregate panel rather than
    a per-task row."""
    events: list[ActivityEvent] = []
    runners = {
        "leader": _leader_stub,
        "planner": _planner_stub,
        "drafter": _drafter_stub,
        "qc": _qc_stub,
    }
    orch = Orchestrator(project, runners, activity_callback=events.append)
    orch.kickoff("one piece")

    leader_events = [e for e in events if e.phase.startswith("leader_verify_")]
    assert len(leader_events) >= 2
    for e in leader_events:
        assert e.task_id is None
        assert e.role == "leader"


def test_qc_events_carry_task_id_and_qc_role(project: Project):
    """QC events are per-task; role is 'qc' so TUI styles the qc agent's
    panel correctly."""
    events: list[ActivityEvent] = []
    runners = {
        "leader": _leader_stub,
        "planner": _planner_stub,
        "drafter": _drafter_stub,
        "qc": _qc_stub,
    }
    orch = Orchestrator(project, runners, activity_callback=events.append)
    orch.kickoff("one piece")

    qc_events = [e for e in events if e.phase.startswith("qc_")]
    assert len(qc_events) >= 2
    for e in qc_events:
        assert e.task_id is not None
        assert e.role == "qc"


def test_callback_events_are_time_ordered(project: Project):
    """Events arrive in monotonic timestamp order so the TUI activity
    log can render them chronologically without sorting."""
    events: list[ActivityEvent] = []
    runners = {
        "leader": _leader_stub,
        "planner": _planner_stub,
        "drafter": _drafter_stub,
        "qc": _qc_stub,
    }
    orch = Orchestrator(project, runners, activity_callback=events.append)
    orch.kickoff("one piece")

    timestamps = [e.timestamp for e in events]
    assert timestamps == sorted(timestamps), (
        "events should be emitted in timestamp-monotonic order"
    )


def test_qc_verdict_carries_passed_detail(project: Project):
    """The qc_verdict event NAMES its outcome — ``detail={"passed": bool}`` —
    so the TUI telemetry rail can tally ✓/✗ live off the feed (the run-scope
    task files don't reliably carry terminal state mid-run)."""
    events: list[ActivityEvent] = []
    runners = {
        "leader": _leader_stub,
        "planner": _planner_stub,
        "drafter": _drafter_stub,
        "qc": _qc_stub,
    }
    orch = Orchestrator(project, runners, activity_callback=events.append)
    orch.kickoff("one piece")

    verdicts = [e for e in events if e.phase == "qc_verdict"]
    assert verdicts
    for e in verdicts:
        assert isinstance(e.detail, dict), f"qc_verdict without detail: {e}"
        assert isinstance(e.detail.get("passed"), bool)
