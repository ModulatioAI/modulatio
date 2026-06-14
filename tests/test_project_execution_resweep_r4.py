"""Round-4 re-sweep regression for project_execution.

Covers one confirmed 0.9.0 pre-ship finding:

  * LOW/error-path — the bounded-mode wall-clock cap check guarded only
    the ``datetime.fromisoformat`` parse in a try/except, NOT the
    subsequent ``datetime.now(timezone.utc) - started_dt`` subtraction.
    A hand-edited (frontmatter is an explicitly supported edit surface)
    timezone-NAIVE ``execution_started_at`` parses fine, then explodes
    the aware-minus-naive subtraction with a TypeError that escapes the
    whole execution loop. The fix coerces a naive ``started_dt`` to UTC
    so the cap math is well-defined.
"""

from __future__ import annotations

import dataclasses
import unittest.mock as _mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from modulatio import budget, plans, project_execution, vault
from modulatio.types import Project


PROJECT_CODE = "tst"


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


def _executing_plan(project, sub_objective_count: int = 2) -> "plans.PlanRecord":
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
    plans.set_status(
        saved.id, project.code, "executing",
        decided_by="dispatcher", note="execution started",
    )
    return plans.load(saved.id, project.code)


def test_wall_clock_cap_handles_naive_started_at_without_typeerror(project, isolated):
    """A NAIVE ``plan_started_at`` (no tz offset — a legitimately
    hand-edited frontmatter value) must not crash the cap check.

    With the bug, ``datetime.fromisoformat`` parses the naive string
    successfully (so the try/except doesn't trip), then
    ``datetime.now(timezone.utc) - started_dt`` raises a TypeError that
    escapes the entire execution loop. With the fix the naive value is
    coerced to UTC and the cap fires cleanly → the plan pauses.
    """
    plan = _executing_plan(project, sub_objective_count=2)

    # Cap = 1 minute; started 10 minutes ago — well over the cap. The
    # timestamp is timezone-NAIVE (no offset), the bug trigger.
    naive_started = (
        datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=10)
    ).isoformat()
    assert "+" not in naive_started and naive_started[-6] != "-"  # truly naive

    # The top-of-loop ``live = plans.load(...)`` supplies the cap via
    # ``(live or plan).max_wall_clock_min`` — give the loaded record the cap.
    capped = dataclasses.replace(plan, max_wall_clock_min=1.0)

    tracker = budget.BudgetTracker(max_tokens=None, max_cost_usd=None)

    def _kickoff(_text: str, _so: dict):  # never reached: cap fires first
        raise AssertionError("kickoff should not run; cap should pause first")

    def _reflect(_prompt: str) -> str:  # never reached
        raise AssertionError("reflect should not run; cap should pause first")

    with _mock.patch.object(plans, "load", return_value=capped):
        result = project_execution._run_execution_loop(
            plan_id=plan.id,
            project=project,
            plan=capped,
            sub_objectives=plans.extract_sub_objectives(plan.body),
            kickoff_callable=_kickoff,
            reflect_runner=_reflect,
            reflect_chat_runner=None,
            completed=0,
            spawned_local=[],
            reflection_log_local=[],
            plan_started_at=naive_started,
            tracker=tracker,
        )

    # No TypeError escaped; the cap fired and paused the plan.
    assert result.final_status == "paused"
    assert result.paused_ticket_id is not None
    assert result.sub_objectives_completed == 0


def test_wall_clock_cap_still_fires_for_aware_started_at(project, isolated):
    """Regression guard: the AWARE path (engine-written, with offset)
    keeps working exactly as before — over-cap → paused."""
    plan = _executing_plan(project, sub_objective_count=2)

    aware_started = (
        datetime.now(timezone.utc) - timedelta(minutes=10)
    ).isoformat()
    capped = dataclasses.replace(plan, max_wall_clock_min=1.0)
    tracker = budget.BudgetTracker(max_tokens=None, max_cost_usd=None)

    def _kickoff(_text: str, _so: dict):
        raise AssertionError("kickoff should not run")

    def _reflect(_prompt: str) -> str:
        raise AssertionError("reflect should not run")

    with _mock.patch.object(plans, "load", return_value=capped):
        result = project_execution._run_execution_loop(
            plan_id=plan.id,
            project=project,
            plan=capped,
            sub_objectives=plans.extract_sub_objectives(plan.body),
            kickoff_callable=_kickoff,
            reflect_runner=_reflect,
            reflect_chat_runner=None,
            completed=0,
            spawned_local=[],
            reflection_log_local=[],
            plan_started_at=aware_started,
            tracker=tracker,
        )

    assert result.final_status == "paused"
    assert result.paused_ticket_id is not None
