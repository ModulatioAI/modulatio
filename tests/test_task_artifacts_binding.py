# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Parallel-execution Phase 1.5 — the task-level artifacts binding.

The live anthology failure: the Leader correctly made ONE wide goal (8 stories),
but the task-planner emitted 8 SEPARATE single-output tasks (+ a compile) → 9 > the
6-task per-goal cap → rejected. _bind_wide_artifacts binds independent, same-kind,
same-skill producer specs into ONE artifacts-fan-out spec BEFORE the cap check: 1
plan item → N parallel sub-tasks (fits the cap, forms the wide wave). A dependent
compile task keeps its dep, remapped onto the merged spec.
"""

from __future__ import annotations

import pytest

from modulatio import vault
from modulatio.orchestration import Orchestrator
from modulatio.types import Project


@pytest.fixture
def orch(tmp_path, monkeypatch):
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path / "vault")
    vault.init_project("WID", "Wide", "obj", exist_ok=True)
    pr = Project(code="WID", name="Wide", objective="obj", leader_model="stub",
                 run_id="20260605T170000Z-aaa111", wiki_path=str(vault.project_dir("WID")))
    return Orchestrator(pr, {"leader": lambda p: "", "planner": lambda p: "",
                             "drafter": lambda p: "", "qc": lambda p: ""})


def _story(path, *, skills=("long-form",), deliverable=True):
    return {"description": f"Write {path}", "output_path": path,
            "artifact_kind": "text", "required_skills": list(skills),
            "deliverable": deliverable, "depends_on": []}


def test_binds_independent_same_kind_tasks_into_one_artifacts_spec(orch):
    data = [_story("01.md"), _story("02.md"), _story("03.md")]
    out = orch._bind_wide_artifacts(data)
    assert len(out) == 1
    arts = out[0]["artifacts"]
    assert [a["path"] for a in arts] == ["01.md", "02.md", "03.md"]
    assert "output_path" not in out[0]  # became an artifacts spec
    assert out[0]["depends_on"] == []


def test_compile_dep_is_remapped_onto_the_merged_spec(orch):
    """The live shape: N stories + a compile task depending on all of them. The
    stories bind to one spec; compile's index-deps remap onto it (the expansion
    later multiplies the dep onto every story sub-task)."""
    data = [
        _story("01.md"), _story("02.md"), _story("03.md"),
        {"description": "Compile the anthology PDF", "output_path": "book.pdf",
         "artifact_kind": "pdf", "required_skills": ["media-assembly"],
         "deliverable": True, "depends_on": [0, 1, 2]},
    ]
    out = orch._bind_wide_artifacts(data)
    assert len(out) == 2  # merged-stories + compile
    merged, compile_spec = out[0], out[1]
    assert len(merged["artifacts"]) == 3
    assert compile_spec["description"].startswith("Compile")
    # all three story deps collapsed to the single merged spec's new index (0)
    assert compile_spec["depends_on"] == [0]


def test_heterogeneous_skills_are_not_merged(orch):
    """Different required_skills → different producer kind → not one group."""
    data = [_story("a.md", skills=("long-form",)), _story("b.py", skills=("coding",))]
    out = orch._bind_wide_artifacts(data)
    assert out == data  # nothing to bind (two distinct singleton groups)


def test_dependent_or_artifacts_specs_are_not_candidates(orch):
    a = _story("a.md")
    a["depends_on"] = [1]   # has a dep → not independent
    b = {"description": "batch", "artifacts": [{"path": "x.md"}],  # already fanned
         "artifact_kind": "text", "required_skills": ["long-form"], "depends_on": []}
    c = _story("c.md")
    out = orch._bind_wide_artifacts([a, b, c])
    assert out == [a, b, c]  # only c is an indep single → group of 1 → no merge


def test_passthrough_when_fewer_than_two_candidates(orch):
    data = [_story("only.md"), {"description": "research", "output_path": "r.md",
            "artifact_kind": "text", "required_skills": ["researcher"], "depends_on": []}]
    assert orch._bind_wide_artifacts(data) == data


def test_live_anthology_shape_binds_wide(orch):
    """The exact live failure (ticket ALX-1): an 8-deliverable goal whose planner
    emitted 8 separate story tasks + 1 compile. Binding folds the 8 stories into
    ONE artifacts spec → 2 plan items, and the compile's deps remap onto the
    merged spec (→ all 8 sub-tasks once expanded). The wide-bind keeps a
    homogeneous fan-out as ONE plan item the engine expands."""
    stories = [_story(f"{i:02d}_title.docx") for i in range(1, 9)]  # 8 independent
    compile_spec = {"description": "Compile the 8 stories into one PDF",
                    "output_path": "anthology.pdf", "artifact_kind": "pdf",
                    "required_skills": ["media-assembly"], "deliverable": True,
                    "depends_on": list(range(8))}
    out = orch._bind_wide_artifacts(stories + [compile_spec])
    assert len(out) == 2                            # 8 stories → 1 spec + the compile
    assert len(out[0]["artifacts"]) == 8            # one wide fan-out of 8 stories
    assert out[1]["depends_on"] == [0]              # compile waits on the merged spec


def test_two_homogeneous_groups_each_bind(orch):
    """8 stories (one group) + 3 diagrams (another) → two artifacts specs."""
    stories = [_story(f"s{i}.md", skills=("long-form",)) for i in range(4)]
    diagrams = [_story(f"d{i}.svg", skills=("diagram",)) for i in range(3)]
    out = orch._bind_wide_artifacts(stories + diagrams)
    assert len(out) == 2
    sizes = sorted(len(s["artifacts"]) for s in out)
    assert sizes == [3, 4]


# ── bind only when ALL expansion-inherited fields match ────────────────────


def _ev(desc):
    return [{"kind": "artifact", "description": desc}]


def test_heterogeneous_evidence_not_merged(orch):
    """Two same-kind/same-skill specs with DIFFERENT evidence_required must NOT
    merge — a sibling would inherit the wrong evidence contract."""
    a = _story("a.md")
    a["evidence_required"] = _ev("2000 words")
    b = _story("b.md")
    b["evidence_required"] = _ev("5000 words")
    assert orch._bind_wide_artifacts([a, b]) == [a, b]


def test_heterogeneous_research_topics_not_merged(orch):
    a = _story("a.md")
    a["research_topics"] = ["robots"]
    b = _story("b.md")
    b["research_topics"] = ["spaceships"]
    assert orch._bind_wide_artifacts([a, b]) == [a, b]


def test_heterogeneous_tool_args_not_merged(orch):
    a = _story("a.md")
    a["tool_args"] = {"mode": "fast"}
    b = _story("b.md")
    b["tool_args"] = {"mode": "deep"}
    assert orch._bind_wide_artifacts([a, b]) == [a, b]


def test_homogeneous_evidence_still_merges(orch):
    """Identical inherited fields (the real anthology shape — same generic 'a
    complete story' evidence) DO merge."""
    a = _story("a.md")
    a["evidence_required"] = _ev("a complete story")
    b = _story("b.md")
    b["evidence_required"] = _ev("a complete story")
    out = orch._bind_wide_artifacts([a, b])
    assert len(out) == 1 and len(out[0]["artifacts"]) == 2


def test_dep_remap_multi_group_shifted_nonmerged_and_weird_deps(orch):
    """Pinned hand-probe case: two merged groups + a shifted non-merged
    singleton + bool/string/out-of-range/duplicate deps in one case."""
    data = [
        _story("s0.md", skills=("long-form",)),    # group A (lead 0)
        _story("s1.md", skills=("long-form",)),    # group A
        _story("d0.svg", skills=("diagram",)),     # group B (lead 2)
        _story("d1.svg", skills=("diagram",)),     # group B
        _story("r.md", skills=("researcher",)),    # singleton (shifts 4 → 2)
        {"description": "Compile", "output_path": "out.pdf", "artifact_kind": "pdf",
         "required_skills": ["media-assembly"], "deliverable": True,
         "depends_on": [0, 1, 2, 3, 4, False, True, "x", 99, 0]},
    ]
    out = orch._bind_wide_artifacts(data)
    assert len(out) == 4  # mergedA, mergedB, research singleton, compile
    compile_spec = out[-1]
    assert compile_spec["depends_on"] == [0, 1, 2, False, True, "x", 99]
