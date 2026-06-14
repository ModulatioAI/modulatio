"""Pre-ship (0.9.0) regression tests for project_execution.

Covers three confirmed pre-ship findings:

  * MEDIUM/race — start_execution resumed from the STALE pre-lock plan
    snapshot rather than the fresh reload taken under the lock, re-running
    already-completed sub-objectives and double-counting budget under a
    daemon race.
  * LOW/correctness — tick() sliced ``discovered[:max_per_tick]`` BEFORE
    the staleness skip, so a stale lowest-id plan starved the tick.
  * LOW/error-path — tick() reported ``final_status='executing'`` for
    project_loader / runners_for failures even though the plan never
    started and is still 'approved' on disk.
"""

from __future__ import annotations

import dataclasses
import json
import unittest.mock as _mock
from pathlib import Path

import pytest

from modulatio import plans, project_execution, vault
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


def _scripted_reflect(outcomes: list[dict]):
    queue = list(outcomes)

    def _runner(_prompt: str) -> str:
        if not queue:
            raise RuntimeError("scripted_reflect exhausted")
        decision = queue.pop(0)
        return f"reasoning prose...\n\n```json\n{json.dumps(decision)}\n```"

    return _runner


# ── MEDIUM/race: resume from fresh under-lock snapshot, not stale ───────


def test_start_execution_resumes_from_fresh_under_lock_index(project, isolated):
    """The pre-lock load can be STALE: a concurrent claimer may have
    advanced ``current_index`` (completed sub-objectives) before we won
    the claim lock. start_execution must resume from the FRESH reload
    taken under the lock, not the stale pre-lock snapshot — otherwise it
    re-runs already-completed sub-objectives.

    With the bug, ``completed`` is taken from the stale plan
    (current_index=0) and all 3 sub-objectives are re-kicked. With the
    fix it's taken from fresh (current_index=2) and only the final one
    fires.
    """
    plan = _approved_plan(project, sub_objective_count=3)

    real_load = plans.load
    call_count = {"n": 0}

    def _advancing_load(plan_id, project_code):
        call_count["n"] += 1
        rec = real_load(plan_id, project_code)
        # The SECOND load is the under-lock ``fresh`` reload. Simulate a
        # concurrent claimer having completed the first two sub-objectives
        # by returning a record with current_index=2 there. The first
        # (pre-lock) load keeps the stale current_index=0.
        if call_count["n"] == 2 and rec is not None:
            return dataclasses.replace(rec, current_index=2)
        return rec

    kicked: list[dict] = []

    def _kickoff(_text: str, so: dict):
        kicked.append(so)
        return "kicked"

    reflect = _scripted_reflect([{"outcome": "continue", "rationale": "x"}])

    with _mock.patch.object(plans, "load", side_effect=_advancing_load):
        result = project_execution.start_execution(
            plan.id, project,
            runners={"leader": reflect},
            reflect_runner=reflect,
            kickoff_callable=_kickoff,
        )

    # Resumed from fresh index 2 → only the third sub-objective fired.
    assert len(kicked) == 1
    assert result.sub_objectives_completed == 3
    assert result.sub_objectives_total == 3
    assert result.final_status == "done"


def test_start_execution_resumes_budget_totals_from_fresh(project, isolated):
    """Budget tracker resume totals must come from the FRESH under-lock
    reload, not the stale pre-lock snapshot — otherwise a concurrent
    claimer's already-spent tokens are discounted and the cap clock is
    reset to an earlier total (double-counting headroom)."""
    plan = _approved_plan(project, sub_objective_count=1)

    real_load = plans.load
    call_count = {"n": 0}

    def _spent_load(plan_id, project_code):
        call_count["n"] += 1
        rec = real_load(plan_id, project_code)
        # Under-lock reload reports tokens already spent by a concurrent
        # claimer; the stale pre-lock snapshot still shows zero.
        if call_count["n"] == 2 and rec is not None:
            return dataclasses.replace(rec, tokens_used=4321, cost_usd_used=0.42)
        return rec

    captured: dict = {}

    real_tracker = project_execution.budget.BudgetTracker

    def _capturing_tracker(*args, **kwargs):
        captured["tokens_used"] = kwargs.get("tokens_used")
        captured["cost_usd_used"] = kwargs.get("cost_usd_used")
        return real_tracker(*args, **kwargs)

    reflect = _scripted_reflect([{"outcome": "continue", "rationale": "x"}])

    with _mock.patch.object(plans, "load", side_effect=_spent_load), \
         _mock.patch.object(
             project_execution.budget, "BudgetTracker",
             side_effect=_capturing_tracker,
         ):
        project_execution.start_execution(
            plan.id, project,
            runners={"leader": reflect},
            reflect_runner=reflect,
            kickoff_callable=lambda _t, _s: "x",
        )

    assert captured["tokens_used"] == 4321
    assert captured["cost_usd_used"] == 0.42


# ── LOW/correctness: cap AFTER the staleness skip ──────────────────────


def test_tick_stale_lowest_id_plan_does_not_starve_tick(project, isolated):
    """tick() must not let a stale lowest-id plan consume the
    max_per_tick slot. With the bug the slice happened before the
    staleness skip, so a single stale lowest-id entry produced zero
    dispatches. With the fix the cap counts only plans actually
    dispatched, so a still-approved later plan is reached."""
    stale = _approved_plan(project, sub_objective_count=1)
    fresh_plan = _approved_plan(project, sub_objective_count=1)

    # The first entry is stale (claimed since the scan); the second is
    # genuinely still approved. Ordered list: stale first.
    stale_rec = plans.load(stale.id, project.code)
    fresh_rec = plans.load(fresh_plan.id, project.code)

    # Simulate the stale entry having been claimed since the scan.
    plans.set_status(
        stale.id, project.code, "executing",
        decided_by="other-daemon", note="claimed",
    )
    stale_snapshot = dataclasses.replace(stale_rec, status="approved")

    dispatched: list[str] = []

    def _loader(_code):
        return project

    def _runners(_p):
        return {"leader": _scripted_reflect(
            [{"outcome": "continue", "rationale": "x"}]
        )}

    real_start = project_execution.start_execution

    def _tracking_start(plan_id, proj, **kwargs):
        dispatched.append(plan_id)
        # Stub the kickoff so the dispatch is cheap and deterministic.
        return real_start(
            plan_id, proj,
            kickoff_callable=lambda _t, _s: "x",
            **kwargs,
        )

    with _mock.patch.object(
        project_execution, "find_approved_plans",
        return_value=[
            (project.code, stale_snapshot),
            (project.code, fresh_rec),
        ],
    ), _mock.patch.object(
        project_execution, "start_execution", side_effect=_tracking_start
    ):
        results = project_execution.tick(
            project_loader=_loader,
            runners_for=_runners,
            project_codes=[project.code],
            max_per_tick=1,
        )

    # The stale lowest-id plan was skipped, and the still-approved plan
    # was reached within the same tick rather than being starved.
    assert dispatched == [fresh_plan.id]
    assert len(results) == 1
    assert results[0].plan_id == fresh_plan.id


# ── LOW/error-path: loader / runners failure reports real status ───────


def test_tick_loader_failure_reports_real_status_not_executing(project, isolated):
    """When project_loader raises, the plan never started and is still
    'approved' on disk. The ExecutionResult must report that real
    status, not a misleading hardcoded 'executing'."""
    plan = _approved_plan(project, sub_objective_count=1)
    rec = plans.load(plan.id, project.code)

    def _boom_loader(_code):
        raise RuntimeError("cannot build project")

    def _runners(_p):  # pragma: no cover — never reached
        return {}

    with _mock.patch.object(
        project_execution, "find_approved_plans",
        return_value=[(project.code, rec)],
    ):
        results = project_execution.tick(
            project_loader=_boom_loader,
            runners_for=_runners,
            project_codes=[project.code],
        )

    assert len(results) == 1
    assert results[0].final_status == "approved"
    assert "project_loader failed" in (results[0].error or "")


def test_tick_runners_failure_reports_real_status_not_executing(project, isolated):
    """Same as the loader-failure path for runners_for: the plan never
    started, so report the real 'approved' status, not 'executing'."""
    plan = _approved_plan(project, sub_objective_count=1)
    rec = plans.load(plan.id, project.code)

    def _loader(_code):
        return project

    def _boom_runners(_p):
        raise RuntimeError("cannot wire runners")

    with _mock.patch.object(
        project_execution, "find_approved_plans",
        return_value=[(project.code, rec)],
    ):
        results = project_execution.tick(
            project_loader=_loader,
            runners_for=_boom_runners,
            project_codes=[project.code],
        )

    assert len(results) == 1
    assert results[0].final_status == "approved"
    assert "runners_for failed" in (results[0].error or "")
