"""LOW-severity audit regression tests for project_execution.

Finding #61: tick() error-path ExecutionResults reported
sub_objectives_total=0, misrepresenting plan size to the daemon even
though sub_objectives_completed was set to the plan's current_index.
The error paths now derive the real total from the plan body.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from modulatio import plans, project_execution, vault
from modulatio.types import Project


PROJECT_CODE = "low"


@pytest.fixture
def isolated(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path / "projects")
    vault.init_project(PROJECT_CODE, "Test", "obj")
    return tmp_path


@pytest.fixture
def project(isolated, tmp_path):
    return Project(
        code=PROJECT_CODE,
        name="Test",
        objective="Improve telemetry coverage",
        leader_model="stub",
        wiki_path=str(tmp_path / "projects" / PROJECT_CODE),
    )


def _approved_plan(project, sub_objective_count: int = 3) -> "plans.PlanRecord":
    items = []
    for i in range(1, sub_objective_count + 1):
        items.append(
            f"**{i}. Step {i} title** — Step {i} description.\n"
            f"  - *Files:* src/step{i}.py\n"
            f"  - *Done when:* tests pass\n"
        )
    body = (
        plans.PLAN_MARKER + "\n\n"
        "### Diagnostic\nState X.\n\n"
        "### Sub-objectives\n" + "\n".join(items) + "\n\n"
        "### Risks\nMaybe.\n"
    )
    saved = plans.persist(
        body, project_code=project.code,
        agent_id="leader",
        source_message="please plan",
    )
    plans.mark_approved(saved.id, project.code, decided_by="user")
    return plans.load(saved.id, project.code)


def test_tick_project_loader_failure_reports_real_total(project, isolated):
    """When project_loader raises, the error ExecutionResult must report
    the real plan size, not 0 (which paired with completed misrepresents
    plan size to the daemon)."""
    plan = _approved_plan(project, sub_objective_count=4)

    def _project_loader(_code: str):
        raise RuntimeError("boom")

    def _runners_for(_p):
        return {"leader": lambda p: ""}

    results = project_execution.tick(
        project_loader=_project_loader,
        runners_for=_runners_for,
        project_codes=[project.code],
    )

    assert len(results) == 1
    res = results[0]
    assert res.plan_id == plan.id
    assert "project_loader failed" in (res.error or "")
    # Regression: previously hardcoded 0. The plan has 4 sub-objectives.
    assert res.sub_objectives_total == 4


def test_tick_runners_for_failure_reports_real_total(project, isolated):
    plan = _approved_plan(project, sub_objective_count=3)

    def _project_loader(_code: str):
        return project

    def _runners_for(_p):
        raise RuntimeError("no runners")

    results = project_execution.tick(
        project_loader=_project_loader,
        runners_for=_runners_for,
        project_codes=[project.code],
    )

    assert len(results) == 1
    res = results[0]
    assert res.plan_id == plan.id
    assert "runners_for failed" in (res.error or "")
    assert res.sub_objectives_total == 3
