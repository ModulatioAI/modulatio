"""Tests for the plain-English ticket-body rewrite.

Surfaced 2026-04-28 in the WLT real-model run: ticket bodies were
technical / jargon-heavy ('declared required_skills containing entries
that are NOT in the current skill registry — the planner likely
hallucinated a skill name'). User asked for clear English so the
intended audience (the operator) can understand what's being requested
without needing to know the harness's internals.

Each ticket body now follows a consistent structure:

  ## What happened   — one-paragraph plain English
  ## Why it matters  — operator-facing impact
  ## What you can do — concrete actions, plain English

Tests assert the structural markers + that the language stays
operator-readable (no unexplained 'required_skills', no raw Python
list syntax in the prose, etc.).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from modulatio import store, vault
from modulatio.types import Project


PROJECT_CODE = "TPE"


@pytest.fixture
def project(tmp_path: Path, monkeypatch) -> Project:
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    vault.init_project(PROJECT_CODE, "Plain-English fixture", "obj")
    return Project(
        code=PROJECT_CODE,
        name="Plain-English fixture",
        objective="obj",
        leader_model="stub",
        wiki_path=str(tmp_path / PROJECT_CODE.lower()),
    )


def test_capability_ticket_body_uses_plain_english(project: Project):
    """A ROSTER_GAP / invalid-skill ticket reads as plain English with
    the structured 'What happened / Why it matters / What you can do'
    layout, not raw Python list dumps."""
    from modulatio import dispatch
    from modulatio.orchestration import Orchestrator
    from modulatio.orchestration import RunSummary
    from modulatio.types import Task, TaskStatus

    runners = {"leader": lambda p: "", "planner": lambda p: "",
               "drafter": lambda p: "", "qc": lambda p: "",
               "researcher": lambda p: ""}
    orch = Orchestrator(project, runners)
    summary = RunSummary(project=project)

    task = Task(
        id="TPE-T-001",
        project_id=project.id,
        goal_id="TPE-G-001",
        description="Research crypto wallets",
        required_skills=["nonexistent_skill"],
        status=TaskStatus.DISPATCHED,
    )
    result = dispatch.DispatchResult(
        outcome=dispatch.DispatchOutcome.INVALID_SKILL,
        agent=None,
        missing_skills=("nonexistent_skill",),
    )
    orch._open_capability_ticket(task, result, summary)
    tickets = store.list_tickets(project.code)
    body = tickets[0].body

    # Structured headers.
    assert "## What happened" in body
    assert "## What you can do" in body
    # Plain-English signals.
    lower = body.lower()
    assert "your team" in lower or "the team" in lower
    # No raw Python list literal in the prose (e.g. "['research', 'web-search']").
    assert "['" not in body, (
        "Ticket body should describe missing skills in prose, not "
        "render the Python list repr."
    )


