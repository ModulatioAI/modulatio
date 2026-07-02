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

from modulatio import vault
from modulatio.orchestration import Orchestrator, _PlanError
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
        return Orchestrator(pr, {"leader": lambda p: "", "planner": planner,
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
    """Jenny LOW #2: a planner-emitted output_path that collides with an
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
    with pytest.raises(_PlanError):
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
    """Wild Bill F2 condition: the engine never gates on chunk count, but the
    count must be prominent enough to diagnose a degenerate split-stage model
    at a glance."""
    orch = make_orch(lambda p: _split_reply("A", "B", "C", "D"))
    with caplog.at_level(logging.INFO, logger="modulatio.orchestration"):
        orch._split_oversized_gathers([_gather()])
    assert any("4" in r.message and "chunk" in r.message.lower()
               for r in caplog.records)
