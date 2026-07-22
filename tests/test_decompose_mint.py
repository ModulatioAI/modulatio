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
from modulatio import store as store_mod
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


# ── Artifact-key validation + lineage ───────────────────────────────────────

def test_child_targeting_parent_artifact_refuses(
    project_with_run, monkeypatch, tmp_path
):
    """A child aimed at the PARENT's canonical artifact key refuses the whole
    split — minted budget must never become fresh writes to the same artifact."""
    from modulatio.orchestration import _DecomposeRefusal
    orch = _make_orchestrator(project_with_run)
    _planner_returns(monkeypatch,
        '[{"description":"a","output_path":"drafts/parent.md"},'
        '{"description":"b","output_path":"drafts/other.md"}]')
    parent = _make_task(output_path="drafts/parent.md")
    outcome = orch._attempt_decompose(parent, _ctx_err(tmp_path))
    assert isinstance(outcome, _DecomposeRefusal)
    assert "drafts/parent.md" in outcome.reason


def test_sibling_duplicate_paths_refuse(project_with_run, monkeypatch, tmp_path):
    from modulatio.orchestration import _DecomposeRefusal
    orch = _make_orchestrator(project_with_run)
    _planner_returns(monkeypatch,
        '[{"description":"a","output_path":"drafts/same.md"},'
        '{"description":"b","output_path":"drafts/same.md"}]')
    outcome = orch._attempt_decompose(_make_task(), _ctx_err(tmp_path))
    assert isinstance(outcome, _DecomposeRefusal)
    assert "drafts/same.md" in outcome.reason


def test_sibling_sugared_equivalent_paths_refuse(
    project_with_run, monkeypatch, tmp_path
):
    """Canonicalization catches sugared twins ('drafts/./one.md' vs
    'drafts/one.md') — distinct spellings, one artifact."""
    from modulatio.orchestration import _DecomposeRefusal
    orch = _make_orchestrator(project_with_run)
    _planner_returns(monkeypatch,
        '[{"description":"a","output_path":"drafts/./one.md"},'
        '{"description":"b","output_path":"drafts/one.md"}]')
    outcome = orch._attempt_decompose(_make_task(), _ctx_err(tmp_path))
    assert isinstance(outcome, _DecomposeRefusal)


def test_ancestor_collision_refuses_at_grandchild_depth(
    project_with_run, monkeypatch, tmp_path
):
    """Comparing only the immediate parent is insufficient: a child whose key
    matches ANY decompose ancestor's refuses (engine-owned lineage)."""
    from modulatio.orchestration import _DecomposeRefusal
    orch = _make_orchestrator(project_with_run)
    _planner_returns(monkeypatch,
        '[{"description":"a","output_path":"drafts/grandparent.md"},'
        '{"description":"b","output_path":"drafts/fresh.md"}]')
    parent = _make_task(decompose_depth=1)
    parent.artifact_lineage = ["drafts/grandparent.md"]
    outcome = orch._attempt_decompose(parent, _ctx_err(tmp_path))
    assert isinstance(outcome, _DecomposeRefusal)
    assert "drafts/grandparent.md" in outcome.reason


def test_children_carry_engine_owned_lineage(
    project_with_run, monkeypatch, tmp_path
):
    """Each child's lineage is the parent's lineage plus the parent's own
    canonical key — the durable relation the grandchild check reads."""
    orch = _make_orchestrator(project_with_run)
    _planner_returns(monkeypatch, TWO_CHILD_SPLIT)
    parent = _make_task(output_path="drafts/parent.md", decompose_depth=1)
    parent.artifact_lineage = ["drafts/root.md"]
    children = orch._attempt_decompose(parent, _ctx_err(tmp_path))
    assert isinstance(children, list)
    for c in children:
        assert c.artifact_lineage == ["drafts/root.md", "drafts/parent.md"]


@pytest.mark.parametrize("hostile", [
    "/etc/passwd", "../escape.md", "drafts//x.md", "drafts/.ssh/keys.md",
])
def test_hostile_child_paths_refuse_whole_split(
    project_with_run, monkeypatch, tmp_path, hostile
):
    """Absolute / traversal / empty-component / dotfile shapes take the
    refusal lane — no silent drop or rename of the hostile member, zero
    children."""
    import json as _json
    from modulatio.orchestration import _DecomposeRefusal
    orch = _make_orchestrator(project_with_run)
    _planner_returns(monkeypatch, _json.dumps([
        {"description": "a", "output_path": hostile},
        {"description": "b", "output_path": "drafts/clean.md"},
    ]))
    outcome = orch._attempt_decompose(_make_task(), _ctx_err(tmp_path))
    assert isinstance(outcome, _DecomposeRefusal)


def test_declared_task_target_collision_refuses(
    project_with_run, monkeypatch, tmp_path
):
    """A child key colliding with an already-declared task's target refuses —
    the reservation authority sees every declared task, not just the split."""
    from modulatio import store
    from modulatio.orchestration import _DecomposeRefusal
    orch = _make_orchestrator(project_with_run)
    other = _make_task(id="MNT-T-777", output_path="drafts/taken.md")
    store.save_task(PROJECT_CODE, other, run_id=project_with_run.run_id)
    _planner_returns(monkeypatch,
        '[{"description":"a","output_path":"drafts/taken.md"},'
        '{"description":"b","output_path":"drafts/free.md"}]')
    outcome = orch._attempt_decompose(_make_task(), _ctx_err(tmp_path))
    assert isinstance(outcome, _DecomposeRefusal)
    assert "drafts/taken.md" in outcome.reason


def test_fallback_path_children_get_distinct_canonical_keys(
    project_with_run, monkeypatch, tmp_path
):
    """Children with NO explicit output_path key at their engine fallback
    (drafts/<id>.<ext>) — distinct per child id, so a plain no-path split is
    accepted and still lineage-stamped."""
    orch = _make_orchestrator(project_with_run)
    _planner_returns(monkeypatch,
        '[{"description":"a"},{"description":"b"}]')
    parent = _make_task()
    children = orch._attempt_decompose(parent, _ctx_err(tmp_path))
    assert isinstance(children, list) and len(children) == 2


# ── Path reservation — one lock/registry authority ──────────────────────────

def test_concurrent_decompose_same_key_exactly_one_wins(
    project_with_run, monkeypatch, tmp_path
):
    """Two concurrent decompositions targeting the same child key: the ONE
    registry authority serializes the check, so exactly one split validates
    and the other refuses — never two owners for one artifact."""
    import threading
    from modulatio.orchestration import _DecomposeRefusal
    orch = _make_orchestrator(project_with_run)
    _planner_returns(monkeypatch,
        '[{"description":"a","output_path":"drafts/contested.md"},'
        '{"description":"b","output_path":"drafts/uncontested-%ID%.md"}]')

    real_run = Orchestrator._run

    def _per_parent(self, role, prompt, **kw):
        payload = real_run(self, role, prompt, **kw)
        return payload.replace("%ID%", kw.get("task_id", "x"))

    monkeypatch.setattr(Orchestrator, "_run", _per_parent)
    parents = [
        _make_task(id="MNT-T-A"),
        _make_task(id="MNT-T-B"),
    ]
    results = {}
    barrier = threading.Barrier(2)

    def _go(p):
        barrier.wait()
        results[p.id] = orch._attempt_decompose(p, _ctx_err(tmp_path))

    threads = [threading.Thread(target=_go, args=(p,)) for p in parents]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    kinds = sorted(type(v).__name__ for v in results.values())
    assert kinds == ["_DecomposeRefusal", "list"], (
        f"exactly one split must win the contested key; got {kinds}"
    )
    refused = [v for v in results.values() if isinstance(v, _DecomposeRefusal)]
    assert "drafts/contested.md" in refused[0].reason


def test_refused_split_reserves_nothing(
    project_with_run, monkeypatch, tmp_path
):
    """A refused validation leaves the registry untouched — a later split may
    claim the same keys."""
    orch = _make_orchestrator(project_with_run)
    _planner_returns(monkeypatch,
        '[{"description":"a","output_path":"drafts/x.md"},'
        '{"description":"b","output_path":"drafts/x.md"}]')  # sibling dup → refuse
    first = orch._attempt_decompose(_make_task(id="MNT-T-A"), _ctx_err(tmp_path))
    assert not isinstance(first, list)
    _planner_returns(monkeypatch,
        '[{"description":"a","output_path":"drafts/x.md"},'
        '{"description":"b","output_path":"drafts/y.md"}]')
    second = orch._attempt_decompose(_make_task(id="MNT-T-B"), _ctx_err(tmp_path))
    assert isinstance(second, list), (
        "a refused split must not leave tentative reservations behind"
    )


def test_reservations_released_after_split_settles(
    project_with_run, monkeypatch, tmp_path
):
    """After _try_decompose_and_run settles the split (children persisted as
    declared tasks), the in-memory tentative reservations are released — the
    declared tasks themselves are the durable authority."""
    from modulatio.orchestration import RunSummary
    from modulatio.types import TaskStatus
    orch = _make_orchestrator(project_with_run)
    _planner_returns(monkeypatch, TWO_CHILD_SPLIT)
    monkeypatch.setattr(Orchestrator, "_run_task_with_redo",
        lambda self, t, summary, **kw: setattr(t, "status", TaskStatus.COMPLETED))
    parent = _make_task()
    summary = RunSummary(project=project_with_run)
    handled, refusal = orch._try_decompose_and_run(
        parent, _ctx_err(tmp_path), summary)
    assert handled is True
    assert orch._decompose_reservations == {}


# ── The mint WAL — one atomic parent commit ─────────────────────────────────

def _run_split(orch, monkeypatch, project, parent, tmp_path, payload=None):
    from modulatio.orchestration import RunSummary
    from modulatio.types import TaskStatus
    _planner_returns(monkeypatch, payload or TWO_CHILD_SPLIT)
    monkeypatch.setattr(Orchestrator, "_run_task_with_redo",
        lambda self, t, summary, **kw: setattr(t, "status", TaskStatus.COMPLETED))
    summary = RunSummary(project=project)
    return orch._try_decompose_and_run(parent, _ctx_err(tmp_path), summary)


def test_mint_commits_marker_spend_and_record_in_one_parent_write(
    project_with_run, monkeypatch, tmp_path
):
    """The committed parent — RELOADED from the store — carries the mint
    record (stable mint_id, full lossless child descriptors, reservations,
    depth, cap, captured pre-burn remaining) AND the spent counter together.
    The commit is the mint; no durable marked-but-unspent state exists."""
    from modulatio import store
    orch = _make_orchestrator(project_with_run)
    parent = _make_task(max_retries=9)  # unspent: remaining 10
    store.save_task(PROJECT_CODE, parent, run_id=project_with_run.run_id)
    handled, _ = _run_split(orch, monkeypatch, project_with_run, parent, tmp_path)
    assert handled is True
    stored = {
        t.id: t for t in store.list_tasks(
            PROJECT_CODE, run_id=project_with_run.run_id)
    }
    reloaded = stored[parent.id]
    rec = reloaded.decompose_mint
    assert rec is not None, "the durable parent must carry the mint record"
    assert rec.mint_id
    assert rec.parent_remaining_was == 10
    assert reloaded.lifetime_attempts == 10  # spent at the same boundary
    ids = [d["id"] for d in rec.child_descriptors]
    assert ids == [f"{parent.id}-D1", f"{parent.id}-D2"]
    # Lossless: descriptors alone must reconstruct the children.
    d0 = rec.child_descriptors[0]
    assert d0["description"] == "part one"
    assert d0["output_path"] == "drafts/one.md"
    assert d0["artifact_lineage"]
    assert rec.reservations == ["drafts/one.md", "drafts/two.md"]


def test_crash_before_first_child_leaves_complete_committed_record(
    project_with_run, monkeypatch, tmp_path
):
    """Die between the parent
    commit and the first child save — the reloaded parent alone carries
    everything recovery needs (marker, descriptors, reservations, spent
    lifetime), and no child files exist."""
    from modulatio import store
    from modulatio.orchestration import RunSummary
    orch = _make_orchestrator(project_with_run)
    parent = _make_task()
    store.save_task(PROJECT_CODE, parent, run_id=project_with_run.run_id)
    _planner_returns(monkeypatch, TWO_CHILD_SPLIT)

    def _die(self, child):
        raise RuntimeError("process death injected before child save")

    monkeypatch.setattr(Orchestrator, "_persist_mint_child_barrier", _die)
    summary = RunSummary(project=project_with_run)
    with pytest.raises(RuntimeError):
        orch._try_decompose_and_run(parent, _ctx_err(tmp_path), summary)
    stored = {
        t.id: t for t in store.list_tasks(
            PROJECT_CODE, run_id=project_with_run.run_id)
    }
    reloaded = stored[parent.id]
    assert reloaded.decompose_mint is not None
    assert reloaded.lifetime_attempts == max(parent.max_retries, 0) + 1
    assert len(reloaded.decompose_mint.child_descriptors) == 2
    assert f"{parent.id}-D1" not in stored  # no child files yet — record owns them


def test_refusal_writes_no_mint_record(project_with_run, monkeypatch, tmp_path):
    """Refuse path: zero durable mint state on the parent."""
    from modulatio import store
    from modulatio.orchestration import RunSummary
    orch = _make_orchestrator(project_with_run)
    parent = _make_task()
    store.save_task(PROJECT_CODE, parent, run_id=project_with_run.run_id)
    _planner_returns(monkeypatch, _nine_specs())
    summary = RunSummary(project=project_with_run)
    handled, refusal = orch._try_decompose_and_run(
        parent, _ctx_err(tmp_path), summary)
    assert handled is False
    stored = {
        t.id: t for t in store.list_tasks(
            PROJECT_CODE, run_id=project_with_run.run_id)
    }
    assert stored[parent.id].decompose_mint is None


def test_parent_commit_failure_releases_reservations_and_refuses(
    project_with_run, monkeypatch, tmp_path
):
    """The atomic parent save raises → no
    mint record, no children, and every tentative reservation released so a
    subsequent decomposition can claim the keys."""
    from modulatio.orchestration import RunSummary, _DecomposeRefusal
    orch = _make_orchestrator(project_with_run)
    parent = _make_task(id="MNT-T-FAIL")
    _planner_returns(monkeypatch, TWO_CHILD_SPLIT)

    def _fail_save(self, task):
        raise OSError("disk full injected at parent commit")

    orig_save = Orchestrator._persist_mint_parent_barrier
    monkeypatch.setattr(Orchestrator, "_persist_mint_parent_barrier", _fail_save)
    summary = RunSummary(project=project_with_run)
    handled, refusal = orch._try_decompose_and_run(
        parent, _ctx_err(tmp_path), summary)
    assert handled is False and isinstance(refusal, _DecomposeRefusal)
    assert parent.decompose_mint is None
    assert parent.lifetime_attempts == 0  # in-memory spend reverted
    assert orch._decompose_reservations == {}
    monkeypatch.setattr(Orchestrator, "_persist_mint_parent_barrier", orig_save)
    _planner_returns(monkeypatch, TWO_CHILD_SPLIT)
    retry = orch._attempt_decompose(
        _make_task(id="MNT-T-NEXT"), _ctx_err(tmp_path))
    assert isinstance(retry, list), (
        "keys must be claimable after a failed commit released them"
    )


# ── Idempotent materialization + recovery routing ───────────────────────────

def _committed_parent_no_children(orch, monkeypatch, project, tmp_path):
    """A parent with a committed mint record and ZERO child files — the
    pre-child crash state recovery must finish from the record alone."""
    from modulatio import store
    from modulatio.orchestration import RunSummary
    parent = _make_task()
    store.save_task(PROJECT_CODE, parent, run_id=project.run_id)
    _planner_returns(monkeypatch, TWO_CHILD_SPLIT)

    def _die(self, child):
        raise RuntimeError("crash injected before child save")

    orig_persist = Orchestrator._persist_mint_child_barrier
    monkeypatch.setattr(Orchestrator, "_persist_mint_child_barrier", _die)
    with pytest.raises(RuntimeError):
        orch._try_decompose_and_run(
            parent, _ctx_err(tmp_path), RunSummary(project=project))
    monkeypatch.setattr(Orchestrator, "_persist_mint_child_barrier", orig_persist)
    stored = {
        t.id: t for t in store.list_tasks(PROJECT_CODE, run_id=project.run_id)
    }
    return stored[parent.id]


def test_recovery_materializes_children_from_record_without_replanning(
    project_with_run, monkeypatch, tmp_path
):
    """re-entering the marked parent through the
    real redo path creates every child from the committed record — planner
    never consulted again, no second mint id, no parent producer call."""
    from modulatio import store
    from modulatio.orchestration import RunSummary
    orch = _make_orchestrator(project_with_run)
    reloaded = _committed_parent_no_children(
        orch, monkeypatch, project_with_run, tmp_path)
    original_mint_id = reloaded.decompose_mint.mint_id
    roles_called = []

    def _record_run(self, role, prompt, **kw):
        roles_called.append(role)
        return "stub"

    monkeypatch.setattr(Orchestrator, "_run", _record_run)
    producer_calls = []

    def _spy(self, task, corrective_notes=""):
        producer_calls.append(task.id)
        task.lifetime_attempts += 1
        path = orch._resolve_draft_path(task)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("draft")
        return path, "sum", 10

    monkeypatch.setattr(Orchestrator, "_producer_execute", _spy)
    monkeypatch.setenv("MODULATIO_QC_FIXER", "0")
    orch._run_task_with_redo(reloaded, RunSummary(project=project_with_run))
    assert "planner" not in roles_called, "recovery must not re-plan the split"
    stored = {
        t.id: t for t in store.list_tasks(
            PROJECT_CODE, run_id=project_with_run.run_id)
    }
    assert f"{reloaded.id}-D1" in stored and f"{reloaded.id}-D2" in stored
    assert reloaded.id not in producer_calls, (
        "a minted container must never reach the producer loop"
    )
    assert stored[reloaded.id].decompose_mint.mint_id == original_mint_id


def test_partial_materialization_preserves_existing_children(
    project_with_run, monkeypatch, tmp_path
):
    """Child D1 already exists COMPLETED —
    recovery creates only D2, never overwrites D1's state, and D1 takes zero
    additional producer calls."""
    from modulatio import store
    from modulatio.orchestration import RunSummary
    from modulatio.types import Task, TaskStatus
    orch = _make_orchestrator(project_with_run)
    reloaded = _committed_parent_no_children(
        orch, monkeypatch, project_with_run, tmp_path)
    rec = reloaded.decompose_mint
    d1 = Task.model_validate(rec.child_descriptors[0])
    d1.status = TaskStatus.COMPLETED
    store.save_task(PROJECT_CODE, d1, run_id=project_with_run.run_id)
    producer_calls = []

    def _spy(self, task, corrective_notes=""):
        producer_calls.append(task.id)
        task.lifetime_attempts += 1
        path = orch._resolve_draft_path(task)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("draft")
        return path, "sum", 10

    monkeypatch.setattr(Orchestrator, "_producer_execute", _spy)
    monkeypatch.setenv("MODULATIO_QC_FIXER", "0")
    orch._run_task_with_redo(reloaded, RunSummary(project=project_with_run))
    stored = {
        t.id: t for t in store.list_tasks(
            PROJECT_CODE, run_id=project_with_run.run_id)
    }
    assert stored[d1.id].status is TaskStatus.COMPLETED
    assert d1.id not in producer_calls, (
        "a completed child must receive zero additional producer calls"
    )
    assert f"{reloaded.id}-D2" in producer_calls


def test_planted_child_with_foreign_authority_is_typed_conflict(
    project_with_run, monkeypatch, tmp_path
):
    """An existing child id under a different
    mint authority → typed engine conflict, no overwrite."""
    from modulatio import store
    from modulatio.orchestration import DecomposeMintConflict
    from modulatio.types import Task
    orch = _make_orchestrator(project_with_run)
    reloaded = _committed_parent_no_children(
        orch, monkeypatch, project_with_run, tmp_path)
    rec = reloaded.decompose_mint
    foreign = Task.model_validate(rec.child_descriptors[0])
    foreign.minted_by = "some-other-mint"
    foreign.description = "foreign payload"
    store.save_task(PROJECT_CODE, foreign, run_id=project_with_run.run_id)
    with pytest.raises(DecomposeMintConflict):
        orch._materialize_mint_children(reloaded)
    stored = {
        t.id: t for t in store.list_tasks(
            PROJECT_CODE, run_id=project_with_run.run_id)
    }
    assert stored[foreign.id].description == "foreign payload", (
        "conflict must not overwrite the existing child"
    )


def test_marked_parent_cannot_mint_again(
    project_with_run, monkeypatch, tmp_path
):
    """One mint per node: _attempt_decompose on a marked parent refuses —
    belt (marker) under the suspenders (spent arithmetic + routing)."""
    from modulatio.orchestration import _DecomposeRefusal
    from modulatio.types import DecomposeMintRecord
    orch = _make_orchestrator(project_with_run)
    _planner_returns(monkeypatch, TWO_CHILD_SPLIT)
    parent = _make_task()
    parent.decompose_mint = DecomposeMintRecord(mint_id="m-1")
    outcome = orch._attempt_decompose(parent, _ctx_err(tmp_path))
    assert isinstance(outcome, _DecomposeRefusal)
    assert "mint" in outcome.reason


def test_prepared_state_alone_is_sufficient_for_recovery(
    project_with_run, monkeypatch, tmp_path
):
    """The optional children_materialized advance is convenience-only —
    recovery works from ``prepared`` + descriptors alone."""
    from modulatio.orchestration import RunSummary
    from modulatio.types import TaskStatus
    orch = _make_orchestrator(project_with_run)
    reloaded = _committed_parent_no_children(
        orch, monkeypatch, project_with_run, tmp_path)
    assert reloaded.decompose_mint.state == "prepared"
    monkeypatch.setattr(Orchestrator, "_run_task_with_redo_inner",
        lambda self, t, summary, *a, **kw: setattr(
            t, "status", TaskStatus.COMPLETED))
    orch._resume_decompose_mint(reloaded, RunSummary(project=project_with_run))
    assert reloaded.status is TaskStatus.COMPLETED


# ── The decompose_mint disclosure fact ──────────────────────────────────────

def test_mint_fact_carries_stable_id_and_real_arithmetic(
    project_with_run, monkeypatch, tmp_path
):
    """One durable ``decompose_mint`` row per mint: stable mint_id from the
    committed record, actual child count, cap, depth, and the pre-burn parent
    remaining — audit evidence with real arithmetic."""
    import json as _json
    from modulatio import store
    orch = _make_orchestrator(project_with_run)
    events = []
    orch.activity_callback = lambda e: events.append(e)
    parent = _make_task(max_retries=9)
    store.save_task(PROJECT_CODE, parent, run_id=project_with_run.run_id)
    handled, _ = _run_split(orch, monkeypatch, project_with_run, parent, tmp_path)
    assert handled is True
    audit = orch._scope_root() / "audit.jsonl"
    rows = [_json.loads(x) for x in audit.read_text().splitlines()]
    mints = [r for r in rows if r.get("event") == "decompose_mint"]
    assert len(mints) == 1
    assert mints[0]["mint_id"] == parent.decompose_mint.mint_id
    assert mints[0]["task_id"] == parent.id
    assert mints[0]["children"] == 2
    assert mints[0]["cap"] == 8
    assert mints[0]["parent_remaining_was"] == 10
    assert "decompose_mint" in [e.phase for e in events]


def test_same_process_replay_emits_one_mint_row(
    project_with_run, monkeypatch, tmp_path
):
    """Re-entering the minted parent in the same process does not double the
    mint fact — the emitter dedups by stable id."""
    import json as _json
    from modulatio import store
    from modulatio.orchestration import RunSummary
    orch = _make_orchestrator(project_with_run)
    parent = _make_task()
    store.save_task(PROJECT_CODE, parent, run_id=project_with_run.run_id)
    handled, _ = _run_split(orch, monkeypatch, project_with_run, parent, tmp_path)
    assert handled is True
    orch._resume_decompose_mint(parent, RunSummary(project=project_with_run))
    audit = orch._scope_root() / "audit.jsonl"
    rows = [_json.loads(x) for x in audit.read_text().splitlines()]
    mints = [r for r in rows if r.get("event") == "decompose_mint"]
    assert len(mints) == 1


def test_restart_replay_counts_one_unique_mint_id(
    project_with_run, monkeypatch, tmp_path
):
    """A fresh process may re-emit the committed mint (its dedup set is
    empty); consumers count UNIQUE mint ids, so crash replay cannot inflate
    the claimed mint count."""
    import json as _json
    from modulatio import store
    from modulatio.orchestration import RunSummary
    orch = _make_orchestrator(project_with_run)
    parent = _make_task()
    store.save_task(PROJECT_CODE, parent, run_id=project_with_run.run_id)
    handled, _ = _run_split(orch, monkeypatch, project_with_run, parent, tmp_path)
    assert handled is True
    fresh = _make_orchestrator(project_with_run)  # restart: empty dedup set
    reloaded = {
        t.id: t for t in store.list_tasks(
            PROJECT_CODE, run_id=project_with_run.run_id)
    }[parent.id]
    fresh._resume_decompose_mint(reloaded, RunSummary(project=project_with_run))
    audit = fresh._scope_root() / "audit.jsonl"
    rows = [_json.loads(x) for x in audit.read_text().splitlines()]
    mints = [r for r in rows if r.get("event") == "decompose_mint"]
    unique = {r["mint_id"] for r in mints}
    assert len(unique) == 1, (
        "replay may re-deliver, but there is ONE logical mint fact per id"
    )


# ── Durable attempt claim + three-state recovery ────────────────────────────

def test_claim_persisted_durably_before_producer_byte(
    project_with_run, monkeypatch, tmp_path
):
    """At the moment the producer's model call
    fires for a minted child, the child FILE already carries the stable
    attempt id and the spent lifetime — the claim is durable before any
    producer byte."""
    from modulatio import store
    from modulatio.orchestration import RunSummary
    orch = _make_orchestrator(project_with_run)
    reloaded = _committed_parent_no_children(
        orch, monkeypatch, project_with_run, tmp_path)
    observed = {}

    def _model_call(self, agent_id, role, prompt, **kw):
        if role == self.default_producer_role and "producer" not in observed:
            child_id = f"{reloaded.id}-D1"
            stored = {
                t.id: t for t in store.list_tasks(
                    PROJECT_CODE, run_id=project_with_run.run_id)
            }
            child = stored.get(child_id)
            observed["producer"] = {
                "claimed": child is not None
                and child.attempt_claim_id is not None,
                "lifetime": None if child is None else child.lifetime_attempts,
            }
        return "stub draft"

    monkeypatch.setattr(Orchestrator, "_run_agent_call", _model_call)
    monkeypatch.setattr(
        Orchestrator, "_run", lambda self, role, prompt, **kw: "stub")
    monkeypatch.setenv("MODULATIO_QC_FIXER", "0")
    orch._run_task_with_redo(reloaded, RunSummary(project=project_with_run))
    assert observed.get("producer", {}).get("claimed") is True, (
        f"the durable claim must precede the producer byte: {observed}"
    )
    assert observed["producer"]["lifetime"] == max(reloaded.max_retries, 0) + 1


def test_claim_is_the_single_increment(
    project_with_run, monkeypatch, tmp_path
):
    """Claim + producer seam must not BOTH bump the
    counter — after a minted child's one attempt, lifetime is exactly
    retry_budget + 1."""
    from modulatio import store
    from modulatio.orchestration import RunSummary
    orch = _make_orchestrator(project_with_run)
    reloaded = _committed_parent_no_children(
        orch, monkeypatch, project_with_run, tmp_path)
    monkeypatch.setattr(
        Orchestrator, "_run_agent_call",
        lambda self, agent_id, role, prompt, **kw: "stub draft")
    monkeypatch.setattr(
        Orchestrator, "_run", lambda self, role, prompt, **kw: "stub")
    monkeypatch.setenv("MODULATIO_QC_FIXER", "0")
    orch._run_task_with_redo(reloaded, RunSummary(project=project_with_run))
    stored = {
        t.id: t for t in store.list_tasks(
            PROJECT_CODE, run_id=project_with_run.run_id)
    }
    for cid in (f"{reloaded.id}-D1", f"{reloaded.id}-D2"):
        child = stored[cid]
        assert child.attempt_claim_id is not None
        assert child.lifetime_attempts == max(child.max_retries, 0) + 1, (
            f"{cid}: claim must BE the increment — got "
            f"{child.lifetime_attempts}"
        )


def test_ordinary_task_never_claims_and_counts_per_attempt(
    project_with_run, monkeypatch, tmp_path
):
    """A non-minted task keeps its exact prior
    behavior — in-memory increment per attempt, no claim id, no extra
    persist."""
    from modulatio.orchestration import RunSummary
    orch = _make_orchestrator(project_with_run)
    calls = {"n": 0}

    def _model_call(self, agent_id, role, prompt, **kw):
        if role == self.default_producer_role:
            calls["n"] += 1
        return "stub draft"

    monkeypatch.setattr(Orchestrator, "_run_agent_call", _model_call)
    monkeypatch.setattr(
        Orchestrator, "_run", lambda self, role, prompt, **kw: "stub")
    monkeypatch.setenv("MODULATIO_QC_FIXER", "0")
    task = _make_task(id="MNT-T-ORD", max_retries=1)
    orch._run_task_with_redo(task, RunSummary(project=project_with_run))
    assert task.attempt_claim_id is None
    assert calls["n"] >= 1
    assert task.lifetime_attempts == calls["n"], (
        "ordinary counting must stay one in-memory increment per attempt"
    )


def _plant_child(project, parent, idx, **overrides):
    from modulatio import store
    from modulatio.types import Task
    d = dict(parent.decompose_mint.child_descriptors[idx])
    d.update(overrides)
    child = Task.model_validate(d)
    store.save_task(PROJECT_CODE, child, run_id=project.run_id)
    return child


def test_claimed_interrupted_child_without_draft_takes_stuck_lane(
    project_with_run, monkeypatch, tmp_path
):
    """Claimed + nonterminal + no durable draft →
    recovery makes ZERO producer calls for that child and settles it through
    the typed interrupted lane — the attempt is consumed, never refunded."""
    from modulatio import store
    from modulatio.orchestration import RunSummary
    from modulatio.types import TaskStatus
    orch = _make_orchestrator(project_with_run)
    reloaded = _committed_parent_no_children(
        orch, monkeypatch, project_with_run, tmp_path)
    clamp = max(reloaded.max_retries, 0)
    interrupted = _plant_child(
        project_with_run, reloaded, 0,
        attempt_claim_id="claim-1", lifetime_attempts=clamp + 1)
    producer_calls = []

    def _spy(self, task, corrective_notes=""):
        producer_calls.append(task.id)
        task.lifetime_attempts += 1
        path = orch._resolve_draft_path(task)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("draft")
        return path, "sum", 10

    monkeypatch.setattr(Orchestrator, "_producer_execute", _spy)
    monkeypatch.setenv("MODULATIO_QC_FIXER", "0")
    orch._run_task_with_redo(reloaded, RunSummary(project=project_with_run))
    assert interrupted.id not in producer_calls, (
        "a claimed attempt must never re-run the producer"
    )
    stored = {
        t.id: t for t in store.list_tasks(
            PROJECT_CODE, run_id=project_with_run.run_id)
    }
    settled = stored[interrupted.id]
    assert settled.status is TaskStatus.BLOCKED
    assert any(
        "interrupted" in tr.rationale for tr in settled.transitions
    ), "the stuck lane must name the interrupted claimed attempt"


def test_claimed_interrupted_child_with_draft_never_reruns_producer(
    project_with_run, monkeypatch, tmp_path
):
    """Claimed + nonterminal + durable draft
    on disk → recovery may only QC the draft; zero producer calls."""
    from modulatio.orchestration import RunSummary
    orch = _make_orchestrator(project_with_run)
    reloaded = _committed_parent_no_children(
        orch, monkeypatch, project_with_run, tmp_path)
    clamp = max(reloaded.max_retries, 0)
    interrupted = _plant_child(
        project_with_run, reloaded, 0,
        attempt_claim_id="claim-1", lifetime_attempts=clamp + 1)
    draft = orch._shared_artifacts_root() / "drafts" / "one.md"
    draft.parent.mkdir(parents=True, exist_ok=True)
    draft.write_text("half-finished draft from the interrupted attempt")
    producer_calls = []

    def _spy(self, task, corrective_notes=""):
        producer_calls.append(task.id)
        task.lifetime_attempts += 1
        path = orch._resolve_draft_path(task)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("draft")
        return path, "sum", 10

    monkeypatch.setattr(Orchestrator, "_producer_execute", _spy)
    monkeypatch.setenv("MODULATIO_QC_FIXER", "0")
    orch._run_task_with_redo(reloaded, RunSummary(project=project_with_run))
    assert interrupted.id not in producer_calls, (
        "an interrupted claimed attempt settles by QC-on-draft or stuck — "
        "never by re-running the producer"
    )


def test_recovered_unclaimed_child_is_claimed_on_its_single_run(
    project_with_run, monkeypatch, tmp_path
):
    """A materialized-but-unclaimed child claims and runs
    exactly once on recovery."""
    from modulatio import store
    from modulatio.orchestration import RunSummary
    orch = _make_orchestrator(project_with_run)
    reloaded = _committed_parent_no_children(
        orch, monkeypatch, project_with_run, tmp_path)
    monkeypatch.setattr(
        Orchestrator, "_run_agent_call",
        lambda self, agent_id, role, prompt, **kw: "stub draft")
    monkeypatch.setattr(
        Orchestrator, "_run", lambda self, role, prompt, **kw: "stub")
    monkeypatch.setenv("MODULATIO_QC_FIXER", "0")
    orch._run_task_with_redo(reloaded, RunSummary(project=project_with_run))
    stored = {
        t.id: t for t in store.list_tasks(
            PROJECT_CODE, run_id=project_with_run.run_id)
    }
    child = stored[f"{reloaded.id}-D1"]
    assert child.attempt_claim_id is not None
    assert child.lifetime_attempts == max(child.max_retries, 0) + 1


# ── The full-tree bound — 584 per root at default policy ────────────────────

def test_full_tree_bound_584_child_attempts_no_split_below_depth_3(
    project_with_run, monkeypatch, tmp_path
):
    """The frozen worst case: an always-8, always-overflowing tree at
    default policy (max_retries=0) yields EXACTLY 584 child producer
    attempts per root (8 + 64 + 512), splits stop at depth 3, and the mint
    count is 73 UNIQUE mint ids (1 + 8 + 64) — counting mint events and
    producer calls, not final task count."""
    import json as _json
    from modulatio.orchestration import RunSummary
    orch = _make_orchestrator(project_with_run)

    def _split_specs(self, role, prompt, **kw):
        tid = kw.get("task_id", "x")
        return _json.dumps([
            {"description": f"part {i} of {tid}",
             "output_path": f"drafts/{tid}-p{i}.md"}
            for i in range(8)
        ])

    monkeypatch.setattr(Orchestrator, "_run", _split_specs)
    attempts = {"root": 0, "child": 0}

    def _overflowing_producer(self, task, corrective_notes=""):
        which = "child" if task.minted_by is not None else "root"
        attempts[which] += 1
        task.lifetime_attempts += 1
        raise context_budget.RecoverableContextError(
            model="m", estimated_tokens=999_999, max_input_tokens=1,
            checkpoint_path=tmp_path / f"cp-{task.id}.json",
        )

    monkeypatch.setattr(Orchestrator, "_producer_execute", _overflowing_producer)
    monkeypatch.setenv("MODULATIO_QC_FIXER", "0")
    root = _make_task(max_retries=0)
    orch._run_task_with_redo(root, RunSummary(project=project_with_run))

    assert attempts["child"] == 584, (
        f"the per-root bound is exactly 584 child attempts; "
        f"got {attempts['child']}"
    )
    assert attempts["root"] == 1
    audit = orch._scope_root() / "audit.jsonl"
    rows = [_json.loads(x) for x in audit.read_text().splitlines()]
    mint_ids = {r["mint_id"] for r in rows if r.get("event") == "decompose_mint"}
    assert len(mint_ids) == 73, (
        f"1 root + 8 + 64 = 73 unique mints; got {len(mint_ids)}"
    )
    refused = [r for r in rows if r.get("event") == "task_decompose_refused"]
    assert len(refused) == 512, (
        "every depth-3 leaf must refuse (depth cap), typed and audited"
    )
    assert all("depth" in r["reason"] for r in refused)
    from modulatio import store
    deep = [
        t.id for t in store.list_tasks(
            PROJECT_CODE, run_id=project_with_run.run_id)
        if t.id.count("-D") > 3
    ]
    assert deep == [], f"no split below depth 3, but found {deep[:3]}"


# ── Declared-key index (the O(n²) scan fix under the mint validator) ────────

def test_declared_artifact_keys_tracks_saves_updates_and_deletes(
    project_with_run
):
    """store.declared_artifact_keys: key → task id for every declared task,
    cached by mtime+size so repeated scans parse only changed files — and it
    must observe saves, updates, and deletions immediately."""
    from modulatio import store
    run_id = project_with_run.run_id
    a = _make_task(id="MNT-T-K1", output_path="drafts/k1.md")
    store.save_task(PROJECT_CODE, a, run_id=run_id)
    keys = store.declared_artifact_keys(PROJECT_CODE, run_id=run_id)
    assert keys["drafts/k1.md"] == "MNT-T-K1"
    a.output_path = "drafts/k1-moved.md"
    store.save_task(PROJECT_CODE, a, run_id=run_id)
    keys = store.declared_artifact_keys(PROJECT_CODE, run_id=run_id)
    assert "drafts/k1.md" not in keys
    assert keys["drafts/k1-moved.md"] == "MNT-T-K1"
    import os
    os.remove(store._task_path(PROJECT_CODE, a.id, run_id=run_id))
    keys = store.declared_artifact_keys(PROJECT_CODE, run_id=run_id)
    assert "drafts/k1-moved.md" not in keys


# ── Durability barriers, sealed seam, monotonic merge ───────────────────────

def _enter_worker_tls(orch):
    """Flip the orchestrator into the isolated-worker deferral posture the
    concurrent wave uses — deferred store writes + buffered children."""
    orch._tls.deferred_writes = []
    orch._tls.child_tasks = []


def test_wal_commit_is_durable_under_worker_deferral(
    project_with_run, monkeypatch, tmp_path
):
    """The parent mint commit is a DURABILITY BARRIER — even
    inside an isolated worker (deferral active), the record + spend are on
    disk before the commit returns."""
    from modulatio import store
    orch = _make_orchestrator(project_with_run)
    parent = _make_task()
    store.save_task(PROJECT_CODE, parent, run_id=project_with_run.run_id)
    _planner_returns(monkeypatch, TWO_CHILD_SPLIT)
    children = orch._attempt_decompose(parent, _ctx_err(tmp_path))
    assert isinstance(children, list)
    _enter_worker_tls(orch)
    try:
        failure = orch._commit_decompose_mint(parent, children)
        assert failure is None
        on_disk = store.get_task(
            PROJECT_CODE, parent.id, run_id=project_with_run.run_id)
        assert on_disk.decompose_mint is not None, (
            "worker deferral must not void the WAL barrier"
        )
        assert on_disk.lifetime_attempts == max(parent.max_retries, 0) + 1
    finally:
        orch._tls.deferred_writes = None
        orch._tls.child_tasks = None


def test_claim_is_durable_under_worker_deferral(
    project_with_run, monkeypatch, tmp_path
):
    """The attempt claim is a barrier too — durable before the
    producer byte even inside a worker."""
    from modulatio import store
    orch = _make_orchestrator(project_with_run)
    child = _make_task(id="MNT-T-001-D1", minted_by="mint-1")
    child.lifetime_attempts = max(child.max_retries, 0)
    store.save_task(PROJECT_CODE, child, run_id=project_with_run.run_id)
    _enter_worker_tls(orch)
    try:
        orch._claim_and_count(child)
        on_disk = store.get_task(
            PROJECT_CODE, child.id, run_id=project_with_run.run_id)
        assert on_disk.attempt_claim_id == child.attempt_claim_id
        assert on_disk.attempt_claim_id is not None
        assert on_disk.lifetime_attempts == max(child.max_retries, 0) + 1
    finally:
        orch._tls.deferred_writes = None
        orch._tls.child_tasks = None


def test_commit_failure_under_worker_tls_fails_closed(
    project_with_run, monkeypatch, tmp_path
):
    """The barrier save raising under worker TLS →
    refusal, zero mint event, zero children, spend reverted, reservations
    released."""
    import json as _json
    from modulatio.orchestration import RunSummary, _DecomposeRefusal
    orch = _make_orchestrator(project_with_run)
    parent = _make_task()
    _planner_returns(monkeypatch, TWO_CHILD_SPLIT)
    monkeypatch.setattr(
        store_mod, "save_task",
        lambda *a, **kw: (_ for _ in ()).throw(OSError("disk full")))
    _enter_worker_tls(orch)
    try:
        handled, refusal = orch._try_decompose_and_run(
            parent, _ctx_err(tmp_path), RunSummary(project=project_with_run))
    finally:
        orch._tls.deferred_writes = None
        orch._tls.child_tasks = None
    assert handled is False and isinstance(refusal, _DecomposeRefusal)
    assert parent.decompose_mint is None
    assert parent.lifetime_attempts == 0
    assert orch._decompose_reservations == {}
    audit = orch._scope_root() / "audit.jsonl"
    if audit.exists():
        rows = [_json.loads(x) for x in audit.read_text().splitlines()]
        assert not [r for r in rows if r.get("event") == "decompose_mint"]


def test_seam_raises_typed_on_claimed_minted_child(
    project_with_run, monkeypatch
):
    """The ONE producer seam fails CLOSED — a claimed minted
    child reaching it raises typed control flow before any producer/tool/
    artifact work or turn consumption."""
    from modulatio.orchestration import MintedAttemptAlreadyClaimed
    orch = _make_orchestrator(project_with_run)
    child = _make_task(id="MNT-T-001-D1", minted_by="mint-1")
    child.attempt_claim_id = "claim-1"
    child.lifetime_attempts = 0  # adversarial: stale/tampered counter
    turns = []
    monkeypatch.setattr(
        Orchestrator, "_increment_turn_persisted",
        lambda self: turns.append(1))
    with pytest.raises(MintedAttemptAlreadyClaimed):
        orch._producer_execute(child)
    assert turns == [], (
        "a rejected claimed entry must not consume a turn"
    )


def test_redo_owner_routes_claimed_seam_refusal_to_interrupted_lane(
    project_with_run, monkeypatch
):
    """The redo owner catches the typed refusal and settles the
    child through the interrupted lane — zero producer work."""
    from modulatio.orchestration import RunSummary
    from modulatio.types import TaskStatus
    orch = _make_orchestrator(project_with_run)
    child = _make_task(id="MNT-T-001-D1", minted_by="mint-1")
    child.attempt_claim_id = "claim-1"
    child.lifetime_attempts = 0  # adversarial re-entry: admitted by arithmetic
    orch._run_task_with_redo(child, RunSummary(project=project_with_run))
    assert child.status is TaskStatus.BLOCKED
    assert any("interrupted" in tr.rationale for tr in child.transitions)


def test_merge_cannot_regress_barrier_state(project_with_run):
    """A stale worker snapshot merged after the barrier
    cannot remove the mint record, decrease the counter, clear the claim, or
    regress a terminal child."""
    from modulatio import store
    from modulatio.types import DecomposeMintRecord, TaskStatus
    run_id = project_with_run.run_id
    current = _make_task(id="MNT-T-MONO", minted_by="mint-1")
    current.attempt_claim_id = "claim-1"
    current.lifetime_attempts = 4
    current.status = TaskStatus.COMPLETED
    current.decompose_mint = DecomposeMintRecord(mint_id="m-1")
    store.save_task(PROJECT_CODE, current, run_id=run_id)
    stale = _make_task(id="MNT-T-MONO", minted_by="mint-1")
    stale.lifetime_attempts = 0  # pre-claim snapshot
    store.save_task_monotonic(PROJECT_CODE, stale, run_id=run_id)
    on_disk = store.get_task(PROJECT_CODE, "MNT-T-MONO", run_id=run_id)
    assert on_disk.decompose_mint is not None
    assert on_disk.attempt_claim_id == "claim-1"
    assert on_disk.lifetime_attempts == 4
    assert on_disk.status is TaskStatus.COMPLETED


def test_merge_conflicting_claim_id_is_typed_conflict(project_with_run):
    """A snapshot carrying a DIFFERENT claim id is a typed
    conflict, never last-writer-wins."""
    from modulatio import store
    from modulatio.orchestration import DecomposeMintConflict
    run_id = project_with_run.run_id
    current = _make_task(id="MNT-T-MONO2", minted_by="mint-1")
    current.attempt_claim_id = "claim-1"
    store.save_task(PROJECT_CODE, current, run_id=run_id)
    imposter = _make_task(id="MNT-T-MONO2", minted_by="mint-1")
    imposter.attempt_claim_id = "claim-OTHER"
    with pytest.raises(DecomposeMintConflict):
        store.save_task_monotonic(PROJECT_CODE, imposter, run_id=run_id)
    on_disk = store.get_task(PROJECT_CODE, "MNT-T-MONO2", run_id=run_id)
    assert on_disk.attempt_claim_id == "claim-1"


# ── Restart reservation authority + strict declared-key index ───────────────

def test_prepared_mint_reservations_survive_restart(
    project_with_run, monkeypatch, tmp_path
):
    """Parent committed, no child files, FRESH process — a
    different split targeting one of the prepared record's keys takes a
    typed refusal; recovery afterwards still materializes the originals."""
    from modulatio.orchestration import RunSummary, _DecomposeRefusal
    from modulatio.types import TaskStatus
    orch = _make_orchestrator(project_with_run)
    reloaded = _committed_parent_no_children(
        orch, monkeypatch, project_with_run, tmp_path)
    assert reloaded.decompose_mint.state == "prepared"
    fresh = _make_orchestrator(project_with_run)  # restart: empty registry
    _planner_returns(monkeypatch,
        '[{"description":"a","output_path":"drafts/one.md"},'
        '{"description":"b","output_path":"drafts/elsewhere.md"}]')
    rival = _make_task(id="MNT-T-RIVAL")
    outcome = fresh._attempt_decompose(rival, _ctx_err(tmp_path))
    assert isinstance(outcome, _DecomposeRefusal), (
        "a prepared mint's reservations must be owned across restart"
    )
    assert "drafts/one.md" in outcome.reason
    monkeypatch.setattr(Orchestrator, "_run_task_with_redo_inner",
        lambda self, t, summary, *a, **kw: setattr(
            t, "status", TaskStatus.COMPLETED))
    fresh._resume_decompose_mint(reloaded, RunSummary(project=project_with_run))
    assert reloaded.status is TaskStatus.COMPLETED


def test_same_size_mtime_restored_replacement_is_observed(
    project_with_run
):
    """(mtime, size) is not a content identity — an equal-size
    replacement with its mtime restored must still be observed (inode/ctime
    signature)."""
    import os
    from modulatio import store
    run_id = project_with_run.run_id
    a = _make_task(id="MNT-T-SWP", output_path="drafts/aaa.md")
    store.save_task(PROJECT_CODE, a, run_id=run_id)
    keys = store.declared_artifact_keys(PROJECT_CODE, run_id=run_id)
    assert "drafts/aaa.md" in keys
    path = store._task_path(PROJECT_CODE, a.id, run_id=run_id)
    st = path.stat()
    body = path.read_text()
    assert "drafts/aaa.md" in body
    replaced = body.replace("drafts/aaa.md", "drafts/bbb.md")  # equal length
    tmp = path.with_suffix(".tmp")
    tmp.write_text(replaced)
    os.replace(tmp, path)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))  # restore mtime
    keys = store.declared_artifact_keys(PROJECT_CODE, run_id=run_id)
    assert "drafts/bbb.md" in keys and "drafts/aaa.md" not in keys, (
        "a same-size mtime-restored replacement must invalidate the cache"
    )


def test_malformed_task_file_refuses_split_fail_closed(
    project_with_run, monkeypatch, tmp_path
):
    """A task file that cannot be parsed is UNKNOWN authority —
    the index raises typed and the whole split refuses; no reservations, no
    mint, no children."""
    from modulatio import store
    from modulatio.orchestration import _DecomposeRefusal
    run_id = project_with_run.run_id
    path = store._task_path(PROJECT_CODE, "MNT-T-JUNK", run_id=run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("::: not frontmatter :::\ngarbage")
    with pytest.raises(store.DeclaredArtifactIndexError):
        store.declared_artifact_keys(PROJECT_CODE, run_id=run_id)
    orch = _make_orchestrator(project_with_run)
    _planner_returns(monkeypatch, TWO_CHILD_SPLIT)
    outcome = orch._attempt_decompose(_make_task(), _ctx_err(tmp_path))
    assert isinstance(outcome, _DecomposeRefusal)
    assert orch._decompose_reservations == {}


# ── Child-authority projection ──────────────────────────────────────────────

@pytest.mark.parametrize("field,value", [
    ("description", "altered payload"),
    ("depends_on", ["MNT-T-999"]),
    ("goal_id", "MNT-G-EVIL"),
    ("max_retries", 99),
    ("decompose_depth", 0),
    ("required_skills", ["shell-execution"]),
    ("tool_args", {"cmd": "rm -rf"}),
])
def test_altered_immutable_descriptor_is_typed_conflict(
    project_with_run, monkeypatch, tmp_path, field, value
):
    """Same mint id, path, and lineage — but ANY altered
    immutable construction field (work, binding, budget, ancestry, tools)
    raises DecomposeMintConflict: no write, no producer call."""
    from modulatio import store
    from modulatio.types import DecomposeMintConflict, Task
    orch = _make_orchestrator(project_with_run)
    reloaded = _committed_parent_no_children(
        orch, monkeypatch, project_with_run, tmp_path)
    rec = reloaded.decompose_mint
    planted = Task.model_validate(rec.child_descriptors[0])
    setattr(planted, field, value)
    store.save_task(PROJECT_CODE, planted, run_id=project_with_run.run_id)
    producer_calls = []
    monkeypatch.setattr(
        Orchestrator, "_producer_execute",
        lambda self, task, corrective_notes="": producer_calls.append(task.id))
    with pytest.raises(DecomposeMintConflict):
        orch._materialize_mint_children(reloaded)
    assert producer_calls == []
    on_disk = store.get_task(
        PROJECT_CODE, planted.id, run_id=project_with_run.run_id)
    assert getattr(on_disk, field) == getattr(planted, field), (
        "conflict must not overwrite the planted record"
    )


def test_honest_reassigned_completed_child_is_preserved(
    project_with_run, monkeypatch, tmp_path
):
    """routing/execution state the
    engine legitimately changes after birth — assigned agent, status,
    counter, claim — must NOT be treated as authority; an honest completed
    child that was reassigned is preserved, not conflicted."""
    from modulatio import store
    from modulatio.types import Task, TaskStatus
    orch = _make_orchestrator(project_with_run)
    reloaded = _committed_parent_no_children(
        orch, monkeypatch, project_with_run, tmp_path)
    rec = reloaded.decompose_mint
    honest = Task.model_validate(rec.child_descriptors[0])
    honest.assigned_agent_id = "reassigned-seat"
    honest.status = TaskStatus.COMPLETED
    honest.lifetime_attempts = 99
    honest.attempt_claim_id = "claim-x"
    store.save_task(PROJECT_CODE, honest, run_id=project_with_run.run_id)
    children = orch._materialize_mint_children(reloaded)
    got = {c.id: c for c in children}[honest.id]
    assert got.status is TaskStatus.COMPLETED
    assert got.assigned_agent_id == "reassigned-seat"


# ── Mint-fact delivery ordering ─────────────────────────────────────────────

def test_audit_append_failure_keeps_mint_fact_recoverable(
    project_with_run, monkeypatch, tmp_path
):
    """If the durable audit append fails, the id is NOT marked
    delivered — a later resume re-emits the same stable id and the durable
    fact lands."""
    import json as _json
    from modulatio import compression, store
    from modulatio.orchestration import RunSummary
    orch = _make_orchestrator(project_with_run)
    parent = _make_task()
    store.save_task(PROJECT_CODE, parent, run_id=project_with_run.run_id)

    def _broken_append(path, row, lock=None, **kw):
        raise OSError("audit disk full")

    orig = compression._append_audit_row_0600
    monkeypatch.setattr(compression, "_append_audit_row_0600", _broken_append)
    handled, _ = _run_split(orch, monkeypatch, project_with_run, parent, tmp_path)
    assert handled is True, "a disclosure failure must not abort the mint"
    mint_id = parent.decompose_mint.mint_id
    assert mint_id not in orch._emitted_mint_ids, (
        "the id must stay retryable when the durable append failed"
    )
    monkeypatch.setattr(compression, "_append_audit_row_0600", orig)
    orch._resume_decompose_mint(parent, RunSummary(project=project_with_run))
    audit = orch._scope_root() / "audit.jsonl"
    rows = [_json.loads(x) for x in audit.read_text().splitlines()]
    mints = [r for r in rows if r.get("event") == "decompose_mint"]
    assert [r["mint_id"] for r in mints] == [mint_id]


def test_activity_callback_failure_does_not_lose_durable_fact(
    project_with_run, monkeypatch, tmp_path
):
    """A raising live-activity subscriber neither aborts the
    mint nor loses the durable audit fact (which lands BEFORE delivery)."""
    import json as _json
    from modulatio import store
    orch = _make_orchestrator(project_with_run)

    def _bad_subscriber(event):
        if event.phase == "decompose_mint":
            raise RuntimeError("subscriber crashed")

    orch.activity_callback = _bad_subscriber
    parent = _make_task()
    store.save_task(PROJECT_CODE, parent, run_id=project_with_run.run_id)
    handled, _ = _run_split(orch, monkeypatch, project_with_run, parent, tmp_path)
    assert handled is True
    audit = orch._scope_root() / "audit.jsonl"
    rows = [_json.loads(x) for x in audit.read_text().splitlines()]
    mints = [r for r in rows if r.get("event") == "decompose_mint"]
    assert len(mints) == 1
    assert parent.decompose_mint.mint_id in orch._emitted_mint_ids


def test_fresh_process_reemit_appends_no_duplicate_row(
    project_with_run, monkeypatch, tmp_path
):
    """The audit append is idempotent by mint_id — a fresh
    process replaying the committed mint appends NO second row."""
    import json as _json
    from modulatio import store
    from modulatio.orchestration import RunSummary
    orch = _make_orchestrator(project_with_run)
    parent = _make_task()
    store.save_task(PROJECT_CODE, parent, run_id=project_with_run.run_id)
    handled, _ = _run_split(orch, monkeypatch, project_with_run, parent, tmp_path)
    assert handled is True
    fresh = _make_orchestrator(project_with_run)
    reloaded = store.get_task(
        PROJECT_CODE, parent.id, run_id=project_with_run.run_id)
    fresh._resume_decompose_mint(reloaded, RunSummary(project=project_with_run))
    audit = fresh._scope_root() / "audit.jsonl"
    rows = [_json.loads(x) for x in audit.read_text().splitlines()]
    mints = [r for r in rows if r.get("event") == "decompose_mint"]
    assert len(mints) == 1, "idempotent append: one row per stable id"


# ── The isolated-worker production-path matrix ──────────────────────────────

def _worker_overflow_setup(orch, monkeypatch, parent_marker):
    """Route the REAL producer seam inside a worker: the parent's model call
    overflows (→ inline decompose); minted children's calls return a draft."""
    def _model_call(self, agent_id, role, prompt, **kw):
        if parent_marker in prompt:
            raise context_budget.RecoverableContextError(
                model="m", estimated_tokens=999_999, max_input_tokens=1,
                checkpoint_path=None,
            )
        return "stub draft"

    monkeypatch.setattr(Orchestrator, "_run_agent_call", _model_call)
    monkeypatch.setenv("MODULATIO_QC_FIXER", "0")


def test_worker_path_wal_durable_before_first_child_write(
    project_with_run, monkeypatch, tmp_path
):
    """Real _execute_task_isolated,
    deferral active — at the FIRST child materialization, a fresh read of
    the parent FILE already shows record + spend."""
    from modulatio import store
    orch = _make_orchestrator(project_with_run)
    parent = _make_task(description="OVERFLOW-ME-PARENT")
    store.save_task(PROJECT_CODE, parent, run_id=project_with_run.run_id)
    _planner_returns(monkeypatch, TWO_CHILD_SPLIT)
    _worker_overflow_setup(orch, monkeypatch, "OVERFLOW-ME-PARENT")
    monkeypatch.setattr(
        Orchestrator, "_run",
        lambda self, role, prompt, **kw: TWO_CHILD_SPLIT)
    observed = {}
    orig_child_barrier = Orchestrator._persist_mint_child_barrier

    def _probe(self, child):
        if "at_first_child" not in observed:
            on_disk = store.get_task(
                PROJECT_CODE, parent.id, run_id=project_with_run.run_id)
            observed["at_first_child"] = {
                "record": on_disk is not None
                and on_disk.decompose_mint is not None,
                "spent": on_disk is not None
                and on_disk.lifetime_attempts
                == max(parent.max_retries, 0) + 1,
            }
        return orig_child_barrier(self, child)

    monkeypatch.setattr(Orchestrator, "_persist_mint_child_barrier", _probe)
    result = orch._execute_task_isolated(parent)
    assert observed.get("at_first_child") == {"record": True, "spent": True}, (
        f"worker WAL must be durable before any child write: {observed}"
    )
    assert result.task.decompose_mint is not None


def test_worker_path_claim_durable_at_child_model_call(
    project_with_run, monkeypatch, tmp_path
):
    """At the minted child's model call
    inside the worker, the child FILE already carries claim + spent."""
    from modulatio import store
    orch = _make_orchestrator(project_with_run)
    parent = _make_task(description="OVERFLOW-ME-PARENT")
    store.save_task(PROJECT_CODE, parent, run_id=project_with_run.run_id)
    observed = {}

    def _model_call(self, agent_id, role, prompt, **kw):
        if "OVERFLOW-ME-PARENT" in prompt:
            raise context_budget.RecoverableContextError(
                model="m", estimated_tokens=999_999, max_input_tokens=1,
                checkpoint_path=None,
            )
        if role == self.default_producer_role and "child" not in observed:
            child_id = f"{parent.id}-D1"
            on_disk = store.get_task(
                PROJECT_CODE, child_id, run_id=project_with_run.run_id)
            observed["child"] = {
                "claimed": on_disk is not None
                and on_disk.attempt_claim_id is not None,
                "spent": on_disk is not None
                and on_disk.lifetime_attempts
                == max(on_disk.max_retries, 0) + 1,
            }
        return "stub draft"

    monkeypatch.setattr(Orchestrator, "_run_agent_call", _model_call)
    monkeypatch.setattr(
        Orchestrator, "_run",
        lambda self, role, prompt, **kw: TWO_CHILD_SPLIT)
    monkeypatch.setenv("MODULATIO_QC_FIXER", "0")
    orch._execute_task_isolated(parent)
    assert observed.get("child") == {"claimed": True, "spent": True}, (
        f"worker claim must be durable before the child's model call: "
        f"{observed}"
    )


def test_worker_death_after_wal_resumes_same_mint_from_fresh_process(
    project_with_run, monkeypatch, tmp_path
):
    """Worker dies after the WAL commit,
    before any child write — a FRESH orchestrator built from disk resumes
    the SAME mint id, materializes the children, no replan, no parent
    producer."""
    from modulatio import store
    from modulatio.orchestration import RunSummary
    orch = _make_orchestrator(project_with_run)
    parent = _make_task(description="OVERFLOW-ME-PARENT")
    store.save_task(PROJECT_CODE, parent, run_id=project_with_run.run_id)
    _worker_overflow_setup(orch, monkeypatch, "OVERFLOW-ME-PARENT")
    monkeypatch.setattr(
        Orchestrator, "_run",
        lambda self, role, prompt, **kw: TWO_CHILD_SPLIT)

    def _die(self, child):
        raise RuntimeError("worker death before child save")

    monkeypatch.setattr(Orchestrator, "_persist_mint_child_barrier", _die)
    try:
        orch._execute_task_isolated(parent)
    except RuntimeError:
        pass
    on_disk = store.get_task(
        PROJECT_CODE, parent.id, run_id=project_with_run.run_id)
    assert on_disk.decompose_mint is not None
    mint_id = on_disk.decompose_mint.mint_id
    monkeypatch.undo()
    fresh = _make_orchestrator(project_with_run)
    roles = []

    def _record(self, role, prompt, **kw):
        roles.append(role)
        return "stub"

    monkeypatch.setattr(Orchestrator, "_run", _record)
    monkeypatch.setattr(
        Orchestrator, "_run_agent_call",
        lambda self, agent_id, role, prompt, **kw: "stub draft")
    monkeypatch.setenv("MODULATIO_QC_FIXER", "0")
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)  # re-pin after undo
    fresh._run_task_with_redo(on_disk, RunSummary(project=project_with_run))
    assert "planner" not in roles
    stored = {
        t.id: t for t in store.list_tasks(
            PROJECT_CODE, run_id=project_with_run.run_id)
    }
    assert stored[parent.id].decompose_mint.mint_id == mint_id
    assert f"{parent.id}-D1" in stored and f"{parent.id}-D2" in stored


def test_two_workers_contesting_one_key_exactly_one_mints(
    project_with_run, monkeypatch, tmp_path
):
    """Two isolated workers race one
    child artifact key across the barrier/release interval — exactly one
    parent mints it."""
    import json as _json
    import threading
    from modulatio import store
    orch = _make_orchestrator(project_with_run)
    parents = [
        _make_task(id="MNT-T-W1", description="OVERFLOW-ME-PARENT"),
        _make_task(id="MNT-T-W2", description="OVERFLOW-ME-PARENT"),
    ]
    for p in parents:
        store.save_task(PROJECT_CODE, p, run_id=project_with_run.run_id)
    _worker_overflow_setup(orch, monkeypatch, "OVERFLOW-ME-PARENT")

    def _split(self, role, prompt, **kw):
        tid = kw.get("task_id", "x")
        return _json.dumps([
            {"description": "a", "output_path": "drafts/contested.md"},
            {"description": "b", "output_path": f"drafts/{tid}-own.md"},
        ])

    monkeypatch.setattr(Orchestrator, "_run", _split)
    barrier = threading.Barrier(2)
    results = {}

    def _go(p):
        barrier.wait()
        results[p.id] = orch._execute_task_isolated(p)

    threads = [threading.Thread(target=_go, args=(p,)) for p in parents]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    minted = [
        p.id for p in parents
        if results[p.id].task.decompose_mint is not None
        and "drafts/contested.md"
        in results[p.id].task.decompose_mint.reservations
    ]
    assert len(minted) == 1, (
        f"exactly one worker may own the contested key; got {minted}"
    )


# ── Commit-point truth, disclosure outbox, canonical live merge ─────────────

def test_verify_read_unavailable_after_save_is_not_a_refusal(
    project_with_run, monkeypatch, tmp_path
):
    """save_task RETURNED (atomic replace done) but the
    verification read comes back None — the commit STANDS. No refusal, no
    rollback; memory and disk agree on mint id + spent counter. Sequential
    and worker-TLS flavors."""
    from modulatio import store
    from modulatio.orchestration import RunSummary
    for worker in (False, True):
        orch = _make_orchestrator(project_with_run)
        parent = _make_task(id=f"MNT-T-CP-{int(worker)}")
        store.save_task(PROJECT_CODE, parent, run_id=project_with_run.run_id)
        _planner_returns(
            monkeypatch,
            f'[{{"description":"a","output_path":"drafts/b7-{int(worker)}-a.md"}},'
            f'{{"description":"b","output_path":"drafts/b7-{int(worker)}-b.md"}}]')
        monkeypatch.setattr(Orchestrator, "_run_task_with_redo",
            lambda self, t, summary, **kw: setattr(
                t, "status", __import__(
                    "modulatio.types", fromlist=["TaskStatus"],
                ).TaskStatus.COMPLETED))
        orig_get = store_mod.get_task
        blind = {"n": 0}

        def _blind_once(code, task_id, run_id=None):
            if task_id == parent.id and blind["n"] == 0:
                blind["n"] += 1
                return None  # transient read failure AFTER the save returned
            return orig_get(code, task_id, run_id=run_id)

        monkeypatch.setattr(store_mod, "get_task", _blind_once)
        if worker:
            _enter_worker_tls(orch)
        try:
            handled, refusal = orch._try_decompose_and_run(
                parent, _ctx_err(tmp_path), RunSummary(project=project_with_run))
        finally:
            if worker:
                orch._tls.deferred_writes = None
                orch._tls.child_tasks = None
        monkeypatch.setattr(store_mod, "get_task", orig_get)
        assert handled is True and refusal is None, (
            f"worker={worker}: a returned save is the commit point — a blind "
            f"verify read must not become a refusal"
        )
        assert parent.decompose_mint is not None
        on_disk = store.get_task(
            PROJECT_CODE, parent.id, run_id=project_with_run.run_id)
        assert on_disk.decompose_mint.mint_id == parent.decompose_mint.mint_id
        assert on_disk.lifetime_attempts == parent.lifetime_attempts


def test_verify_read_foreign_authority_is_typed_barrier_conflict(
    project_with_run, monkeypatch, tmp_path
):
    """Verification reads back a DIFFERENT mint authority —
    typed MintBarrierConflict: no children, no producer, no rollback, and no
    'nothing minted' refusal (the in-memory committed record is retained)."""
    from modulatio import store
    from modulatio.orchestration import MintBarrierConflict, RunSummary
    from modulatio.types import DecomposeMintRecord
    orch = _make_orchestrator(project_with_run)
    parent = _make_task()
    store.save_task(PROJECT_CODE, parent, run_id=project_with_run.run_id)
    _planner_returns(monkeypatch, TWO_CHILD_SPLIT)
    producer_calls = []
    monkeypatch.setattr(
        Orchestrator, "_producer_execute",
        lambda self, task, corrective_notes="": producer_calls.append(task.id))
    orig_get = store_mod.get_task

    def _foreign(code, task_id, run_id=None):
        if task_id == parent.id:
            impost = orig_get(code, task_id, run_id=run_id)
            if impost is not None and impost.decompose_mint is not None:
                impost.decompose_mint = DecomposeMintRecord(
                    mint_id="FOREIGN-MINT")
            return impost
        return orig_get(code, task_id, run_id=run_id)

    monkeypatch.setattr(store_mod, "get_task", _foreign)
    with pytest.raises(MintBarrierConflict):
        orch._try_decompose_and_run(
            parent, _ctx_err(tmp_path), RunSummary(project=project_with_run))
    monkeypatch.setattr(store_mod, "get_task", orig_get)
    assert producer_calls == []
    assert parent.decompose_mint is not None, (
        "conflict must retain the in-memory committed record, not roll back"
    )


def test_claim_verify_read_unavailable_claim_is_consumed(
    project_with_run, monkeypatch, tmp_path
):
    """The claim save returned, its verify read fails —
    the claim is CONSUMED. Fresh recovery sees it and makes zero producer
    calls."""
    from modulatio import store
    orch = _make_orchestrator(project_with_run)
    child = _make_task(id="MNT-T-001-D1", minted_by="mint-1")
    child.lifetime_attempts = max(child.max_retries, 0)
    store.save_task(PROJECT_CODE, child, run_id=project_with_run.run_id)
    orig_get = store_mod.get_task
    monkeypatch.setattr(
        store_mod, "get_task",
        lambda code, task_id, run_id=None: None
        if task_id == child.id else orig_get(code, task_id, run_id=run_id))
    orch._claim_and_count(child)  # must NOT raise — the claim stands
    monkeypatch.setattr(store_mod, "get_task", orig_get)
    assert child.attempt_claim_id is not None
    on_disk = store.get_task(
        PROJECT_CODE, child.id, run_id=project_with_run.run_id)
    assert on_disk.attempt_claim_id == child.attempt_claim_id


def test_failed_disclosure_recovers_via_real_startup(
    project_with_run, monkeypatch, tmp_path
):
    """Audit append fails, the run ends with the parent
    COMPLETED — a fresh orchestrator entering through the REAL kickoff entry
    recovers the pending disclosure: exactly one stable-id row, record
    advanced to emitted."""
    import json as _json
    from modulatio import compression, store
    orch = _make_orchestrator(project_with_run)
    parent = _make_task()
    store.save_task(PROJECT_CODE, parent, run_id=project_with_run.run_id)
    orig_append = compression._append_audit_row_0600
    monkeypatch.setattr(
        compression, "_append_audit_row_0600",
        lambda *a, **kw: (_ for _ in ()).throw(OSError("audit sink down")))
    handled, _ = _run_split(orch, monkeypatch, project_with_run, parent, tmp_path)
    assert handled is True
    on_disk = store.get_task(
        PROJECT_CODE, parent.id, run_id=project_with_run.run_id)
    assert on_disk.decompose_mint.disclosure == "pending", (
        "a failed append must leave a DURABLE pending marker, not just a "
        "volatile unmarked set"
    )
    monkeypatch.setattr(compression, "_append_audit_row_0600", orig_append)
    fresh = _make_orchestrator(project_with_run)
    monkeypatch.setattr(
        Orchestrator, "_kickoff_inner",
        lambda self, *a, **kw: __import__(
            "modulatio.orchestration", fromlist=["RunSummary"],
        ).RunSummary(project=project_with_run))
    fresh.kickoff("noop")  # the REAL startup entry — no private resume call
    audit = fresh._scope_root() / "audit.jsonl"
    rows = [_json.loads(x) for x in audit.read_text().splitlines()]
    mints = [r for r in rows if r.get("event") == "decompose_mint"]
    assert [r["mint_id"] for r in mints] == [on_disk.decompose_mint.mint_id]
    recovered = store.get_task(
        PROJECT_CODE, parent.id, run_id=project_with_run.run_id)
    assert recovered.decompose_mint.disclosure == "emitted"


def test_crash_between_append_and_advance_yields_one_row(
    project_with_run, monkeypatch, tmp_path
):
    """The row landed but the emitted advance did not —
    startup recovery appends NO second row and advances the record."""
    import json as _json
    from modulatio import store
    orch = _make_orchestrator(project_with_run)
    parent = _make_task()
    store.save_task(PROJECT_CODE, parent, run_id=project_with_run.run_id)
    handled, _ = _run_split(orch, monkeypatch, project_with_run, parent, tmp_path)
    assert handled is True
    on_disk = store.get_task(
        PROJECT_CODE, parent.id, run_id=project_with_run.run_id)
    on_disk.decompose_mint.disclosure = "pending"  # simulate the lost advance
    store.save_task(PROJECT_CODE, on_disk, run_id=project_with_run.run_id)
    fresh = _make_orchestrator(project_with_run)
    monkeypatch.setattr(
        Orchestrator, "_kickoff_inner",
        lambda self, *a, **kw: __import__(
            "modulatio.orchestration", fromlist=["RunSummary"],
        ).RunSummary(project=project_with_run))
    fresh.kickoff("noop")
    audit = fresh._scope_root() / "audit.jsonl"
    rows = [_json.loads(x) for x in audit.read_text().splitlines()]
    mints = [r for r in rows if r.get("event") == "decompose_mint"]
    assert len(mints) == 1, "idempotent recovery: one row, not two"
    recovered = store.get_task(
        PROJECT_CODE, parent.id, run_id=project_with_run.run_id)
    assert recovered.decompose_mint.disclosure == "emitted"


def test_merge_syncs_canonical_terminal_state_into_live_objects(
    project_with_run, monkeypatch
):
    """A stale nonterminal snapshot merged through the
    REAL _merge_task_result path — disk, result.task, task_map, and
    summary.tasks all expose the durable terminal record afterward."""
    from modulatio import store
    from modulatio.orchestration import (
        RunSummary, TaskExecutionResult, _merge_task_result,
    )
    from modulatio.types import TaskStatus
    run_id = project_with_run.run_id
    orch = _make_orchestrator(project_with_run)
    durable = _make_task(id="MNT-T-MG", minted_by="mint-1")
    durable.attempt_claim_id = "claim-1"
    durable.lifetime_attempts = 4
    durable.status = TaskStatus.COMPLETED
    store.save_task(PROJECT_CODE, durable, run_id=run_id)
    stale = _make_task(id="MNT-T-MG", minted_by="mint-1")
    stale.status = TaskStatus.PENDING
    stale.lifetime_attempts = 0
    summary = RunSummary(project=project_with_run)
    task_map = {stale.id: stale}
    res = TaskExecutionResult(task=stale)
    _merge_task_result(
        res, summary,
        save_task=lambda t: orch._merge_save(t, summary),
        merged_ids=set(),
    )
    on_disk = store.get_task(PROJECT_CODE, "MNT-T-MG", run_id=run_id)
    assert on_disk.status is TaskStatus.COMPLETED
    assert res.task.status is TaskStatus.COMPLETED, (
        "the live result object must be synchronized to the durable record"
    )
    assert task_map["MNT-T-MG"].status is TaskStatus.COMPLETED
    assert summary.tasks and summary.tasks[0].status is TaskStatus.COMPLETED
    assert res.task.attempt_claim_id == "claim-1"


def test_merge_conflict_reconciles_live_object_and_surfaces_error(
    project_with_run, monkeypatch
):
    """A conflicting snapshot — durable record kept, the
    live object reconciled TO it, and a summary error surfaced (not
    log-only)."""
    from modulatio import store
    from modulatio.orchestration import (
        RunSummary, TaskExecutionResult, _merge_task_result,
    )
    run_id = project_with_run.run_id
    orch = _make_orchestrator(project_with_run)
    durable = _make_task(id="MNT-T-MC", minted_by="mint-1")
    durable.attempt_claim_id = "claim-1"
    store.save_task(PROJECT_CODE, durable, run_id=run_id)
    imposter = _make_task(id="MNT-T-MC", minted_by="mint-1")
    imposter.attempt_claim_id = "claim-EVIL"
    summary = RunSummary(project=project_with_run)
    res = TaskExecutionResult(task=imposter)
    _merge_task_result(
        res, summary,
        save_task=lambda t: orch._merge_save(t, summary),
        merged_ids=set(),
    )
    on_disk = store.get_task(PROJECT_CODE, "MNT-T-MC", run_id=run_id)
    assert on_disk.attempt_claim_id == "claim-1"
    assert res.task.attempt_claim_id == "claim-1", (
        "the live object must reconcile to the durable authority"
    )
    assert any("MNT-T-MC" in e for e in summary.errors), (
        "a merge conflict must surface as a summary error, not log-only"
    )
