"""The producer runbook — the always-on bar-commit spine injected at the head of
every producer task.

It is the procedural scaffold that lets a thinking-OFF producer stay rigorous:
the discipline reasoning would otherwise supply, at fixed prompt cost instead of
churning context with reasoning tokens. The generic discipline lives ONCE (the
producer-runbook seed); the craft for an artifact kind stays in the task's skill
+ standards, so it isn't duplicated in-prompt.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from modulatio import skills, vault
from modulatio.orchestration import Orchestrator
from modulatio.types import Project, ProjectState

PROJECT_CODE = "PRB"


@pytest.fixture
def project(tmp_path: Path, monkeypatch) -> Project:
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    vault.init_project(PROJECT_CODE, "producer-runbook fixture", "obj")
    return Project(
        code=PROJECT_CODE, name="producer-runbook fixture", objective="obj",
        state=ProjectState.ACTIVE, leader_model="stub",
        wiki_path=str(vault.project_dir(PROJECT_CODE)),
    )


def _orch(project: Project) -> Orchestrator:
    return Orchestrator(
        project, {"leader": lambda p: "", "drafter": lambda p: "", "qc": lambda p: ""}
    )


def test_with_producer_runbook_prepends_the_spine_at_the_head(project: Project):
    out = _orch(project)._with_producer_runbook("THE TASK BODY")
    assert "NAME THE OPERATION" in out          # the bar-commit spine rides along
    assert "QC holds your work" in out
    assert "THE TASK BODY" in out               # the task body is preserved
    # runbook is at the HEAD — read first, every time — ahead of the task
    assert out.index("NAME THE OPERATION") < out.index("THE TASK BODY")


def test_producer_runbook_seed_is_the_source_of_truth(project: Project):
    """The helper loads the producer-runbook seed (overridable), not a hardcode."""
    body = _orch(project)._with_producer_runbook("X")
    assert skills.load("producer-runbook").strip().splitlines()[0] in body


def test_coding_skill_keeps_craft_after_dropping_the_generic_spine():
    body = skills.load("coding")
    # the generic spine header is gone (now injected once via producer-runbook)...
    assert "name the operation, then commit the bar" not in body.lower()
    # ...but every task-specific coding best-practice stays
    assert "Reuse before you write" in body
    assert "Smoke-test via run_shell" in body
    assert "Don't bloat" in body
    # and the coding-specific per-operation bars are kept as code depth
    assert "Fix / debug" in body and "Refactor / improve" in body


def test_sourcing_skill_keeps_craft_after_dropping_the_generic_spine():
    body = skills.load("rigorous-sourcing")
    assert "name the operation, then commit the bar" not in body.lower()
    assert "Dates come from the world" in body
    assert "Cite what you use" in body
    # sourcing-specific per-operation bars kept
    assert "Gather / survey" in body and "Update / current events" in body
