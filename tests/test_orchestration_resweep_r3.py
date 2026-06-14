"""Round-3 re-sweep regressions for ``src/modulatio/orchestration.py``.

Each test pins one confirmed 0.9.0 pre-ship debug finding. Kept additive +
isolated from any prior ``test_orchestration_resweep*.py`` file.

Findings covered (others were deferred / false-positive — see the run report):
  #1  Comptroller deny BLOCKER now carries affected_goal_id so auto-resume fires
  #2  qc_history.similar_verdicts failure can no longer fail a QC review
  #3  converse attachment inlining falls back to reading att.path (content=None)
  #5  auto-resume retires a stale ticket whose goal already terminalized
  #6  no-regress refusal audit logs the TOKEN measure, not whitespace word-count
  #8  wave-reflect cannot swap a pending task's ASSEMBLER skill out of band
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from modulatio import store, vault
from modulatio.types import (
    Goal,
    GoalStatus,
    Project,
    Task,
    TaskStatus,
    TicketPriority,
    TicketStatus,
)


PROJECT_CODE = "R3O"


@pytest.fixture
def project(tmp_path: Path, monkeypatch) -> Project:
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    vault.init_project(PROJECT_CODE, "Re-sweep R3 fixture", "obj")
    return Project(
        code=PROJECT_CODE,
        name="Re-sweep R3 fixture",
        objective="obj",
        leader_model="stub",
        wiki_path=str(tmp_path / PROJECT_CODE.lower()),
    )


def _orch(project: Project):
    from modulatio.orchestration import Orchestrator

    runners = {
        "leader": lambda p: "",
        "planner": lambda p: "",
        "drafter": lambda p: "",
        "qc": lambda p: "",
        "researcher": lambda p: "",
    }
    return Orchestrator(project, runners)


# ── Finding #1 ──────────────────────────────────────────────────────────────
def test_deny_ticket_carries_affected_goal_id(project: Project):
    """The Comptroller escalation-budget deny BLOCKER must bind affected_goal_id
    (= task.goal_id), or _auto_resume_refreshable_goals — which skips
    affected_goal_id-only tickets — can never honor the ticket's auto-resume
    promise."""
    from modulatio import comptroller, roster
    from modulatio.orchestration import RunSummary

    orch = _orch(project)
    summary = RunSummary(project=project)

    task = Task(
        id="R3O-T-001",
        project_id=project.id,
        goal_id="R3O-G-001",
        description="produce a unit",
        status=TaskStatus.QC_REJECTED,
    )
    denied = roster.Agent(
        id="premium-1",
        name="Premium",
        model_tier="reasoning",
        cost_class="premium-cloud",
    )
    refresh_at = datetime.now(timezone.utc) + timedelta(days=1)
    auth = comptroller.Authorization(
        allowed=False, refresh_at=refresh_at, reason="daily budget exhausted"
    )

    orch._open_budget_ticket(task, denied, auth, summary)

    tickets = [
        t
        for t in store.list_tickets(project.code)
        if t.priority is TicketPriority.BLOCKER
    ]
    assert tickets, "expected a BLOCKER ticket"
    assert tickets[0].affected_goal_id == "R3O-G-001"
    assert tickets[0].affected_task_id == "R3O-T-001"


# ── Finding #2 ──────────────────────────────────────────────────────────────
def test_qc_review_survives_similar_verdicts_failure(project: Project, monkeypatch):
    """Advisory QC-history precedent must never fail a task: a raising
    similar_verdicts is swallowed and the review proceeds (mirrors
    _recall_team_memory's defensive contract)."""
    from modulatio import qc_history
    from modulatio.orchestration import Orchestrator

    artifacts = Path(project.wiki_path) / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    draft = artifacts / "draft.md"
    draft.write_text("This is a complete, substantive draft body.\n" * 5)

    def _raise(*a, **k):
        raise RuntimeError("lancedb rebuild blew up mid-create")

    monkeypatch.setattr(qc_history, "similar_verdicts", _raise)

    runners = {
        "leader": lambda p: "",
        "planner": lambda p: "",
        "drafter": lambda p: "",
        # Valid JSON verdict so the review parses through to a result.
        "qc": lambda p: '{"passed": true, "check": "ok", "notes": ""}',
        "researcher": lambda p: "",
    }
    orch = Orchestrator(project, runners)
    # Force the embedder-present branch so similar_verdicts is actually called.
    orch.qc_history_embedder = object()

    task = Task(
        id="R3O-T-002",
        project_id=project.id,
        goal_id="R3O-G-002",
        description="write a draft",
        artifact_kind="text",
        qc_agent_id=None,
    )

    verdict, _notes, _defect = orch._qc_review(
        task, draft, checksum="sha256:deadbeef", token_count=40
    )
    assert verdict.passed is True


# ── Finding #3 ──────────────────────────────────────────────────────────────
def test_kickoff_attachment_path_only_document_is_read(tmp_path: Path):
    """A path-only document attachment (content=None) must inline its file body,
    not an empty fenced block, so the Leader does not plan blind."""
    from modulatio.attachments import Attachment
    from modulatio.orchestration import _format_kickoff_attachments

    doc = tmp_path / "brief.md"
    doc.write_text("LOAD-BEARING-BRIEF-CONTENT")
    att = Attachment(kind="document", path=doc, name="brief.md", content=None)

    rendered = _format_kickoff_attachments([att])
    assert "LOAD-BEARING-BRIEF-CONTENT" in rendered


def test_kickoff_attachment_missing_path_falls_back_to_empty(tmp_path: Path):
    """A path-only document whose file is unreadable degrades to '' without
    raising."""
    from modulatio.attachments import Attachment
    from modulatio.orchestration import _format_kickoff_attachments

    att = Attachment(
        kind="document",
        path=tmp_path / "nope.md",
        name="nope.md",
        content=None,
    )
    rendered = _format_kickoff_attachments([att])
    assert "nope.md" in rendered  # filename still referenced, no crash


# ── Finding #5 ──────────────────────────────────────────────────────────────
def test_auto_resume_retires_stale_ticket_for_terminal_goal(project: Project):
    """An OPEN refreshable ticket whose goal already COMPLETED (e.g. a duplicate
    ticket recovered first) must be RESOLVED, not orphaned OPEN forever."""
    from modulatio.orchestration import RunSummary

    orch = _orch(project)
    summary = RunSummary(project=project)

    goal = Goal(
        id="R3O-G-005",
        project_id=project.id,
        description="already done",
        success_criteria="ok",
        status=GoalStatus.COMPLETED,
    )
    store.save_goal(project.code, goal)

    past = datetime.now(timezone.utc) - timedelta(hours=1)
    ticket = store.create_ticket(
        project_id=project.id,
        project_code=project.code,
        run_id=project.run_id,
        priority=TicketPriority.BLOCKER,
        title="stale refresh ticket",
        body="body",
        affected_goal_id=goal.id,
        actor="leader",
    )
    ticket.refresh_at = past
    from modulatio.store import _ticket_path, _write_entity

    _write_entity(_ticket_path(project.code, ticket.id), ticket, ticket.body)

    orch._auto_resume_refreshable_goals(summary)

    reloaded = [t for t in store.list_tickets(project.code) if t.id == ticket.id]
    assert reloaded, "ticket should still exist"
    assert reloaded[0].status is TicketStatus.RESOLVED


# ── Finding #6 ──────────────────────────────────────────────────────────────
def test_note_regression_kept_audit_is_token_native(project: Project, monkeypatch):
    """The no-regress refusal AUDIT rationale must use the TOKEN measure the gate
    decides on, not whitespace word-count — so a low-whitespace (minified)
    deliverable does not log a misleading '1 -> 1 tokens'."""
    from modulatio import tool_summarization
    from modulatio.orchestration import Orchestrator

    artifacts = Path(project.wiki_path) / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    # A single-line "minified" blob: hundreds of chars, ~1 whitespace word.
    kept_text = "a" * 800
    path = artifacts / "kept.json"
    path.write_text(kept_text)

    # Deterministic, whitespace-independent token count (char/4-style).
    monkeypatch.setattr(
        tool_summarization,
        "count_tokens",
        lambda model, *, text: max(1, len(text) // 4),
    )

    runners = {
        "leader": lambda p: "",
        "planner": lambda p: "",
        "drafter": lambda p: "",
        "qc": lambda p: "",
        "researcher": lambda p: "",
    }
    orch = Orchestrator(project, runners)
    task = Task(
        id="R3O-T-006",
        project_id=project.id,
        goal_id="R3O-G-006",
        description="data",
        artifact_kind="data",
    )

    orch._note_regression_kept(task, path, "tiny")

    # The AUDIT rationale is token-native: 800 chars -> ~200 tokens, NOT the
    # single whitespace "word" .split() would report for a minified blob.
    rationale = task.transitions[-1].rationale
    assert f"{len(kept_text) // 4} →" in rationale  # "200 →" not "1 →"
    assert "1 → 1 tokens" not in rationale
    assert "no-regress" in rationale


# ── Finding #8 ──────────────────────────────────────────────────────────────
def test_wave_reflect_rejects_assembler_skill_swap(project: Project):
    """A wave-boundary reflect 'revise' must NOT swap a pending task's assembler
    skill out of band — that bypasses _select_assembler_skill canonicalization +
    the #73 evidence-family normalization. Non-assembler revisions still apply."""
    import json

    from modulatio.orchestration import Orchestrator, RunSummary

    task = Task(
        id="R3O-T-008",
        project_id=project.id,
        goal_id="R3O-G-008",
        description="assemble units",
        artifact_kind="text",
        required_skills=["document-assembly"],
        status=TaskStatus.PENDING,
    )
    store.save_task(project.code, task)

    reflection = json.dumps(
        {
            "edits": [
                {
                    "task_id": task.id,
                    "action": "revise",
                    # swaps the assembler family AND tries to re-describe
                    "description": "now assemble as media",
                    "required_skills": ["media-assembly"],
                }
            ]
        }
    )

    orch = Orchestrator(
        project,
        {
            "leader": lambda p: reflection,
            "planner": lambda p: "",
            "drafter": lambda p: "",
            "qc": lambda p: "",
            "researcher": lambda p: "",
        },
    )
    orch.operator_present = False  # autonomous-only path

    saved: list = []
    orch._wave_boundary_reflect(
        [task], {task.id: task}, RunSummary(project=project), saved.append
    )

    # Assembler skill swap rejected — still document-assembly.
    assert "media-assembly" not in task.required_skills
    assert "document-assembly" in task.required_skills
    # Non-skill revision (description) still landed.
    assert task.description == "now assemble as media"
