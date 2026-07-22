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


# ── Width cap + typed refusal ───────────────────────────────────────────────

def _nine_specs() -> str:
    import json
    return json.dumps([
        {"description": f"part {i}", "output_path": f"drafts/p{i}.md"}
        for i in range(9)
    ])


def _eight_specs() -> str:
    import json
    return json.dumps([
        {"description": f"part {i}", "output_path": f"drafts/p{i}.md"}
        for i in range(8)
    ])


def test_over_cap_split_refuses_typed_with_counts(
    project_with_run, monkeypatch, tmp_path
):
    """Nine valid specs → the ENTIRE split refuses (no children, no silent
    take-8 truncation) with a typed reason naming the count and the cap."""
    from modulatio.orchestration import _DecomposeRefusal
    orch = _make_orchestrator(project_with_run)
    _planner_returns(monkeypatch, _nine_specs())
    parent = _make_task()
    outcome = orch._attempt_decompose(parent, _ctx_err(tmp_path))
    assert isinstance(outcome, _DecomposeRefusal)
    assert "9" in outcome.reason and "8" in outcome.reason


def test_cap_width_split_is_accepted(project_with_run, monkeypatch, tmp_path):
    """Exactly eight children is within the width cap."""
    orch = _make_orchestrator(project_with_run)
    _planner_returns(monkeypatch, _eight_specs())
    parent = _make_task()
    outcome = orch._attempt_decompose(parent, _ctx_err(tmp_path))
    assert isinstance(outcome, list) and len(outcome) == 8


def test_depth_cap_refusal_is_typed(project_with_run, monkeypatch, tmp_path):
    from modulatio.orchestration import _DecomposeRefusal
    orch = _make_orchestrator(project_with_run)
    _planner_returns(monkeypatch, TWO_CHILD_SPLIT)
    parent = _make_task(decompose_depth=3)
    outcome = orch._attempt_decompose(parent, _ctx_err(tmp_path))
    assert isinstance(outcome, _DecomposeRefusal)
    assert "depth" in outcome.reason


def test_single_child_refusal_is_typed(project_with_run, monkeypatch, tmp_path):
    from modulatio.orchestration import _DecomposeRefusal
    orch = _make_orchestrator(project_with_run)
    _planner_returns(monkeypatch, '[{"description":"only","output_path":"a.md"}]')
    parent = _make_task()
    outcome = orch._attempt_decompose(parent, _ctx_err(tmp_path))
    assert isinstance(outcome, _DecomposeRefusal)


def test_refusal_emits_refused_fact_and_no_children_no_producer(
    project_with_run, monkeypatch, tmp_path
):
    """Over-cap atomicity through the run path: zero children persisted, zero
    producer calls, a ``task_decompose_refused`` fact (live + durable audit
    row) and NO ``task_decomposed`` fact."""
    import json as _json
    from modulatio.orchestration import RunSummary
    orch = _make_orchestrator(project_with_run)
    _planner_returns(monkeypatch, _nine_specs())
    events = []
    orch.activity_callback = lambda e: events.append(e)
    calls = {"n": 0}

    def _spy(task, corrective_notes=""):
        calls["n"] += 1
        raise AssertionError("producer must not run on a refused split")

    orch._producer_execute = _spy  # type: ignore[assignment]
    parent = _make_task()
    summary = RunSummary(project=project_with_run)
    handled, refusal = orch._try_decompose_and_run(
        parent, _ctx_err(tmp_path), summary)
    assert handled is False
    assert refusal is not None and "9" in refusal.reason
    assert calls["n"] == 0
    phases = [e.phase for e in events]
    assert "task_decompose_refused" in phases
    assert "task_decomposed" not in phases
    audit = orch._scope_root() / "audit.jsonl"
    rows = [_json.loads(x) for x in audit.read_text().splitlines()]
    refused = [r for r in rows if r.get("event") == "task_decompose_refused"]
    assert len(refused) == 1
    assert refused[0]["task_id"] == parent.id
    assert "9" in refused[0]["reason"]
    assert not [r for r in rows if r.get("event") == "decompose_mint"]


def test_refusal_reason_reaches_stuck_ticket(
    project_with_run, monkeypatch, tmp_path
):
    """The context-budget stuck ticket names the refusal reason — the
    operator sees WHY the split was refused, not just that the task stuck."""
    from modulatio import store
    from modulatio.orchestration import RunSummary
    orch = _make_orchestrator(project_with_run)
    _planner_returns(monkeypatch, _nine_specs())
    parent = _make_task()
    summary = RunSummary(project=project_with_run)
    handled, refusal = orch._try_decompose_and_run(
        parent, _ctx_err(tmp_path), summary)
    assert handled is False
    orch._block_for_context_budget(
        parent, _ctx_err(tmp_path), summary, decompose_refusal=refusal)
    tickets = store.list_tickets(PROJECT_CODE, run_id=project_with_run.run_id)
    assert tickets, "context-budget refusal must open the stuck ticket"
    joined = " ".join(t.body for t in tickets)
    assert "9" in joined and "8" in joined
