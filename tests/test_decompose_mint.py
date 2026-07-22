"""The decompose-mint contract.

A decompose split is a MINT: the parent's remaining budget is consumed by
the split (spend-at-mint), and every child is constructed with exactly one
producer attempt (``lifetime_attempts == max(max_retries, 0)``). The run's
spend is bounded by one-mint-per-node × width 8 × depth 3, not by handing
children a share of the parent's counter — a starved child (zero attempts)
forces QC to author keystone work, which is the pathology this floor fixes.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from modulatio import context_budget, vault
from modulatio.orchestration import Orchestrator
from modulatio.types import Project, Task


PROJECT_CODE = "MNT"


@pytest.fixture
def project_with_run(tmp_path: Path, monkeypatch) -> Project:
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    vault.init_project(PROJECT_CODE, "mint test", "decompose mint contract")
    run_id = "run-mnt-001"
    vault.init_run(PROJECT_CODE, run_id, "decompose mint contract")
    return Project(
        code=PROJECT_CODE,
        name="mint test",
        objective="decompose mint contract",
        leader_model="stub",
        wiki_path=str(tmp_path / PROJECT_CODE.lower()),
        run_id=run_id,
    )


def _make_task(**overrides) -> Task:
    fields = dict(
        id="MNT-T-001",
        project_id=uuid4(),
        goal_id="MNT-G-001",
        description="anything",
        max_retries=3,
    )
    fields.update(overrides)
    return Task(**fields)


def _make_orchestrator(project: Project) -> Orchestrator:
    runner = lambda prompt: "stub"  # noqa: E731 — test stub
    return Orchestrator(
        project,
        runners={
            "leader": runner,
            "planner": runner,
            "drafter": runner,
            "researcher": runner,
            "qc": runner,
        },
    )


def _ctx_err(tmp_path, est=200_000, cap=16_000):
    return context_budget.RecoverableContextError(
        model="m", estimated_tokens=est, max_input_tokens=cap,
        checkpoint_path=tmp_path / "cp.json",
    )


def _planner_returns(monkeypatch, payload: str):
    monkeypatch.setattr(
        Orchestrator, "_run",
        lambda self, role, prompt, **kw: payload,
    )


TWO_CHILD_SPLIT = (
    '[{"description":"part one","output_path":"drafts/one.md"},'
    '{"description":"part two","output_path":"drafts/two.md"}]'
)


# ── The child floor — one attempt per child, every ceiling ──────────────────

@pytest.mark.parametrize("ceiling", [-7, 0, 2, 9, 1_000_000])
def test_every_child_constructed_with_exactly_one_remaining_attempt(
    project_with_run, monkeypatch, tmp_path, ceiling
):
    """Every minted child has ``lifetime_attempts == max(ceiling, 0)`` at
    construction — exactly one remaining attempt under the producer
    arithmetic — for ANY operator ceiling including zero and negative
    misconfiguration. The clamp writes the counter; a raw ``-7`` must not
    mint extra attempts."""
    orch = _make_orchestrator(project_with_run)
    _planner_returns(monkeypatch, TWO_CHILD_SPLIT)
    parent = _make_task(max_retries=ceiling)
    children = orch._attempt_decompose(parent, _ctx_err(tmp_path))
    assert children is not None and len(children) == 2
    clamped = max(ceiling, 0)
    for c in children:
        assert c.lifetime_attempts == clamped
        remaining = max(0, (max(c.max_retries, 0) + 1) - c.lifetime_attempts)
        assert remaining == 1


def test_spent_parent_children_still_get_one_attempt_each(
    project_with_run, monkeypatch, tmp_path
):
    """No stagger: the split's budget bound is one-mint-per-node (spend at
    mint), NOT a share of the parent's remaining counter. A budget-spent
    parent's children each still get their one attempt — a starved child
    would force QC to author the keystone artifact."""
    orch = _make_orchestrator(project_with_run)
    _planner_returns(monkeypatch, TWO_CHILD_SPLIT)
    parent = _make_task(max_retries=3)
    parent.lifetime_attempts = 4  # fully spent under the old shared-counter shape
    children = orch._attempt_decompose(parent, _ctx_err(tmp_path))
    assert children is not None
    for c in children:
        assert c.lifetime_attempts == 3, (
            "child was staggered to the ceiling — the shared-remaining shape "
            "is retired; every child gets exactly one attempt"
        )
