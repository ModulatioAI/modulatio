# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Brick B1a — Job Template artifact format + loaders. A JT is the Leader's
self-authored job-SETUP memory (interview + param schema + output spec),
domain-agnostic, forked from the skill registry. These cover the markdown +
single-line-JSON round-trip, 3-location precedence, graceful degradation, and
the pure schema helpers (defaults / missing_required)."""

from __future__ import annotations

import pytest

from modulatio import job_templates as jt
from modulatio import vault


@pytest.fixture
def shared(tmp_path, monkeypatch):
    monkeypatch.setattr(jt, "_JT_ROOT", tmp_path / "shared" / "job_templates")
    monkeypatch.setattr(jt, "_SEED_JT_ROOT", tmp_path / "seed" / "job_templates")
    return tmp_path


# ── round-trip: nested param_schema / output_spec as single-line JSON ──────


def test_round_trip_nested_schema_and_output_spec(shared):
    t = jt.create_job_template(
        name="weekly-brief", description="Weekly competitor brief",
        interview_body="# Interview\nConfirm the list.\n",
        output_spec=jt.OutputSpec(cardinality="per-item", per="competitors",
                                  artifact_kind="document", naming="{competitor} — Brief"),
        param_schema=(
            jt.ParamField(name="competitors", type="list[str]", required=True, prompt="Who?"),
            jt.ParamField(name="lookback", type="int", default=7, prompt="Days back?"),
            jt.ParamField(name="tone", type="enum", default="terse",
                          enum=("terse", "narrative"), prompt="Terse or narrative?"),
        ),
        capability_preferences=("web-research", "summarization"), version="1",
    )
    loaded = jt.load_with_metadata("weekly-brief")
    assert loaded.name == "weekly-brief" and loaded.version == "1"
    assert loaded.output_spec == t.output_spec          # nested JSON round-trips
    assert loaded.param_schema == t.param_schema        # incl. order, defaults, enum
    assert loaded.capability_preferences == ("web-research", "summarization")
    assert "Confirm the list." in loaded.interview_body


def test_param_schema_with_colons_in_json_survives_parser(shared):
    # The naive first-colon frontmatter parser must keep JSON colons intact.
    jt.create_job_template(
        name="t", description="d", interview_body="body",
        param_schema=(jt.ParamField(name="url", type="str", prompt="Base URL (e.g. https://x.io)?"),),
    )
    loaded = jt.load_with_metadata("t")
    assert loaded.param_schema[0].prompt == "Base URL (e.g. https://x.io)?"


def test_cardinality_variants(shared):
    for card, per in (("one", None), ("per-item", "items"), ("fixed:12", None)):
        jt.save(jt.JobTemplate(name=f"c-{card.replace(':','-')}", description="d",
                               interview_body="b",
                               output_spec=jt.OutputSpec(cardinality=card, per=per)))
    assert jt.load_with_metadata("c-one").output_spec.cardinality == "one"
    assert jt.load_with_metadata("c-per-item").output_spec.per == "items"
    assert jt.load_with_metadata("c-fixed-12").output_spec.cardinality == "fixed:12"


# ── 3-location precedence: project > shared > seed ────────────────────────


def test_precedence_project_over_shared_over_seed(tmp_path, monkeypatch):
    monkeypatch.setattr(jt, "_JT_ROOT", tmp_path / "shared" / "job_templates")
    monkeypatch.setattr(jt, "_SEED_JT_ROOT", tmp_path / "seed" / "job_templates")
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path / "vault")
    code = "PRJ"
    vault.init_project(code, "Proj", "obj", exist_ok=True)

    # seed only
    jt._SEED_JT_ROOT.mkdir(parents=True)
    (jt._SEED_JT_ROOT / "x.md").write_text("---\nname: x\ndescription: SEED\n---\nseed body\n")
    assert jt.load_with_metadata("x", project_code=code).description == "SEED"

    # shared shadows seed
    jt.save(jt.JobTemplate(name="x", description="SHARED", interview_body="shared body"))
    assert jt.load_with_metadata("x", project_code=code).description == "SHARED"

    # project shadows shared
    jt.save(jt.JobTemplate(name="x", description="PROJECT", interview_body="proj body"),
            project_code=code)
    assert jt.load_with_metadata("x", project_code=code).description == "PROJECT"


def test_list_job_templates_unions_all_tiers(tmp_path, monkeypatch):
    monkeypatch.setattr(jt, "_JT_ROOT", tmp_path / "shared" / "job_templates")
    monkeypatch.setattr(jt, "_SEED_JT_ROOT", tmp_path / "seed" / "job_templates")
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path / "vault")
    code = "PRJ"
    vault.init_project(code, "Proj", "obj", exist_ok=True)
    jt._SEED_JT_ROOT.mkdir(parents=True)
    (jt._SEED_JT_ROOT / "seedjt.md").write_text("---\nname: seedjt\n---\nb\n")
    jt.save(jt.JobTemplate(name="sharedjt", description="d", interview_body="b"))
    jt.save(jt.JobTemplate(name="projjt", description="d", interview_body="b"), project_code=code)
    assert jt.list_job_templates(project_code=code) == ["projjt", "seedjt", "sharedjt"]


# ── graceful degradation (best-effort parsing, never raises) ──────────────


def test_missing_jt_returns_empty_not_error(shared):
    assert jt.load_with_metadata("does-not-exist").name == ""


def test_malformed_json_degrades_to_defaults(shared):
    root = shared / "shared" / "job_templates"
    root.mkdir(parents=True)
    (root / "bad.md").write_text(
        "---\nname: bad\ndescription: d\nparam_schema: {not valid json\noutput_spec: also bad\n---\nbody\n"
    )
    loaded = jt.load_with_metadata("bad")
    assert loaded.name == "bad"                       # still loads
    assert loaded.param_schema == ()                  # malformed schema → empty
    assert loaded.output_spec == jt.OutputSpec()      # malformed spec → default


def test_no_frontmatter_is_all_body(shared):
    root = shared / "shared" / "job_templates"
    root.mkdir(parents=True)
    (root / "plain.md").write_text("just an interview, no frontmatter\n")
    loaded = jt.load_with_metadata("plain")
    assert loaded.name == "plain"                     # falls back to the stem
    assert "just an interview" in loaded.interview_body


# ── pure schema helpers ───────────────────────────────────────────────────


def test_defaults_omits_params_without_a_default():
    t = jt.JobTemplate(
        name="t", description="d", interview_body="b",
        param_schema=(
            jt.ParamField(name="a", required=True),          # no default → omitted
            jt.ParamField(name="b", default=7),
            jt.ParamField(name="c", default="x"),
        ),
    )
    assert t.defaults() == {"b": 7, "c": "x"}


def test_missing_required_lists_absent_required_params():
    t = jt.JobTemplate(
        name="t", description="d", interview_body="b",
        param_schema=(
            jt.ParamField(name="a", required=True),
            jt.ParamField(name="b", required=True),
            jt.ParamField(name="c", required=False),
        ),
    )
    assert t.missing_required({}) == ["a", "b"]
    assert t.missing_required({"a": 1}) == ["b"]
    assert t.missing_required({"a": 1, "b": 2}) == []
    assert t.missing_required({"a": 1, "b": None}) == ["b"]  # None counts as absent


# ── name-dedup hard guard ─────────────────────────────────────────────────


def test_create_raises_on_name_collision(shared):
    jt.create_job_template(name="dup", description="d", interview_body="b")
    with pytest.raises(FileExistsError):
        jt.create_job_template(name="dup", description="d2", interview_body="b2")
