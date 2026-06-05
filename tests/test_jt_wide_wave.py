# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Parallel-execution Phase 1 — the JT wide-wave collapse guard.

A bound PER-ITEM Job Template is a HARD operator requirement for N independent
same-kind deliverables. The PARALLEL DELIVERABLES contract steers the Leader to put
them in ONE goal (which the task-planner fans into a wide parallel wave), but prose
only bends — the Leader can still split the N items into N SEPARATE goals (the
serial goal loop then runs them one at a time: the anthology failure). The engine
merges the per-item goals back into one wide-wave goal. These tests cover the
collapse on the raw decompose dicts.
"""

from __future__ import annotations

import pytest

from modulatio import job_templates as jt
from modulatio import vault
from modulatio.orchestration import Orchestrator
from modulatio.types import Project


@pytest.fixture
def orch(tmp_path, monkeypatch):
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path / "vault")
    vault.init_project("ANT", "Anthology", "obj", exist_ok=True)
    pr = Project(code="ANT", name="Anthology", objective="obj",
                 leader_model="stub", run_id="20260605T090000Z-aaa111",
                 wiki_path=str(vault.project_dir("ANT")))
    return Orchestrator(pr, {"leader": lambda p: "", "planner": lambda p: "",
                             "drafter": lambda p: "", "qc": lambda p: ""})


def _bind(orch, *, per="stories", values, cardinality="per-item", artifact_kind="document"):
    orch._bound_jt = jt.JobTemplate(
        name="anthology", description="d", interview_body="",
        output_spec=jt.OutputSpec(cardinality=cardinality, per=per, artifact_kind=artifact_kind),
    )
    orch._bound_jt_params = {per: list(values)}


def _item(title: str) -> dict:
    return {
        "description": f"Write the story “{title}” for the anthology",
        "success_criteria": "A complete story.",
        "evidence_required": [{"kind": "artifact", "description": title,
                               "output_path": f"{title}.md"}],
    }


def _plain(desc: str, *, artifact=True) -> dict:
    ev = [{"kind": "artifact", "description": desc, "output_path": "x.md"}] if artifact else []
    return {"description": desc, "success_criteria": "done", "evidence_required": ev}


def test_collapse_per_item_goals_into_one(orch):
    """N item goals (each referencing one per-item value) → ONE merged goal that
    carries all N artifact requirements (→ a single artifacts task → wide wave)."""
    _bind(orch, values=["Alpha", "Bravo", "Charlie"])
    data = [_item("Alpha"), _item("Bravo"), _item("Charlie")]
    out = orch._collapse_jt_item_goals(data)
    assert len(out) == 1
    arts = [r for r in out[0]["evidence_required"] if r.get("kind") == "artifact"]
    assert len(arts) == 3
    assert "3" in out[0]["description"]  # "Produce all 3 ..."


def test_collapse_leaves_front_matter_and_assembly(orch):
    """Only the per-item story goals collapse — the front-matter goal and the
    assembly goal (which reference no per-item value) are left in place, in order."""
    _bind(orch, values=["Alpha", "Bravo", "Charlie"])
    data = [
        _plain("Front matter and title page"),
        _item("Alpha"), _item("Bravo"), _item("Charlie"),
        _plain("Assemble the finished anthology"),
    ]
    out = orch._collapse_jt_item_goals(data)
    assert len(out) == 3  # front matter + merged items + assembly
    assert out[0]["description"] == "Front matter and title page"
    assert "Produce all 3" in out[1]["description"]  # merged items took the items' slot
    assert out[2]["description"] == "Assemble the finished anthology"


def test_collapse_noop_when_already_one_goal(orch):
    """The correct shape — ONE goal referencing all items — is a no-op."""
    _bind(orch, values=["Alpha", "Bravo"])
    one = {
        "description": "Write both stories Alpha and Bravo",
        "success_criteria": "two stories",
        "evidence_required": [
            {"kind": "artifact", "output_path": "Alpha.md"},
            {"kind": "artifact", "output_path": "Bravo.md"},
        ],
    }
    out = orch._collapse_jt_item_goals([one])
    assert out == [one]


def test_collapse_noop_for_fixed_n(orch):
    """fixed:N has no per-item values to match → no engine collapse (prose only)."""
    _bind(orch, values=[], cardinality="fixed:3", per=None)
    orch._bound_jt_params = {}
    data = [_item("Alpha"), _item("Bravo"), _item("Charlie")]
    assert orch._collapse_jt_item_goals(data) == data


def test_collapse_noop_no_bound_jt(orch):
    orch._bound_jt = None
    data = [_item("Alpha"), _item("Bravo")]
    assert orch._collapse_jt_item_goals(data) == data


def test_collapse_noop_when_only_one_item_goal(orch):
    """A single item goal (e.g. only one value actually appeared) is not collapsed
    — never merge fewer than 2."""
    _bind(orch, values=["Alpha", "Bravo", "Charlie"])
    data = [_plain("Front matter"), _item("Alpha")]
    out = orch._collapse_jt_item_goals(data)
    assert out == data


def test_collapse_ignores_non_artifact_item_goal(orch):
    """A goal mentioning an item value but emitting NO artifact (a review/note) is
    not an item goal — don't fold it into the deliverable set."""
    _bind(orch, values=["Alpha", "Bravo"])
    review = {"description": "Review story Alpha for tone", "success_criteria": "noted",
              "evidence_required": [{"kind": "assertion"}]}
    data = [_item("Alpha"), _item("Bravo"), review]
    out = orch._collapse_jt_item_goals(data)
    # Alpha+Bravo collapse; the artifact-less review stays separate
    assert len(out) == 2
    assert any("Produce all 2" in g["description"] for g in out)
    assert any(g["description"] == "Review story Alpha for tone" for g in out)


# ── Nemo hull review (2026-06-05): proof-of-partition, not substring presence ──


def test_collapse_excludes_assembly_naming_all_values(orch):
    """Nemo B1 #1: an assembly/synthesis goal that names ALL item values (and emits
    an artifact) must NOT be folded into the item set — it references MULTIPLE
    values, so it's not a per-item goal. The N stories still collapse; assembly stays."""
    _bind(orch, values=["Alpha", "Bravo", "Charlie"])
    assembly = {
        "description": "Assemble the anthology of Alpha, Bravo, Charlie",
        "success_criteria": "one bound anthology",
        "evidence_required": [{"kind": "artifact", "output_path": "anthology.md"}],
    }
    data = [_item("Alpha"), _item("Bravo"), _item("Charlie"), assembly]
    out = orch._collapse_jt_item_goals(data)
    assert len(out) == 2  # merged-items + the (preserved) assembly goal
    assert any("Produce all 3" in g["description"] for g in out)
    asm = [g for g in out if "Assemble the anthology" in g["description"]]
    assert len(asm) == 1
    # the assembly's own artifact was NOT absorbed into the item set
    merged = [g for g in out if "Produce all 3" in g["description"]][0]
    paths = [r.get("output_path") for r in merged["evidence_required"]]
    assert "anthology.md" not in paths


def test_collapse_noop_for_short_values(orch):
    """Nemo B1 #2: short values over-match even with a word boundary ("A"/"B") →
    no safe per-item signal, fall back to the prose contract (no engine collapse)."""
    _bind(orch, values=["A", "B"])
    data = [
        {"description": "Write Atlas profile", "success_criteria": "x",
         "evidence_required": [{"kind": "artifact", "output_path": "atlas.md"}]},
        {"description": "Write biography for Bob", "success_criteria": "x",
         "evidence_required": [{"kind": "artifact", "output_path": "bob.md"}]},
    ]
    assert orch._collapse_jt_item_goals(data) == data


def test_collapse_word_boundary_not_substring(orch):
    """A value must match on a WORD BOUNDARY — "Ada" must not match "Adagio"."""
    _bind(orch, values=["Ada", "Bee", "Cal"])
    data = [
        {"description": "Write the Adagio movement", "success_criteria": "x",
         "evidence_required": [{"kind": "artifact", "output_path": "adagio.md"}]},
        _item("Bee"), _item("Cal"),
    ]
    # "Ada" doesn't match "Adagio" → value Ada uncovered → no clean bijection → no-op
    assert orch._collapse_jt_item_goals(data) == data


def test_collapse_noop_on_frontmatter_value_coincidence(orch):
    """A front-matter goal that coincidentally names ONE item value gives that value
    two candidates → ambiguous → bail (never mis-merge). Conservative by design."""
    _bind(orch, values=["Alpha", "Bravo", "Charlie"])
    front = {
        "description": "Front matter introducing the Alpha collection",
        "success_criteria": "title page",
        "evidence_required": [{"kind": "artifact", "output_path": "front.md"}],
    }
    data = [front, _item("Alpha"), _item("Bravo"), _item("Charlie")]
    # value "Alpha" now claimed by front + the Alpha story → ambiguous → no collapse
    assert orch._collapse_jt_item_goals(data) == data
