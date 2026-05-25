"""Tests for slice #16 — Ticket approval fields + Leader prior-approval context.

Three orthogonal approval fields on ``Ticket`` let Leader request binary
human decisions (budget gates, output-quality uncertainty, etc.) without
expanding ticket semantics beyond notifications. Leader's verify prompt
surfaces prior approval decisions so the same question isn't asked twice.

Scope discipline (per user direction): basic, no extra code. Only the
three fields + create/update store helpers + one prompt slot. No approval
chains, no timeouts, no multi-approver flows.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from modulatio import store, vault
from modulatio.orchestration import Orchestrator
from modulatio.types import (
    GoalStatus,
    Project,
    TicketPriority,
    TicketStatus,
)


PROJECT_CODE = "APR"


@pytest.fixture
def project(tmp_path: Path, monkeypatch) -> Project:
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    vault.init_project(PROJECT_CODE, "Approval fixture", "draft 1")
    return Project(
        code=PROJECT_CODE,
        name="Approval fixture",
        objective="draft 1",
        leader_model="stub",
        wiki_path=str(tmp_path / PROJECT_CODE.lower()),
    )


def _leader_with_verdict(verdict: str):
    """Stub leader runner: decompose once, then return the given verify verdict."""
    def _runner(prompt: str) -> str:
        if "LEADER GOAL VERIFICATION" in prompt:
            payload = {
                "verdict": verdict,
                "rationale": f"stub {verdict}",
                "report_body": f"## Goal Report\n\nStub {verdict} body.\n",
            }
            return f"```json\n{json.dumps(payload)}\n```"
        goals = [{
            "description": "Draft 1 piece",
            "success_criteria": "1 file exists",
            "evidence_required": [{"kind": "artifact", "description": "file exists"}],
        }]
        return f"```json\n{json.dumps(goals)}\n```"
    return _runner


def _coordinator_stub(prompt: str) -> str:
    tasks = [{
        "description": "Draft task",
        "assignee_specialist": "drafter",
        "artifact_kind": "text",
        "evidence_required": [{"kind": "artifact", "description": "file exists"}],
    }]
    return f"```json\n{json.dumps(tasks)}\n```"


def _drafter_stub(prompt: str) -> str:
    return "Stub artifact body — offline test.\n"


def _qc_stub(prompt: str) -> str:
    verdict = {"check": "artifact present", "passed": True, "notes": "", "defect_type": None}
    return f"```json\n{json.dumps(verdict)}\n```"


# ─── Unit tests: field defaults + round-trip ────────────────────────────────

def test_ticket_defaults_have_approval_fields_off(project: Project):
    """Baseline: new tickets carry approval_required=False, decision=None,
    decided_by=None. Preserves every pre-#16 caller's behavior."""
    t = store.create_ticket(
        project_id=project.id,
        project_code=project.code,
        priority=TicketPriority.MINOR,
        title="plain notification",
        body="nothing to approve",
    )
    assert t.approval_required is False
    assert t.approval_decision is None
    assert t.approval_decided_by is None


def test_create_ticket_accepts_approval_required_kwarg(project: Project):
    """Orchestrator-side callers opt in by passing approval_required=True
    at creation time. The helper wires the flag onto the stored ticket."""
    t = store.create_ticket(
        project_id=project.id,
        project_code=project.code,
        priority=TicketPriority.CRITICAL,
        title="Budget at 80% — continue?",
        body="Approval request body.",
        approval_required=True,
    )
    assert t.approval_required is True
    assert t.approval_decision is None
    assert t.approval_decided_by is None


def test_update_ticket_approval_persists_decision(project: Project):
    """User records a decision via ``update_ticket_approval``; the vault
    reflects it on next read. This is the TUI-facing write path."""
    t = store.create_ticket(
        project_id=project.id,
        project_code=project.code,
        priority=TicketPriority.CRITICAL,
        title="Output quality uncertain — acceptable?",
        body="",
        approval_required=True,
    )

    store.update_ticket_approval(
        project.code,
        t.id,
        decision="approved",
        decided_by="user",
    )

    loaded = store.list_tickets(project.code)
    assert len(loaded) == 1
    assert loaded[0].approval_required is True
    assert loaded[0].approval_decision == "approved"
    assert loaded[0].approval_decided_by == "user"


def test_update_ticket_approval_persists_note(project: Project):
    """Optional decision note is stored on the ticket so the team can
    see *why* a decision was made, not just the verdict."""
    t = store.create_ticket(
        project_id=project.id,
        project_code=project.code,
        priority=TicketPriority.CRITICAL,
        title="Continue?",
        body="",
        approval_required=True,
    )
    store.update_ticket_approval(
        project.code, t.id,
        decision="approved", decided_by="user",
        note="T-004 ships as draft; schema is correct.",
    )
    loaded = store.get_ticket(project.code, t.id)
    assert loaded is not None
    assert loaded.approval_note == "T-004 ships as draft; schema is correct."


def test_update_ticket_approval_stamps_decided_at(project: Project):
    """approval_decided_at is set to now() when the decision is recorded."""
    from datetime import datetime, timezone

    t = store.create_ticket(
        project_id=project.id,
        project_code=project.code,
        priority=TicketPriority.CRITICAL,
        title="Continue?",
        body="",
        approval_required=True,
    )
    before = datetime.now(timezone.utc)
    store.update_ticket_approval(
        project.code, t.id, decision="approved", decided_by="user",
    )
    after = datetime.now(timezone.utc)
    loaded = store.get_ticket(project.code, t.id)
    assert loaded is not None
    assert loaded.approval_decided_at is not None
    assert before <= loaded.approval_decided_at <= after


def test_update_ticket_approval_transitions_status_to_resolved(project: Project):
    """A recorded human decision is a terminal event: OPEN → RESOLVED.
    The transition log captures it so the team has audit trail."""
    t = store.create_ticket(
        project_id=project.id,
        project_code=project.code,
        priority=TicketPriority.CRITICAL,
        title="Continue?",
        body="",
        approval_required=True,
    )
    assert t.status == TicketStatus.OPEN  # pre-condition

    store.update_ticket_approval(
        project.code, t.id,
        decision="approved", decided_by="user",
        note="bounded scope; ship it",
    )

    loaded = store.get_ticket(project.code, t.id)
    assert loaded is not None
    assert loaded.status == TicketStatus.RESOLVED
    last = loaded.transitions[-1]
    assert last.from_state == TicketStatus.OPEN.value
    assert last.to_state == TicketStatus.RESOLVED.value
    assert "approved" in last.rationale
    assert "bounded scope" in last.rationale


# ─── Decline reopens affected goal/task (step 4 — redo context) ─────────────


def _create_goal(project: Project, status: GoalStatus = GoalStatus.IN_PROGRESS) -> "Goal":  # noqa: F821
    from modulatio.types import Goal
    g = Goal(
        id=f"{project.code}-G-001",
        project_id=project.id,
        description="A goal under scrutiny",
        success_criteria="works",
        status=status,
    )
    store.save_goal(project.code, g)
    return g


def _create_task(project: Project, goal_id: str, status=None) -> "Task":  # noqa: F821
    from modulatio.types import Task, TaskStatus
    t = Task(
        id=f"{project.code}-T-001",
        project_id=project.id,
        goal_id=goal_id,
        description="A task under scrutiny",
        status=status or TaskStatus.QC_REJECTED,
    )
    store.save_task(project.code, t)
    return t


def test_decline_reopens_affected_goal(project: Project):
    """Declining a ticket flips its affected goal back to IN_PROGRESS so
    the next Leader run has a concrete redo target. The decline note
    becomes the redo rationale on the goal's transition log."""
    goal = _create_goal(project, status=GoalStatus.IN_PROGRESS)
    ticket = store.create_ticket(
        project_id=project.id,
        project_code=project.code,
        priority=TicketPriority.CRITICAL,
        title="Goal completion uncertain",
        body="",
        approval_required=True,
        affected_goal_id=goal.id,
    )
    store.update_ticket_approval(
        project.code, ticket.id,
        decision="denied", decided_by="user",
        note="missing T-004 deliverable; redo with explicit DB init",
    )

    reopened = store.get_goal(project.code, goal.id)
    assert reopened is not None
    assert reopened.status == GoalStatus.IN_PROGRESS
    last = reopened.transitions[-1]
    assert last.to_state == GoalStatus.IN_PROGRESS.value
    assert last.actor == "user"
    assert "missing T-004 deliverable" in last.rationale
    assert ticket.id in last.rationale  # cross-link back to the ticket


def test_decline_reopens_affected_task(project: Project):
    """Symmetric to goal: an affected_task_id on a denied ticket flips
    the task back to PENDING with the note in the transition log."""
    from modulatio.types import TaskStatus
    goal = _create_goal(project)
    task = _create_task(project, goal.id, status=TaskStatus.QC_REJECTED)
    ticket = store.create_ticket(
        project_id=project.id,
        project_code=project.code,
        priority=TicketPriority.CRITICAL,
        title="Task verdict uncertain",
        body="",
        approval_required=True,
        affected_task_id=task.id,
    )
    store.update_ticket_approval(
        project.code, ticket.id,
        decision="denied", decided_by="user",
        note="output is fine but scope is wrong; redo from spec",
    )

    reopened = store.get_task(project.code, task.id)
    assert reopened is not None
    assert reopened.status == TaskStatus.PENDING
    last = reopened.transitions[-1]
    assert last.to_state == TaskStatus.PENDING.value
    assert "scope is wrong" in last.rationale


def test_approve_does_not_reopen_affected_goal(project: Project):
    """Only decline reopens. Approve leaves the goal alone (it's the
    user agreeing the goal is satisfied)."""
    goal = _create_goal(project, status=GoalStatus.COMPLETED)
    ticket = store.create_ticket(
        project_id=project.id,
        project_code=project.code,
        priority=TicketPriority.CRITICAL,
        title="Goal completion uncertain",
        body="",
        approval_required=True,
        affected_goal_id=goal.id,
    )
    store.update_ticket_approval(
        project.code, ticket.id,
        decision="approved", decided_by="user",
    )
    unchanged = store.get_goal(project.code, goal.id)
    assert unchanged is not None
    assert unchanged.status == GoalStatus.COMPLETED  # untouched
    # No "redo" transition added on approve
    rationales = [t.rationale for t in unchanged.transitions]
    assert not any("redo" in r.lower() for r in rationales)


def test_decline_with_no_affected_entity_is_noop(project: Project):
    """Decline on a ticket without affected_goal_id/affected_task_id
    just records the decision — no goal/task to reopen."""
    ticket = store.create_ticket(
        project_id=project.id,
        project_code=project.code,
        priority=TicketPriority.CRITICAL,
        title="Generic concern",
        body="",
        approval_required=True,
    )
    # Should not raise.
    store.update_ticket_approval(
        project.code, ticket.id,
        decision="denied", decided_by="user",
        note="not actionable yet",
    )
    loaded = store.get_ticket(project.code, ticket.id)
    assert loaded.approval_decision == "denied"


def test_decline_does_not_reopen_abandoned_goal(project: Project):
    """ABANDONED is terminal; decline leaves it alone (avoids zombie
    revival of explicitly-killed goals)."""
    goal = _create_goal(project, status=GoalStatus.ABANDONED)
    ticket = store.create_ticket(
        project_id=project.id,
        project_code=project.code,
        priority=TicketPriority.CRITICAL,
        title="Concerns about an abandoned goal",
        body="",
        approval_required=True,
        affected_goal_id=goal.id,
    )
    store.update_ticket_approval(
        project.code, ticket.id,
        decision="denied", decided_by="user",
        note="leave it dead",
    )
    unchanged = store.get_goal(project.code, goal.id)
    assert unchanged is not None
    assert unchanged.status == GoalStatus.ABANDONED  # still terminal


def test_update_ticket_approval_supports_denied_decision(project: Project):
    """'denied' is symmetric with 'approved'; both persist with decider."""
    t = store.create_ticket(
        project_id=project.id,
        project_code=project.code,
        priority=TicketPriority.CRITICAL,
        title="Escalate to premium model?",
        body="",
        approval_required=True,
    )
    store.update_ticket_approval(project.code, t.id, decision="denied", decided_by="user")

    loaded = store.list_tickets(project.code)
    assert loaded[0].approval_decision == "denied"
    assert loaded[0].approval_decided_by == "user"


# ─── Step 5: orchestrator polling — drain decided tickets ──────────────────


def test_drain_marks_goal_completed_on_approve(project: Project):
    """Approve decision on a CRITICAL ticket → drain marks affected goal
    COMPLETED with the note in the transition log, ticket closes."""
    from modulatio.orchestration import Orchestrator, RunSummary

    goal = _create_goal(project, status=GoalStatus.IN_PROGRESS)
    ticket = store.create_ticket(
        project_id=project.id,
        project_code=project.code,
        priority=TicketPriority.CRITICAL,
        title="Goal completion uncertain",
        body="",
        approval_required=True,
        affected_goal_id=goal.id,
    )
    store.update_ticket_approval(
        project.code, ticket.id,
        decision="approved", decided_by="user",
        note="ship it; bounded scope",
    )

    orch = Orchestrator(project, runners={})
    summary = RunSummary(project=project)
    summary.goals.append(goal)

    orch._drain_decided_tickets(summary)

    reopened_goal = store.get_goal(project.code, goal.id)
    assert reopened_goal is not None
    assert reopened_goal.status == GoalStatus.COMPLETED
    last = reopened_goal.transitions[-1]
    assert last.to_state == GoalStatus.COMPLETED.value
    assert last.actor == "user"
    assert "ship it" in last.rationale
    assert ticket.id in last.rationale

    closed_ticket = store.get_ticket(project.code, ticket.id)
    assert closed_ticket is not None
    assert closed_ticket.status == TicketStatus.CLOSED


def test_drain_closes_ticket_on_decline(project: Project):
    """Decline decision → drain closes ticket. Goal/task already in
    redo-ready state from step 4; drain doesn't change their status."""
    from modulatio.orchestration import Orchestrator, RunSummary

    goal = _create_goal(project, status=GoalStatus.IN_PROGRESS)
    ticket = store.create_ticket(
        project_id=project.id,
        project_code=project.code,
        priority=TicketPriority.CRITICAL,
        title="Goal completion uncertain",
        body="",
        approval_required=True,
        affected_goal_id=goal.id,
    )
    store.update_ticket_approval(
        project.code, ticket.id,
        decision="denied", decided_by="user",
        note="missing T-004 deliverable",
    )
    # Step 4 already flipped goal back to IN_PROGRESS by the time drain runs.

    orch = Orchestrator(project, runners={})
    summary = RunSummary(project=project)
    orch._drain_decided_tickets(summary)

    closed_ticket = store.get_ticket(project.code, ticket.id)
    assert closed_ticket.status == TicketStatus.CLOSED

    # Goal should remain in the redo-ready state set by step 4.
    goal_after = store.get_goal(project.code, goal.id)
    assert goal_after.status == GoalStatus.IN_PROGRESS


def test_drain_is_idempotent(project: Project):
    """Calling drain twice doesn't double-process: the ticket is already
    CLOSED after the first call, so the second call skips it."""
    from modulatio.orchestration import Orchestrator, RunSummary

    goal = _create_goal(project, status=GoalStatus.IN_PROGRESS)
    ticket = store.create_ticket(
        project_id=project.id,
        project_code=project.code,
        priority=TicketPriority.CRITICAL,
        title="Goal completion uncertain",
        body="",
        approval_required=True,
        affected_goal_id=goal.id,
    )
    store.update_ticket_approval(
        project.code, ticket.id,
        decision="approved", decided_by="user",
    )

    orch = Orchestrator(project, runners={})
    summary = RunSummary(project=project)
    orch._drain_decided_tickets(summary)
    first_transition_count = len(store.get_goal(project.code, goal.id).transitions)
    orch._drain_decided_tickets(summary)
    second_transition_count = len(store.get_goal(project.code, goal.id).transitions)

    assert first_transition_count == second_transition_count


def test_drain_skips_undecided_resolved_tickets(project: Project):
    """A RESOLVED ticket without an approval_decision (e.g. auto-resumed
    budget ticket from slice #7e) is not an approval event — drain
    leaves it alone."""
    from modulatio.orchestration import Orchestrator, RunSummary

    goal = _create_goal(project, status=GoalStatus.IN_PROGRESS)
    ticket = store.create_ticket(
        project_id=project.id,
        project_code=project.code,
        priority=TicketPriority.BLOCKER,
        title="Auto-resumed budget ticket",
        body="",
        approval_required=False,
        affected_goal_id=goal.id,
    )
    # Manually transition to RESOLVED without recording a decision (mimics
    # what auto_resume does on slice #7e).
    store.update_ticket_status(
        project.code, ticket.id,
        TicketStatus.RESOLVED,
        actor="leader", rationale="auto-resumed",
    )

    orch = Orchestrator(project, runners={})
    summary = RunSummary(project=project)
    orch._drain_decided_tickets(summary)

    untouched_goal = store.get_goal(project.code, goal.id)
    assert untouched_goal.status == GoalStatus.IN_PROGRESS  # not auto-completed
    after = store.get_ticket(project.code, ticket.id)
    assert after.status == TicketStatus.RESOLVED  # not closed


def test_drain_does_not_auto_complete_goal_for_notification_ticket(project: Project):
    """Approval on a NOTIFICATION ticket (approval_required=False) is
    the user acknowledging, not the user being the verdict — the
    affected goal must NOT auto-flip to COMPLETED. Surfaced 2026-04-28
    in the WLT run where approving a retry-budget notification
    incorrectly closed the goal."""
    from modulatio.orchestration import Orchestrator, RunSummary

    goal = _create_goal(project, status=GoalStatus.IN_PROGRESS)
    ticket = store.create_ticket(
        project_id=project.id,
        project_code=project.code,
        priority=TicketPriority.BLOCKER,
        title="Retry budget exhausted (notification)",
        body="",
        approval_required=False,  # ← notification, not approval-gated
        affected_goal_id=goal.id,
    )
    store.update_ticket_approval(
        project.code, ticket.id, decision="approved", decided_by="user",
    )

    orch = Orchestrator(project, runners={})
    summary = RunSummary(project=project)
    orch._drain_decided_tickets(summary)

    untouched_goal = store.get_goal(project.code, goal.id)
    assert untouched_goal.status == GoalStatus.IN_PROGRESS, (
        "notification ticket approval should NOT mark the goal "
        "completed — that's the user-as-verdict semantic for "
        "approval_required=True tickets only"
    )
    # Ticket still gets closed (idempotent processing).
    closed = store.get_ticket(project.code, ticket.id)
    assert closed.status == TicketStatus.CLOSED


def test_drain_appends_goal_to_summary_if_missing(project: Project):
    """Approved tickets on goals not yet in summary.goals — e.g. a
    between-runs decision on an old goal — get added so the run summary
    reflects the closure."""
    from modulatio.orchestration import Orchestrator, RunSummary

    goal = _create_goal(project, status=GoalStatus.IN_PROGRESS)
    ticket = store.create_ticket(
        project_id=project.id,
        project_code=project.code,
        priority=TicketPriority.CRITICAL,
        title="Goal completion uncertain",
        body="",
        approval_required=True,
        affected_goal_id=goal.id,
    )
    store.update_ticket_approval(
        project.code, ticket.id, decision="approved", decided_by="user",
    )

    orch = Orchestrator(project, runners={})
    summary = RunSummary(project=project)  # summary.goals starts empty
    orch._drain_decided_tickets(summary)

    assert any(g.id == goal.id for g in summary.goals)


# ─── Step 6: drain returns redo goals + _reexecute_goal ────────────────────


def test_drain_returns_goal_id_for_denied_with_affected_goal(project: Project):
    """A RESOLVED denied ticket with affected_goal_id puts the goal in
    drain's returned redo set so the kickoff wind-down loop can
    re-execute it."""
    from modulatio.orchestration import Orchestrator, RunSummary

    goal = _create_goal(project, status=GoalStatus.IN_PROGRESS)
    ticket = store.create_ticket(
        project_id=project.id,
        project_code=project.code,
        priority=TicketPriority.CRITICAL,
        title="Goal completion uncertain",
        body="",
        approval_required=True,
        affected_goal_id=goal.id,
    )
    store.update_ticket_approval(
        project.code, ticket.id, decision="denied", decided_by="user",
        note="redo with constraints",
    )

    orch = Orchestrator(project, runners={})
    redo = orch._drain_decided_tickets(RunSummary(project=project))
    assert goal.id in redo


def test_drain_returns_parent_goal_id_for_denied_with_affected_task_only(project: Project):
    """When a ticket only references affected_task_id, drain looks up
    the task's parent goal so the wind-down loop can re-execute the
    whole goal pipeline (re-verify after task redo)."""
    from modulatio.orchestration import Orchestrator, RunSummary
    from modulatio.types import TaskStatus

    goal = _create_goal(project)
    task = _create_task(project, goal.id, status=TaskStatus.QC_REJECTED)
    ticket = store.create_ticket(
        project_id=project.id,
        project_code=project.code,
        priority=TicketPriority.CRITICAL,
        title="Task verdict uncertain",
        body="",
        approval_required=True,
        affected_task_id=task.id,
    )
    store.update_ticket_approval(
        project.code, ticket.id, decision="denied", decided_by="user",
    )

    orch = Orchestrator(project, runners={})
    redo = orch._drain_decided_tickets(RunSummary(project=project))
    assert goal.id in redo


def test_drain_returns_empty_set_when_only_approvals(project: Project):
    """Approve decisions don't request redo — they close the goal in
    place. Drain's returned set covers decline cases only."""
    from modulatio.orchestration import Orchestrator, RunSummary

    goal = _create_goal(project)
    ticket = store.create_ticket(
        project_id=project.id,
        project_code=project.code,
        priority=TicketPriority.CRITICAL,
        title="Goal completion uncertain",
        body="",
        approval_required=True,
        affected_goal_id=goal.id,
    )
    store.update_ticket_approval(
        project.code, ticket.id, decision="approved", decided_by="user",
    )

    orch = Orchestrator(project, runners={})
    redo = orch._drain_decided_tickets(RunSummary(project=project))
    assert redo == set()


def test_reexecute_goal_runs_each_pending_task(project: Project, monkeypatch):
    """Step 4 reopened tasks land in PENDING. _reexecute_goal flips them
    to DISPATCHED and routes through _run_task_with_redo so the existing
    producer + QC + redo machinery handles re-execution."""
    from modulatio.orchestration import Orchestrator, RunSummary
    from modulatio.types import Task, TaskStatus

    goal = _create_goal(project, status=GoalStatus.IN_PROGRESS)
    # Two PENDING tasks (decline-reopened) and one COMPLETED (untouched).
    pending_a = Task(
        id=f"{project.code}-T-001",
        project_id=project.id,
        goal_id=goal.id,
        description="A",
        status=TaskStatus.PENDING,
    )
    pending_b = Task(
        id=f"{project.code}-T-002",
        project_id=project.id,
        goal_id=goal.id,
        description="B",
        status=TaskStatus.PENDING,
    )
    completed = Task(
        id=f"{project.code}-T-003",
        project_id=project.id,
        goal_id=goal.id,
        description="C",
        status=TaskStatus.COMPLETED,
    )
    for t in (pending_a, pending_b, completed):
        store.save_task(project.code, t)

    orch = Orchestrator(project, runners={})
    called: list[str] = []
    monkeypatch.setattr(
        orch, "_run_task_with_redo",
        lambda t, summary: called.append(t.id),
    )
    monkeypatch.setattr(
        orch, "_leader_verify_goal",
        lambda *a, **kw: None,  # skip verify in unit test
    )

    summary = RunSummary(project=project)
    orch._reexecute_goal(goal, summary)

    assert called == [pending_a.id, pending_b.id]
    # COMPLETED task is left alone.
    assert "T-003" not in str(called)


def test_reexecute_goal_marks_redispatched_in_transition_log(project: Project, monkeypatch):
    """Re-executed tasks get a transition entry pointing at the redo
    cause so the audit trail explains why a COMPLETED task got re-run."""
    from modulatio.orchestration import Orchestrator, RunSummary
    from modulatio.types import Task, TaskStatus

    goal = _create_goal(project, status=GoalStatus.IN_PROGRESS)
    task = Task(
        id=f"{project.code}-T-001",
        project_id=project.id,
        goal_id=goal.id,
        description="A",
        status=TaskStatus.PENDING,
    )
    store.save_task(project.code, task)

    orch = Orchestrator(project, runners={})
    monkeypatch.setattr(orch, "_run_task_with_redo", lambda t, s: None)
    monkeypatch.setattr(orch, "_leader_verify_goal", lambda *a, **kw: None)

    orch._reexecute_goal(goal, RunSummary(project=project))

    reloaded = store.get_task(project.code, task.id)
    rationales = [tr.rationale for tr in reloaded.transitions]
    assert any("re-dispatch" in r for r in rationales)


# ─── Integration tests: orchestrator wires approval_required correctly ──────

def test_on_the_fence_verdict_opens_approval_required_ticket(project: Project):
    """Leader's on_the_fence verdict = 'review before accepting' — the
    ticket it opens IS asking for a decision, so approval_required must
    be True. Under basic semantics, this is the primary Leader-initiated
    approval path."""
    runners = {
        "leader": _leader_with_verdict("on_the_fence"),
        "planner": _coordinator_stub,
        "drafter": _drafter_stub,
        "qc": _qc_stub,
    }
    orch = Orchestrator(project, runners)
    orch.kickoff("one piece")

    tickets = store.list_tickets(PROJECT_CODE)
    assert len(tickets) == 1
    assert tickets[0].priority is TicketPriority.CRITICAL
    assert tickets[0].approval_required is True, (
        "on_the_fence tickets explicitly request a human decision"
    )


def test_satisfied_verdict_opens_informational_ticket_without_approval_flag(project: Project):
    """'ready for sign-off' tickets are informational; the user may still
    sign off but Leader isn't gating anything. approval_required stays
    False to keep the signal honest — not every resolved-goal ticket is
    an approval request."""
    runners = {
        "leader": _leader_with_verdict("satisfied"),
        "planner": _coordinator_stub,
        "drafter": _drafter_stub,
        "qc": _qc_stub,
    }
    orch = Orchestrator(project, runners)
    orch.kickoff("one piece")

    tickets = store.list_tickets(PROJECT_CODE)
    assert len(tickets) == 1
    assert tickets[0].approval_required is False, (
        "satisfied/sign-off tickets are notifications, not gated approvals"
    )


# ─── Prompt: Leader verify sees prior approval decisions ────────────────────

def test_leader_verify_prompt_renders_prior_approval_decisions(project: Project):
    """Seed two resolved approval tickets (one approved, one denied) on
    the project. On the next goal verification, Leader's prompt surfaces
    both with their decisions + decider — so Leader can reason 'already
    asked, already answered, don't ask again.'"""
    prior_approved = store.create_ticket(
        project_id=project.id,
        project_code=project.code,
        priority=TicketPriority.CRITICAL,
        title="Continue past 80% budget?",
        body="",
        approval_required=True,
    )
    store.update_ticket_approval(
        project.code, prior_approved.id, decision="approved", decided_by="user",
    )

    prior_denied = store.create_ticket(
        project_id=project.id,
        project_code=project.code,
        priority=TicketPriority.CRITICAL,
        title="Accept output despite concerns?",
        body="",
        approval_required=True,
    )
    store.update_ticket_approval(
        project.code, prior_denied.id, decision="denied", decided_by="user",
    )

    # Capture the leader-verify prompt text.
    captured = {}
    real_leader = _leader_with_verdict("satisfied")
    def _capturing_leader(prompt: str) -> str:
        if "LEADER GOAL VERIFICATION" in prompt:
            captured["verify_prompt"] = prompt
        return real_leader(prompt)

    runners = {
        "leader": _capturing_leader,
        "planner": _coordinator_stub,
        "drafter": _drafter_stub,
        "qc": _qc_stub,
    }
    orch = Orchestrator(project, runners)
    orch.kickoff("one piece")

    prompt = captured["verify_prompt"]
    # Both prior decisions appear by ticket id, title, decision, and decider.
    assert prior_approved.id in prompt
    assert "Continue past 80% budget?" in prompt
    assert "approved" in prompt

    assert prior_denied.id in prompt
    assert "Accept output despite concerns?" in prompt
    assert "denied" in prompt

    # The decider is visible so Leader knows 'the user said yes' vs 'a
    # different role answered.' Basic field, but load-bearing.
    assert "user" in prompt


def test_leader_verify_prompt_neutral_marker_when_no_prior_approvals(project: Project):
    """No prior approvals = neutral placeholder instead of an empty
    section. Prevents the prompt from mis-rendering as if Leader was
    handed an empty list to 'respect.'"""
    captured = {}
    real_leader = _leader_with_verdict("satisfied")
    def _capturing_leader(prompt: str) -> str:
        if "LEADER GOAL VERIFICATION" in prompt:
            captured["verify_prompt"] = prompt
        return real_leader(prompt)

    runners = {
        "leader": _capturing_leader,
        "planner": _coordinator_stub,
        "drafter": _drafter_stub,
        "qc": _qc_stub,
    }
    orch = Orchestrator(project, runners)
    orch.kickoff("one piece")

    prompt = captured["verify_prompt"]
    # The block renders with an explicit neutral marker, not empty space.
    assert "no prior approval decisions" in prompt.lower()
