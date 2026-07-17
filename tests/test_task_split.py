# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Context-size-driven task fan-out (design 2026-07-01).

A capable planner fans a multi-dimension gather goal only ~50% of the time; when
it collapses, one producer gets the whole scope and rides the compression bands.
_split_oversized_gathers engine-binds the fan: a focused per-spec LLM call judges
whether the scope's projected working context fits under the prudent cap (a
fraction of that task's own window); an oversized spec is REPLACED with an
``artifacts:[]`` fan of the fewest size-bounded chunks, paths engine-derived and
unique by construction. Size decides whether to cut; the model picks the lines.
"""

from __future__ import annotations

import json
import logging

import pytest

# NOTE: orchestration members are resolved at test runtime via attribute
# access (orchestration.Orchestrator / orchestration._PlanError), never
# from-imported at module top — test_orchestration_resweep.py reloads the
# module mid-suite, and a collection-time class reference goes stale
# (pytest.raises on the old exception class misses the reloaded one).
from modulatio import orchestration, vault
from modulatio.types import Project


@pytest.fixture
def make_orch(tmp_path, monkeypatch):
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path / "vault")
    vault.init_project("SPL", "Split", "obj", exist_ok=True)

    def _make(planner):
        pr = Project(
            code="SPL", name="Split", objective="obj", leader_model="stub",
            run_id="20260702T210000Z-bbb222",
            wiki_path=str(vault.project_dir("SPL")),
        )
        return orchestration.Orchestrator(pr, {"leader": lambda p: "", "planner": planner,
                                 "drafter": lambda p: "", "qc": lambda p: ""})

    return _make


def _gather(desc="Research every degradation mechanism", **kw):
    spec = {"description": desc, "output_path": "drafts/notes.md",
            "artifact_kind": "research", "operation": "research",
            "required_skills": ["web-search"], "deliverable": False,
            "depends_on": []}
    spec.update(kw)
    return spec


def _split_reply(*chunks, estimated=40_000):
    return json.dumps(
        {"fits": False, "estimated_tokens": estimated, "chunks": list(chunks)}
    )


def test_oversized_gather_splits_into_engine_pathed_chunks(make_orch):
    orch = make_orch(lambda p: _split_reply("Mechanism A", "Mechanism B", "Mechanism C"))
    out = orch._split_oversized_gathers([_gather()])
    assert len(out) == 1
    arts = out[0]["artifacts"]
    assert [a["description"] for a in arts] == ["Mechanism A", "Mechanism B", "Mechanism C"]
    # Paths are engine-derived, unique by construction via the spec-index prefix —
    # never a string the LLM invents.
    assert [a["path"] for a in arts] == [
        "drafts/0-research-every-degradation-mechanism_chunk_00.md",
        "drafts/0-research-every-degradation-mechanism_chunk_01.md",
        "drafts/0-research-every-degradation-mechanism_chunk_02.md",
    ]
    assert "output_path" not in out[0]  # REPLACED wholesale, single path authority
    assert out[0]["required_skills"] == ["web-search"]  # spec fields survive
    assert out[0]["operation"] == "research"


def test_fitting_gather_passes_through_unchanged(make_orch):
    orch = make_orch(lambda p: '{"fits": true, "estimated_tokens": 9000}')
    data = [_gather()]
    out = orch._split_oversized_gathers(data)
    assert out == data


@pytest.mark.parametrize("op", ["construct", "produce", "debug"])
def test_non_gather_ops_never_reach_the_split_model(make_orch, op):
    calls = []
    orch = make_orch(lambda p: calls.append(p) or '{"fits": true}')
    data = [_gather(operation=op)]
    out = orch._split_oversized_gathers(data)
    assert out == data
    assert calls == []


@pytest.mark.parametrize("op", ["research", "comprehend", "evaluate"])
def test_gather_class_ops_are_all_candidates(make_orch, op):
    calls = []

    def planner(p):
        calls.append(p)
        return '{"fits": true, "estimated_tokens": 5000}'

    orch = make_orch(planner)
    orch._split_oversized_gathers([_gather(operation=op)])
    assert len(calls) == 1


def test_deliverable_gather_is_never_split(make_orch):
    """H-4: a deliverable's size-floor metric is computed for ONE artifact;
    splitting would mis-stamp the per-unit floor."""
    calls = []
    orch = make_orch(lambda p: calls.append(p) or '{"fits": true}')
    data = [_gather(deliverable=True)]
    out = orch._split_oversized_gathers(data)
    assert out == data
    assert calls == []


def test_prefanned_artifacts_spec_is_skipped(make_orch):
    calls = []
    orch = make_orch(lambda p: calls.append(p) or '{"fits": true}')
    spec = _gather()
    del spec["output_path"]
    spec["artifacts"] = [{"path": "drafts/a.md", "description": "A"}]
    out = orch._split_oversized_gathers([spec])
    assert out == [spec]
    assert calls == []


def test_single_chunk_response_leaves_spec_unchanged(make_orch):
    """One chunk == the whole scope fits in one task: no fan, no synthesis —
    the no-frivolous-task guarantee."""
    orch = make_orch(lambda p: _split_reply("the whole thing"))
    data = [_gather()]
    out = orch._split_oversized_gathers(data)
    assert out == data


def test_garbage_response_fails_open_to_no_split(make_orch):
    """A malformed split reply must not sink the plan — the spec passes through
    unsplit; the runtime RecoverableContextError→decompose keystone backstops an
    oversized task."""
    orch = make_orch(lambda p: "hmm, let me think about chunks…")
    data = [_gather()]
    out = orch._split_oversized_gathers(data)
    assert out == data


def test_planner_path_collision_with_chunk_path_fails_closed(make_orch):
    """A planner-emitted output_path that collides with an
    engine-derived chunk path must fail closed at plan time, before task
    creation — never a silent clobber, never a blocked wave later."""
    orch = make_orch(lambda p: _split_reply("A", "B"))
    data = [
        _gather(),
        {"description": "sneaky twin",
         "output_path": "drafts/0-research-every-degradation-mechanism_chunk_00.md",
         "artifact_kind": "text", "operation": "construct",
         "required_skills": [], "deliverable": False, "depends_on": []},
    ]
    with pytest.raises(orchestration._PlanError):
        orch._split_oversized_gathers(data)


def test_downstream_dep_indices_survive_the_split(make_orch):
    """The transform replaces the spec IN PLACE (same index), so a dependent
    synthesis task's index-deps stay valid — the artifacts expansion later
    multiplies the dep onto every chunk sub-task."""
    orch = make_orch(lambda p: _split_reply("A", "B"))
    data = [
        _gather(),
        {"description": "Synthesize the brief", "output_path": "drafts/brief.md",
         "artifact_kind": "text", "operation": "produce",
         "required_skills": [], "deliverable": True, "depends_on": [0]},
    ]
    out = orch._split_oversized_gathers(data)
    assert len(out) == 2
    assert out[1]["depends_on"] == [0]
    assert len(out[0]["artifacts"]) == 2


def test_cap_follows_the_tasks_own_budget_role(make_orch):
    """Per-role cap: research-artifact work is sized against the research window
    (64K → 12800 at the 0.20 default); other gathers against the producer window
    (48K → 9600)."""
    prompts = []

    def planner(p):
        prompts.append(p)
        return '{"fits": true, "estimated_tokens": 1000}'

    orch = make_orch(planner)
    orch._split_oversized_gathers([_gather()])  # artifact_kind=research
    orch._split_oversized_gathers(
        [_gather(artifact_kind="text", operation="comprehend")]
    )
    assert "12800" in prompts[0] or "12,800" in prompts[0]
    assert "9600" in prompts[1] or "9,600" in prompts[1]


def test_chunk_count_is_logged_prominently(make_orch, caplog):
    """The engine never gates on chunk count, but the
    count must be prominent enough to diagnose a degenerate split-stage model
    at a glance."""
    orch = make_orch(lambda p: _split_reply("A", "B", "C", "D"))
    with caplog.at_level(logging.INFO, logger="modulatio.orchestration"):
        orch._split_oversized_gathers([_gather()])
    assert any("4" in r.message and "chunk" in r.message.lower()
               for r in caplog.records)


def test_sugared_duplicate_path_cannot_bypass_the_invariant(make_orch):
    """Regression: the invariant compared RAW
    strings, but _validate_output_path later strips one leading "artifacts/"
    — so "artifacts/drafts/<chunk>.md" slipped past the comparison and
    canonicalized into a collision at task creation. Paths must be normalized
    with the SAME sugar rules before comparison."""
    orch = make_orch(lambda p: _split_reply("A", "B"))
    data = [
        _gather(),
        {"description": "sugared twin",
         "output_path": "artifacts/drafts/0-research-every-degradation-mechanism_chunk_00.md",
         "artifact_kind": "text", "operation": "construct",
         "required_skills": [], "deliverable": False, "depends_on": []},
    ]
    with pytest.raises(orchestration._PlanError):
        orch._split_oversized_gathers(data)


@pytest.mark.parametrize("sugar", ["./{p}", "{p}", "././{p}"])
def test_dot_slash_sugar_cannot_bypass_the_invariant(make_orch, sugar):
    """Leading "./" is sugar the normalizer strips; a malformed ".//x" is NOT
    sugar — it still fails closed at _validate_output_path (empty component)."""
    p = "drafts/0-research-every-degradation-mechanism_chunk_01.md"
    orch = make_orch(lambda p_: _split_reply("A", "B"))
    data = [
        _gather(),
        {"description": "dot twin", "output_path": sugar.format(p=p),
         "artifact_kind": "text", "operation": "construct",
         "required_skills": [], "deliverable": False, "depends_on": []},
    ]
    with pytest.raises(orchestration._PlanError):
        orch._split_oversized_gathers(data)


# ── end-to-end wiring through _plan_tasks ────────────────────────────────────
# Unit tests prove the part, not the wiring: nothing above drives
# _plan_tasks → _bind_wide_artifacts → _split_oversized_gathers → artifacts
# expansion → _validate_output_path → dep multiplication. These do, so a
# refactor that disconnects the seam (drops the split call, reorders it)
# fails HERE instead of silently.


def _e2e_planner(plan_specs):
    """A planner runner serving BOTH calls: the task-plan (returns the spec
    list) and the task-split (returns a two-chunk split for the gather)."""
    import json as _json

    def _run(prompt: str) -> str:
        if "TASK SCOPE:" in prompt:  # the task-split call
            return _split_reply("Mechanism A", "Mechanism B")
        return "```json\n" + _json.dumps(plan_specs) + "\n```"

    return _run


def _e2e_goal(orch):
    from modulatio import store
    from modulatio.types import Goal, GoalStatus

    goal = Goal(id="SPL-G-001", project_id=orch.project.id, description="d",
                success_criteria="s", status=GoalStatus.IN_PROGRESS)
    store.save_goal("SPL", goal)
    return goal


def test_split_wires_end_to_end_through_plan_tasks(make_orch):
    specs = [
        {"description": "Research every degradation mechanism",
         "output_path": "drafts/notes.md", "artifact_kind": "research",
         "operation": "research", "required_skills": ["web-search"],
         "deliverable": False, "depends_on": []},
        {"description": "Synthesize the brief",
         # sugared on purpose: proves _validate_output_path ran on the seam
         "output_path": "artifacts/brief.md", "artifact_kind": "text",
         "operation": "produce", "required_skills": [],
         "deliverable": True, "depends_on": [0]},
    ]
    orch = make_orch(_e2e_planner(specs))
    tasks = orch._plan_tasks(_e2e_goal(orch))

    research = [t for t in tasks if t.artifact_kind == "research"]
    synth = [t for t in tasks if t.artifact_kind == "text"]
    assert len(research) == 2 and len(synth) == 1
    # (a) the Task objects carry the engine-derived, validated chunk paths
    assert sorted(t.output_path for t in research) == [
        "drafts/0-research-every-degradation-mechanism_chunk_00.md",
        "drafts/0-research-every-degradation-mechanism_chunk_01.md",
    ]
    assert sorted(t.description for t in research) == ["Mechanism A", "Mechanism B"]
    # (b) the synthesis dep on spec 0 multiplied onto BOTH chunk tasks
    assert sorted(synth[0].depends_on) == sorted(t.id for t in research)
    # (c) _validate_output_path ran at this seam: the sugared plan path
    # came out normalized
    assert synth[0].output_path == "brief.md"


def test_plan_tasks_fails_closed_on_sugared_chunk_collision(make_orch):
    """The unit-level collision scenario, driven through the REAL seam: a
    planner path that canonicalizes onto an engine chunk path must kill the
    plan at _plan_tasks time, not dispatch."""
    specs = [
        {"description": "Research every degradation mechanism",
         "output_path": "drafts/notes.md", "artifact_kind": "research",
         "operation": "research", "required_skills": ["web-search"],
         "deliverable": False, "depends_on": []},
        {"description": "sneaky twin",
         "output_path": "artifacts/drafts/0-research-every-degradation-mechanism_chunk_00.md",
         "artifact_kind": "text", "operation": "construct",
         "required_skills": [], "deliverable": False, "depends_on": []},
    ]
    orch = make_orch(_e2e_planner(specs))
    with pytest.raises(orchestration._PlanError):
        orch._plan_tasks(_e2e_goal(orch))
