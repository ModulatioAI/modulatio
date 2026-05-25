"""Tests for the scope-discipline slice.

Adds explicit scope guidance to the Leader and Coordinator prompts so
short, single-deliverable objectives don't decompose into platform-style
work. Plus a soft post-decompose check that warns (non-blocking) when
a short objective yields a heavy goal list.

Surfaced from the crypto_advisers re-validation 2026-04-27 where the
objective 'To analyze the crypto market to know when to buy or sell'
became 4 goals + 18+ tasks (full SQLite-backed ingestion pipeline)
instead of the ~3 tasks the user actually wanted (research + draft
list + QC).
"""
from __future__ import annotations

from pathlib import Path

import json
import pytest

from modulatio import vault
from modulatio.orchestration import (
    _TASK_PLAN_PROMPT,
    _LEADER_DECOMPOSE_PROMPT,
    Orchestrator,
)
from modulatio.types import Project


PROJECT_CODE = "SCD"


@pytest.fixture
def project(tmp_path: Path, monkeypatch) -> Project:
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    vault.init_project(PROJECT_CODE, "Scope-discipline fixture", "obj")
    return Project(
        code=PROJECT_CODE,
        name="Scope-discipline fixture",
        objective="obj",
        leader_model="stub",
        wiki_path=str(tmp_path / PROJECT_CODE.lower()),
    )


# ─── Prompt content: guidance is present ────────────────────────────────────


def test_leader_prompt_includes_scope_discipline_guidance():
    """Leader's decompose prompt must include explicit guidance to
    prefer single-deliverable goal structure for short verb-objectives
    rather than multi-stage platform decomposition."""
    text = _LEADER_DECOMPOSE_PROMPT
    # Match the spirit, not the exact phrasing — a future copy edit
    # shouldn't break the test.
    lower = text.lower()
    assert "simplest" in lower or "minimum" in lower or "fewest" in lower
    # And explicit reference to deliverable-shape thinking.
    assert "deliverable" in lower or "artifact" in lower


def test_coordinator_prompt_distinguishes_skills_from_capabilities():
    """Coordinator prompt must include explicit anti-confusion guidance
    so the model doesn't put capability tags ('writing', 'web-search')
    into the required_skills slot. Surfaced 2026-04-28 in the WLT run
    where T-001 had required_skills=['research', 'web-search'] —
    those are capability tags, not skill names. Pure capability-tag
    confusion."""
    text = _TASK_PLAN_PROMPT.lower()
    # Some signal that the two slots are distinct + not interchangeable.
    assert (
        "do not put capability" in text
        or "capability tags do not go" in text
        or "not a capability tag" in text
        or ("skills are distinct" in text and "capabilities" in text)
        or "registered skill name" in text
    ), "Coordinator prompt should explicitly warn against capability/skill confusion"


def test_coordinator_prompt_includes_task_count_guidance():
    """Coordinator's plan prompt must steer toward proportional task
    counts — no SQLite-backed ingestion pipelines for 'produce a list'
    style goals."""
    text = _TASK_PLAN_PROMPT
    lower = text.lower()
    # Some signal that proportionality matters.
    assert (
        "proportional" in lower
        or "simplest" in lower
        or "fewest" in lower
        or "smallest" in lower
        or "minimal" in lower
    )


# ─── Soft post-decompose warning ────────────────────────────────────────────


def _stub_runner_emitting_n_goals(n: int):
    """Build a leader-runner that returns N stub goals."""
    def _runner(prompt: str) -> str:
        goals = [
            {
                "description": f"Stub goal {i}",
                "success_criteria": "ok",
                "evidence_required": [],
            }
            for i in range(1, n + 1)
        ]
        return f"```json\n{json.dumps(goals)}\n```"
    return _runner


def _empty_runners(leader_runner):
    """Other runners do nothing — coordinator returns empty task list
    so the goal loop has no real work."""
    return {
        "leader": leader_runner,
        "planner": lambda p: "```json\n[]\n```",
        "drafter": lambda p: "",
        "qc": lambda p: "",
        "researcher": lambda p: "",
    }


def test_short_objective_with_heavy_goal_list_emits_warning(project: Project):
    """A short objective that decomposes into 6+ goals triggers a
    summary.errors warning so the user notices the over-decomposition
    without blocking the run."""
    runners = _empty_runners(_stub_runner_emitting_n_goals(7))
    orch = Orchestrator(project, runners)
    summary = orch.kickoff("analyze the crypto market")

    assert any(
        "scope" in err.lower() or "decomposit" in err.lower()
        for err in summary.errors
    ), f"expected scope-warning in errors, got {summary.errors!r}"


def test_short_objective_with_modest_goal_list_no_warning(project: Project):
    """Same short objective but only 3 goals — no warning. The check
    fires on heavy decomposition, not on goal-count alone."""
    runners = _empty_runners(_stub_runner_emitting_n_goals(3))
    orch = Orchestrator(project, runners)
    summary = orch.kickoff("analyze the crypto market")

    assert not any(
        "scope" in err.lower() or "decomposit" in err.lower()
        for err in summary.errors
    )


def test_long_objective_with_many_goals_no_warning(project: Project):
    """A long, detailed objective justifies more goals — no warning
    even when the goal count would otherwise trip the threshold.
    This avoids false alarms on legitimate multi-artifact projects."""
    runners = _empty_runners(_stub_runner_emitting_n_goals(7))
    orch = Orchestrator(project, runners)
    long_objective = (
        "Build a complete SaaS platform with: "
        "user authentication and roles, billing integration, "
        "admin dashboard, public marketing site, customer portal, "
        "API with rate limiting, and a mobile companion app — "
        "deliver each as a separately-deployable component."
    )
    summary = orch.kickoff(long_objective)

    assert not any(
        "scope" in err.lower() or "decomposit" in err.lower()
        for err in summary.errors
    )
