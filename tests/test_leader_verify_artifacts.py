"""Tests for the Leader-artifact-visibility fix.

Surfaced 2026-04-28 in the WLT real-model run: T-004 produced a
guide artifact at `artifacts/WLT_crypto_wallets_guide.md`, QC passed
it, but the Leader's goal-verify prompt ONLY scanned
`artifacts/drafts/<task-id>.md` and missed the file entirely. The
Leader returned a 'disappointed' verdict claiming the artifact was
never produced.

The fix:
  1. Leader's scan respects ``task.output_path`` (relative to the
     project's artifacts/ directory) before falling back to the
     drafts/ convention.
  2. The verify prompt includes a snippet of each artifact's actual
     content so the Leader has something concrete to evaluate.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from modulatio import store, vault
from modulatio.orchestration import Orchestrator, RunSummary
from modulatio.types import (
    Goal, GoalStatus, Project, Task, TaskStatus,
)


PROJECT_CODE = "LVA"


@pytest.fixture
def project(tmp_path: Path, monkeypatch) -> Project:
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    vault.init_project(PROJECT_CODE, "Leader-verify-artifact fixture", "obj")
    return Project(
        code=PROJECT_CODE,
        name="Leader-verify-artifact fixture",
        objective="obj",
        leader_model="stub",
        wiki_path=str(tmp_path / PROJECT_CODE.lower()),
    )


def _capturing_orch(project: Project):
    """Orchestrator with a leader runner that captures the verify
    prompt for assertion."""
    captured: list[str] = []
    def _leader(prompt: str) -> str:
        captured.append(prompt)
        return "```json\n" + json.dumps({
            "verdict": "satisfied",
            "rationale": "ok",
            "report": "ok",
        }) + "\n```"
    runners = {
        "leader": _leader,
        "planner": lambda p: "```json\n[]\n```",
        "drafter": lambda p: "",
        "qc": lambda p: "",
        "researcher": lambda p: "",
    }
    return Orchestrator(project, runners), captured


def test_leader_verify_finds_artifact_at_task_output_path(project: Project, tmp_path: Path):
    """A completed task with ``output_path='guide.md'`` produces a file
    at ``<artifacts>/guide.md``. Leader's verify scan must find it
    there, not require a drafts/<task-id>.md naming."""
    artifacts_root = tmp_path / PROJECT_CODE.lower() / "artifacts"
    artifacts_root.mkdir(parents=True, exist_ok=True)
    guide_path = artifacts_root / "WLT_crypto_wallets_guide.md"
    guide_path.write_text(
        "# Beginner's Guide to Crypto Wallets\n\nIntro paragraph...\n"
    )

    goal = Goal(
        id="LVA-G-001",
        project_id=project.id,
        description="Produce the guide",
        success_criteria="guide exists",
        status=GoalStatus.IN_PROGRESS,
    )
    store.save_goal(project.code, goal)
    task = Task(
        id="LVA-T-001",
        project_id=project.id,
        goal_id=goal.id,
        description="Draft the guide",
        output_path="WLT_crypto_wallets_guide.md",
        status=TaskStatus.COMPLETED,
    )
    store.save_task(project.code, task)

    orch, captured = _capturing_orch(project)
    summary = RunSummary(project=project)
    orch._leader_verify_goal(goal, [task], summary)

    assert len(captured) == 1
    prompt = captured[0]
    assert "WLT_crypto_wallets_guide.md" in prompt, (
        "Leader prompt must reference the artifact at task.output_path "
        "(it lives at artifacts/<output_path>, not drafts/<task-id>.md)"
    )


def test_leader_verify_includes_artifact_content_in_prompt(project: Project, tmp_path: Path):
    """Path discovery alone isn't enough — Leader needs actual content
    to evaluate quality. The verify prompt must include a snippet of
    each completed task's artifact body, not just the path."""
    artifacts_root = tmp_path / PROJECT_CODE.lower() / "artifacts"
    artifacts_root.mkdir(parents=True, exist_ok=True)
    guide_path = artifacts_root / "guide.md"
    guide_body = (
        "# The Guide Title\n\n"
        "Distinctive sentence the Leader can recognize: "
        "BUDGETARY-SENTINEL-XYZ-12345.\n\n"
        "Section content...\n"
    )
    guide_path.write_text(guide_body)

    goal = Goal(
        id="LVA-G-002",
        project_id=project.id,
        description="Produce the guide",
        success_criteria="guide exists",
        status=GoalStatus.IN_PROGRESS,
    )
    store.save_goal(project.code, goal)
    task = Task(
        id="LVA-T-002",
        project_id=project.id,
        goal_id=goal.id,
        description="Draft the guide",
        output_path="guide.md",
        status=TaskStatus.COMPLETED,
    )
    store.save_task(project.code, task)

    orch, captured = _capturing_orch(project)
    summary = RunSummary(project=project)
    orch._leader_verify_goal(goal, [task], summary)

    prompt = captured[0]
    assert "BUDGETARY-SENTINEL-XYZ-12345" in prompt, (
        "Leader prompt must include the artifact's actual body so "
        "the Leader can evaluate quality, not just file existence."
    )


def test_leader_verify_falls_back_to_drafts_convention(project: Project, tmp_path: Path):
    """When a task has no output_path, the legacy drafts/ convention
    still works (back-compat for older roster fixtures)."""
    artifacts_root = tmp_path / PROJECT_CODE.lower() / "artifacts"
    drafts_dir = artifacts_root / "drafts"
    drafts_dir.mkdir(parents=True, exist_ok=True)
    legacy_path = drafts_dir / "lva-t-003.md"
    legacy_path.write_text("Legacy draft body — DRAFTS-FALLBACK-MARKER-789.\n")

    goal = Goal(
        id="LVA-G-003",
        project_id=project.id,
        description="Legacy goal",
        success_criteria="ok",
        status=GoalStatus.IN_PROGRESS,
    )
    store.save_goal(project.code, goal)
    task = Task(
        id="LVA-T-003",
        project_id=project.id,
        goal_id=goal.id,
        description="Legacy task",
        output_path="",  # ← no output_path → fall back to drafts/
        status=TaskStatus.COMPLETED,
    )
    store.save_task(project.code, task)

    orch, captured = _capturing_orch(project)
    summary = RunSummary(project=project)
    orch._leader_verify_goal(goal, [task], summary)

    prompt = captured[0]
    assert "DRAFTS-FALLBACK-MARKER-789" in prompt
