"""Tests for #33 — paper-trail emit sites.

Closes the observability gap surfaced during the 2026-04-27
crypto_advisers re-validation, where mid-run state was invisible to
the user (TUI Status tab silent, no live updates anywhere) for the
~80 minutes of dispatch + execution.

This slice expands ActivityEvent emission to cover every phase
boundary of the kickoff loop: kickoff start/end, Leader decompose
start/end, Coordinator plan start/end per goal, every ticket-open,
and the soft scope-drift warning. Existing six events
(task_dispatched, task_completed, qc_started, qc_verdict,
leader_verify_started, leader_verify_ended) remain.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from modulatio import vault
from modulatio.orchestration import Orchestrator
from modulatio.types import ActivityEvent, Project


PROJECT_CODE = "PTL"


@pytest.fixture
def project(tmp_path: Path, monkeypatch) -> Project:
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    vault.init_project(PROJECT_CODE, "Paper-trail fixture", "obj")
    return Project(
        code=PROJECT_CODE,
        name="Paper-trail fixture",
        objective="obj",
        leader_model="stub",
        wiki_path=str(tmp_path / PROJECT_CODE.lower()),
    )


def _empty_runners(leader_response: str = "```json\n[]\n```"):
    """Stub runners. Default leader returns empty goal list so no
    coordinator/producer phases fire — useful for kickoff-only tests."""
    return {
        "leader": lambda p: leader_response,
        "planner": lambda p: "```json\n[]\n```",
        "drafter": lambda p: "",
        "qc": lambda p: "",
        "researcher": lambda p: "",
    }


def _capturing_orch(project: Project, runners: dict) -> tuple[Orchestrator, list[ActivityEvent]]:
    events: list[ActivityEvent] = []
    orch = Orchestrator(
        project, runners,
        activity_callback=events.append,
    )
    return orch, events


# ─── Kickoff start/end ──────────────────────────────────────────────────────


def test_kickoff_emits_started_event(project: Project):
    """kickoff() fires a kickoff_started event before any other work."""
    orch, events = _capturing_orch(project, _empty_runners())
    orch.kickoff("a tiny objective")
    phases = [e.phase for e in events]
    assert phases[0] == "kickoff_started", f"expected kickoff_started first, got {phases!r}"


def test_kickoff_emits_ended_event(project: Project):
    """kickoff() fires kickoff_ended before returning."""
    orch, events = _capturing_orch(project, _empty_runners())
    orch.kickoff("a tiny objective")
    phases = [e.phase for e in events]
    assert phases[-1] == "kickoff_ended", f"expected kickoff_ended last, got {phases!r}"


# ─── Leader decompose ───────────────────────────────────────────────────────


def test_leader_decompose_emits_start_and_end(project: Project):
    """Leader's goal-decomposition phase is bracketed with start/end
    events so subscribers see when the Leader is thinking."""
    orch, events = _capturing_orch(project, _empty_runners())
    orch.kickoff("objective")
    phases = [e.phase for e in events]
    assert "leader_decompose_started" in phases
    assert "leader_decompose_ended" in phases
    # Order: started before ended.
    start = phases.index("leader_decompose_started")
    end = phases.index("leader_decompose_ended")
    assert start < end


# ─── Coordinator plan (per goal) ────────────────────────────────────────────


def test_coordinator_plan_emits_per_goal(project: Project):
    """Each goal triggers a task_planning_started/ended pair so the
    user can see which goal the Coordinator is working on right now."""
    leader_returns = json.dumps([
        {
            "description": f"Goal {i}",
            "success_criteria": "ok",
            "evidence_required": [],
        }
        for i in (1, 2)
    ])
    runners = _empty_runners(leader_response=f"```json\n{leader_returns}\n```")
    orch, events = _capturing_orch(project, runners)
    orch.kickoff("two-goal objective")
    phases = [e.phase for e in events]
    started_count = phases.count("task_planning_started")
    ended_count = phases.count("task_planning_ended")
    assert started_count == 2
    assert ended_count == 2


# ─── Ticket opened ──────────────────────────────────────────────────────────


def test_ticket_opened_emits_event(project: Project, monkeypatch):
    """Capability tickets are the easiest path to verify ticket_opened
    emission: wire an empty roster + a coordinator that emits a task
    requiring a registered skill, dispatch returns ROSTER_GAP, helper
    opens a ticket and emits the event."""
    leader_returns = json.dumps([
        {
            "description": "g1",
            "success_criteria": "ok",
            "evidence_required": [],
        }
    ])
    coord_returns = json.dumps([
        {
            "description": "t1",
            "assignee_specialist": "drafter",
            "artifact_kind": "research",
            "required_skills": ["nonexistent_skill"],
            "evidence_required": [],
        }
    ])
    runners = {
        "leader": lambda p: f"```json\n{leader_returns}\n```",
        "planner": lambda p: f"```json\n{coord_returns}\n```",
        "drafter": lambda p: "",
        "qc": lambda p: "",
        "researcher": lambda p: "",
    }
    orch, events = _capturing_orch(project, runners)
    orch.kickoff("any")
    phases = [e.phase for e in events]
    assert "ticket_opened" in phases


# ─── Scope-drift warning ────────────────────────────────────────────────────


def test_scope_drift_warning_emits_event(project: Project):
    """When the soft scope-drift check fires, an event is also emitted
    so live subscribers (TUI banner, Telegram) see the warning beyond
    just the post-run summary.errors."""
    leader_returns = json.dumps([
        {"description": f"Goal {i}", "success_criteria": "ok", "evidence_required": []}
        for i in range(1, 8)  # 7 goals → triggers warning
    ])
    runners = _empty_runners(leader_response=f"```json\n{leader_returns}\n```")
    orch, events = _capturing_orch(project, runners)
    orch.kickoff("short objective")
    phases = [e.phase for e in events]
    assert "scope_drift_warning" in phases
