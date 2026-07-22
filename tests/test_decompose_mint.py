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

    monkeypatch.setattr(Orchestrator, "_persist_child_task", _die)
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

    orig_save = Orchestrator._save_task_deferrable
    monkeypatch.setattr(Orchestrator, "_save_task_deferrable", _fail_save)
    summary = RunSummary(project=project_with_run)
    handled, refusal = orch._try_decompose_and_run(
        parent, _ctx_err(tmp_path), summary)
    assert handled is False and isinstance(refusal, _DecomposeRefusal)
    assert parent.decompose_mint is None
    assert parent.lifetime_attempts == 0  # in-memory spend reverted
    assert orch._decompose_reservations == {}
    monkeypatch.setattr(Orchestrator, "_save_task_deferrable", orig_save)
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

    orig_persist = Orchestrator._persist_child_task
    monkeypatch.setattr(Orchestrator, "_persist_child_task", _die)
    with pytest.raises(RuntimeError):
        orch._try_decompose_and_run(
            parent, _ctx_err(tmp_path), RunSummary(project=project))
    monkeypatch.setattr(Orchestrator, "_persist_child_task", orig_persist)
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
