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


# ── #101 C.0: DeliverableSpec — the declared-constraint vessel ──────────────


def test_round_trip_deliverable_spec(shared):
    t = jt.create_job_template(
        name="anthology", description="bound anthology", interview_body="# Interview\n",
        output_spec=jt.OutputSpec(cardinality="fixed:8", artifact_kind="document"),
        deliverable_spec=jt.DeliverableSpec(
            part_floor=2000, part_ceiling=3000, size_unit="words",
            required_structure=("title", "toc"), title="Have Robot, Will Travel"),
    )
    loaded = jt.load_with_metadata("anthology")
    assert loaded.deliverable_spec == t.deliverable_spec     # nested JSON round-trips
    assert loaded.deliverable_spec.part_floor == 2000
    assert loaded.deliverable_spec.required_structure == ("title", "toc")
    assert loaded.deliverable_spec.title == "Have Robot, Will Travel"


def test_deliverable_spec_absent_is_empty_and_omitted(shared):
    jt.create_job_template(name="plain", description="d", interview_body="b")
    loaded = jt.load_with_metadata("plain")
    assert loaded.deliverable_spec.is_empty()               # nothing declared == today
    raw = (jt._JT_ROOT / "plain.md").read_text()
    assert "deliverable_spec" not in raw                    # omitted to keep files lean


def test_parse_deliverable_spec_graceful_and_agnostic():
    assert jt._parse_deliverable_spec("").is_empty()
    assert jt._parse_deliverable_spec("not json").is_empty()
    assert jt._parse_deliverable_spec("[1, 2]").is_empty()  # non-dict → empty
    # product-agnostic: a data spec sized in ROWS round-trips with no document vocab
    d = jt._parse_deliverable_spec('{"part_floor": 500, "size_unit": "rows"}')
    assert d.part_floor == 500 and d.size_unit == "rows" and not d.is_empty()


def test_cardinality_variants(shared):
    for card, per in (("one", None), ("per-item", "items"), ("fixed:12", None)):
        jt.save(jt.JobTemplate(name=f"c-{card.replace(':','-')}", description="d",
                               interview_body="b",
                               output_spec=jt.OutputSpec(cardinality=card, per=per)))
    assert jt.load_with_metadata("c-one").output_spec.cardinality == "one"
    assert jt.load_with_metadata("c-per-item").output_spec.per == "items"
    assert jt.load_with_metadata("c-fixed-12").output_spec.cardinality == "fixed:12"


def test_parse_output_spec_rejects_out_of_grammar_cardinality():
    """Finding #30: the load path must enforce the same cardinality grammar as
    create_job_template — a hand-edited/migrated template can't smuggle a literal
    the engine branches on into the run. Off-grammar → 'one' (safe default)."""
    # bare digit normalizes to fixed:N (mirrors the create tool)
    assert jt._parse_output_spec('{"cardinality": "8"}').cardinality == "fixed:8"
    # case/space tolerant
    assert jt._parse_output_spec('{"cardinality": "Fixed: 5"}').cardinality == "fixed:5"
    # fixed:N with N<1 is not enforceable → one
    assert jt._parse_output_spec('{"cardinality": "fixed:0"}').cardinality == "one"
    assert jt._parse_output_spec('{"cardinality": "fixed:-3"}').cardinality == "one"
    # arbitrary junk → one (was: passed through verbatim)
    assert jt._parse_output_spec('{"cardinality": "lots"}').cardinality == "one"
    assert jt._parse_output_spec('{"cardinality": ""}').cardinality == "one"
    # per-item with no `per` has no fan-out contract → one
    assert jt._parse_output_spec('{"cardinality": "per-item"}').cardinality == "one"
    # per-item WITH a per param survives
    spec = jt._parse_output_spec('{"cardinality": "per-item", "per": "items"}')
    assert spec.cardinality == "per-item" and spec.per == "items"
    # the valid literals round-trip unchanged
    assert jt._parse_output_spec('{"cardinality": "one"}').cardinality == "one"
    assert jt._parse_output_spec('{"cardinality": "fixed:12"}').cardinality == "fixed:12"


def test_parse_deliverable_spec_rejects_bool_and_nonintegral_floor():
    """Finding #29: JSON bool/non-integral float must NOT coerce into a silent
    wrong part_floor/part_ceiling. `int(True)`==1, `int(False)`==0, `int(2.9)`==2
    are author mistakes, not constraints — they drop to None (no floor)."""
    # bool floor/ceiling → None, not 1/0
    d = jt._parse_deliverable_spec('{"part_floor": true, "part_ceiling": false}')
    assert d.part_floor is None and d.part_ceiling is None
    # non-integral float → None, not truncated
    assert jt._parse_deliverable_spec('{"part_floor": 2.9}').part_floor is None
    # integral float still accepted (3.0 → 3)
    assert jt._parse_deliverable_spec('{"part_floor": 3.0}').part_floor == 3
    # a real int is unaffected
    assert jt._parse_deliverable_spec('{"part_floor": 500}').part_floor == 500
    # a digit string still coerces (existing behavior preserved)
    assert jt._parse_deliverable_spec('{"part_floor": "750"}').part_floor == 750
    # garbage → None
    assert jt._parse_deliverable_spec('{"part_floor": "many"}').part_floor is None


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


def test_unfilled_required_is_strict_about_empty_values():
    """#97: the fit-gate's required check. UNLIKE missing_required (absent/None only),
    unfilled_required treats empty-string, whitespace-only, and (for list/per-driver
    fields) empty-list as MISSING — an explicit cron bind of {"topic": ""} must not pass."""
    t = jt.JobTemplate(
        name="t", description="d", interview_body="b",
        param_schema=(
            jt.ParamField(name="topic", type="str", required=True),
            jt.ParamField(name="competitors", type="list[str]", required=True),
            jt.ParamField(name="lookback", type="int", required=False),
        ),
    )
    # absent / None → missing (matches missing_required)
    assert t.unfilled_required({}) == ["topic", "competitors"]
    assert t.unfilled_required({"topic": None, "competitors": None}) == ["topic", "competitors"]
    # empty / whitespace string → missing (the bypass missing_required allows)
    assert t.unfilled_required({"topic": "", "competitors": ["x"]}) == ["topic"]
    assert t.unfilled_required({"topic": "   ", "competitors": ["x"]}) == ["topic"]
    # empty list for a list-typed required field → missing
    assert t.unfilled_required({"topic": "ai", "competitors": []}) == ["competitors"]
    # all filled, non-empty → nothing missing
    assert t.unfilled_required({"topic": "ai", "competitors": ["x"]}) == []
    # optional empties are fine — only REQUIRED fields are checked
    assert t.unfilled_required({"topic": "ai", "competitors": ["x"], "lookback": ""}) == []


def test_unfilled_required_empty_schema_is_back_compat():
    """A legacy JT with no param_schema has nothing required → fit passes (no false-refuse)."""
    t = jt.JobTemplate(name="legacy", description="d", interview_body="b")
    assert t.unfilled_required({}) == []
    assert t.unfilled_required({"anything": "here"}) == []


def test_enum_violations_flags_out_of_contract_values():
    """#97: a supplied value outside a declared non-empty enum is a
    present-but-out-of-contract misfit. List/per-driver fields: any non-member item flags.
    A field with no enum is unconstrained; a not-supplied field is the presence check's job."""
    t = jt.JobTemplate(
        name="t", description="d", interview_body="b",
        param_schema=(
            jt.ParamField(name="region", type="enum", enum=("NA", "EU", "APAC")),
            jt.ParamField(name="zones", type="list[str]", enum=("a", "b", "c")),
            jt.ParamField(name="freeform", type="str"),  # no enum → unconstrained
        ),
    )
    # in-contract scalar + list → no violations
    assert t.enum_violations({"region": "EU", "zones": ["a", "c"]}) == []
    # out-of-contract scalar
    assert t.enum_violations({"region": "Atlantis"}) == ["region"]
    # any non-member item in a list field flags the field
    assert t.enum_violations({"zones": ["a", "Atlantis"]}) == ["zones"]
    # unconstrained field never flags
    assert t.enum_violations({"freeform": "whatever"}) == []
    # absent enum field is NOT an enum violation (presence-check owns absence)
    assert t.enum_violations({}) == []
    # empty-string against an enum is absence-shaped, left to the presence check, not flagged here
    assert t.enum_violations({"region": ""}) == []


# ── name-dedup hard guard ─────────────────────────────────────────────────


def test_create_raises_on_name_collision(shared):
    jt.create_job_template(name="dup", description="d", interview_body="b")
    with pytest.raises(FileExistsError):
        jt.create_job_template(name="dup", description="d2", interview_body="b2")
