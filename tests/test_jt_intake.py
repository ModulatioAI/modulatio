# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Brick B2 (foundation) — Job Template retrieval + bind at job intake, and the
conversational interview seam. The Leader greps its JT library against the
objective (or an explicit JT is bound for headless/cron), then interviews-or-
defaults the params. ask_operator is the seam the future streaming TUI drives;
None ⇒ bind defaults ("do it like I always do it"). Greenfield stays untouched.

This unit deliberately does NOT yet enforce output cardinality — that engine-
binds layer (the per-item override) lands with the output_contract."""

from __future__ import annotations

import pytest

from modulatio import job_templates as jt
from modulatio import kickoff_history as kh
from modulatio import vault
from modulatio.orchestration import Orchestrator, RunSummary
from modulatio.types import Project


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(jt, "_JT_ROOT", tmp_path / "shared" / "job_templates")
    monkeypatch.setattr(jt, "_SEED_JT_ROOT", tmp_path / "seed" / "job_templates")
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path / "vault")
    code = "PHI"
    vault.init_project(code, "Philosophy", "obj", exist_ok=True)
    return code


def _orch(code, objective="Write a daily philosophy article", run_id="20260531T090000Z-aaa111"):
    pr = Project(code=code, name="Philosophy", objective=objective,
                 leader_model="stub", run_id=run_id, wiki_path=str(vault.project_dir(code)))
    o = Orchestrator(pr, {"leader": lambda p: "", "planner": lambda p: "",
                          "drafter": lambda p: "", "qc": lambda p: "", "researcher": lambda p: ""})
    return o, pr


def _make_jt(name="daily-philosophy", desc="A daily philosophy article", project_code=None,
             schema=(), code_pref=()):
    jt.create_job_template(
        name=name, description=desc, interview_body="# Interview\nConfirm theme.\n",
        output_spec=jt.OutputSpec(cardinality="one", naming="{theme} — Essay"),
        param_schema=schema, capability_preferences=code_pref, project_code=project_code,
    )


def _resolve(o, pr, **kw):
    s = RunSummary(project=pr)
    o._resolve_job_template(pr.objective, bound_jt_name=kw.get("bound_jt_name"),
                            bound_jt_params=kw.get("bound_jt_params"),
                            ask_operator=kw.get("ask_operator"), summary=s)
    return s


# ── #97 Part 2: the create-JT tool captures param_schema (the HARD prereq) ──


def _create_tool(o):
    return o._leader_function_tools()["create_job_template"].call


def test_create_job_template_tool_captures_param_schema(env):
    """#97 Part 2 (Nemo hole 5 / Hero Q6): the Leader's create_job_template tool must
    persist param_schema, or the engine's own JTs ship with no required blanks and the
    fit-gate is toothless on them forever. The library fn already accepts it; the tool
    wrapper must thread it through (it used to swallow extras via **_)."""
    o, pr = _orch(env)
    msg = _create_tool(o)(
        name="competitor-brief", description="One brief per competitor",
        interview="Confirm the competitors and region.", cardinality="per-item", per="competitors",
        param_schema=[
            {"name": "competitors", "type": "list[str]", "required": True, "prompt": "Who?"},
            {"name": "region", "type": "enum", "required": True, "enum": ["NA", "EU", "APAC"]},
            {"name": "lookback", "type": "int", "required": False, "default": 7},
        ],
    )
    assert "Created job template" in msg
    loaded = jt.load_with_metadata("competitor-brief", project_code=pr.code)
    names = {p.name for p in loaded.param_schema}
    assert names == {"competitors", "region", "lookback"}
    by = {p.name: p for p in loaded.param_schema}
    assert by["competitors"].required is True
    assert by["region"].enum == ("NA", "EU", "APAC")
    assert by["lookback"].required is False
    # and the gate now has teeth on this engine-created JT:
    assert loaded.unfilled_required({"region": "EU"}) == ["competitors"]
    assert loaded.enum_violations({"competitors": ["x"], "region": "Mars"}) == ["region"]


def test_create_job_template_tool_without_param_schema_still_works(env):
    """Back-compat: omitting param_schema yields a JT with an empty schema (legacy shape)."""
    o, pr = _orch(env)
    msg = _create_tool(o)(
        name="plain-doc", description="A plain doc", interview="Confirm.", cardinality="one",
    )
    assert "Created job template" in msg
    loaded = jt.load_with_metadata("plain-doc", project_code=pr.code)
    assert loaded.param_schema == ()


# ── #97 Part 1: the bind gate refuses a wedge (Decision B) ────────────────


def test_explicit_bind_refused_when_required_param_unfillable(env):
    """Decision B: an explicit/cron bind to a JT whose required param can't be filled
    (absent OR present-but-empty) is REFUSED — the corrupt template does NOT bind, and
    a refusal is recorded so the run can derive/skip instead of mis-running."""
    _make_jt(name="brief", schema=(
        jt.ParamField(name="competitors", type="list[str]", required=True),
    ))
    o, pr = _orch(env)
    # empty-list required param → unfillable (the missing_required bypass)
    s = _resolve(o, pr, bound_jt_name="brief", bound_jt_params={"competitors": []})
    assert o._bound_jt is None                       # refused, not bound
    assert o._jt_refusal is not None
    assert o._jt_refusal["name"] == "brief"
    assert "competitors" in o._jt_refusal["reason"]
    assert s.job_slug is None                         # no JT folder named


def test_explicit_bind_refused_when_value_out_of_enum(env):
    """R1 (Hero): a supplied value outside a declared enum is refused (present-but-out-of-contract)."""
    _make_jt(name="regional", schema=(
        jt.ParamField(name="region", type="enum", required=True, enum=("NA", "EU", "APAC")),
    ))
    o, pr = _orch(env)
    _resolve(o, pr, bound_jt_name="regional", bound_jt_params={"region": "Atlantis"})
    assert o._bound_jt is None
    assert o._jt_refusal is not None and "region" in o._jt_refusal["reason"]


def test_explicit_bind_refused_when_per_driver_empty(env):
    """A per-item JT whose fan-out driver param is empty can't run → refused."""
    jt.create_job_template(
        name="per-comp", description="one per competitor", interview_body="b",
        output_spec=jt.OutputSpec(cardinality="per-item", per="competitors"),
        param_schema=(jt.ParamField(name="competitors", type="list[str]", required=True),),
        project_code=None,
    )
    o, pr = _orch(env)
    _resolve(o, pr, bound_jt_name="per-comp", bound_jt_params={"competitors": []})
    assert o._bound_jt is None
    assert o._jt_refusal is not None


def test_fitting_explicit_bind_still_binds(env):
    """No false-refuse: a bind that supplies all required, in-contract params binds unchanged."""
    _make_jt(name="brief2", schema=(
        jt.ParamField(name="competitors", type="list[str]", required=True),
    ))
    o, pr = _orch(env)
    s = _resolve(o, pr, bound_jt_name="brief2", bound_jt_params={"competitors": ["acme"]})
    assert o._bound_jt is not None
    assert o._bound_jt.name == "brief2"
    assert o._jt_refusal is None
    assert s.job_slug == "brief2"


def test_legacy_no_schema_jt_still_binds(env):
    """Back-compat: a legacy JT with no param_schema has nothing required → fit passes → binds."""
    jt.create_job_template(name="legacy", description="d", interview_body="b", project_code=None)
    o, pr = _orch(env)
    _resolve(o, pr, bound_jt_name="legacy", bound_jt_params={})
    assert o._bound_jt is not None and o._bound_jt.name == "legacy"
    assert o._jt_refusal is None


def test_cron_refused_bind_skips_the_slot(env):
    """R2 (Hero): a refused explicit bind under on_refused='skip' (the cron default)
    SKIPS the slot — no greenfield substitute runs, the refused template name is
    recorded for the visible gap, and the run does not decompose any goals."""
    _make_jt(name="brief", schema=(jt.ParamField(name="topic", required=True),))
    o, pr = _orch(env)
    summary = o.kickoff(pr.objective, bound_jt_name="brief", bound_jt_params={"topic": ""},
                        on_refused="skip")
    assert summary.skipped_refused_jt == "brief"
    assert summary.goals == []                       # skipped before decompose
    assert o._bound_jt is None
    # Hero m1: the skip surface must carry the WHY, not just the name — a slot skips
    # every cycle until a human fixes it; the reason is the single most useful string.
    assert summary.skipped_refused_reason is not None
    assert "topic" in summary.skipped_refused_reason


def test_engine_created_jt_is_gated_end_to_end(env):
    """Hero Q6 build-order suite-property: create a JT via the engine's OWN
    create_job_template tool, then bind it with the required param empty — the gate
    must REFUSE. This test cannot pass unless param_schema capture (Part 2) landed
    before the bind gate (Part 1), so the HARD build order is a suite invariant, not
    a prose promise."""
    o, pr = _orch(env)
    msg = _create_tool(o)(
        name="engine-made", description="d", interview="Confirm.", cardinality="one",
        param_schema=[{"name": "subject", "type": "str", "required": True}],
    )
    assert "Created job template" in msg
    # now an explicit bind that can't fill 'subject' must be refused on the engine's own JT
    o2, pr2 = _orch(env)
    _resolve(o2, pr2, bound_jt_name="engine-made", bound_jt_params={"subject": ""})
    assert o2._bound_jt is None
    assert o2._jt_refusal is not None and "subject" in o2._jt_refusal["reason"]


def test_malformed_bind_params_refuse_not_swallowed(env):
    """Nemo code-hull BLOCKER 1: malformed bound params (a non-dict) must become a CLEAN
    refusal — the engine binds the invariant. Previously an AttributeError escaped the
    interview/gate and the broad best-effort catch reset _jt_refusal to None, so a
    malformed cron bind silently greenfielded instead of skipping. The param path is now
    total: non-dict params → refused (never a swallowed crash, never a stale-None state)."""
    _make_jt(name="brief", schema=(jt.ParamField(name="topic", required=True),))
    o, pr = _orch(env)
    _resolve(o, pr, bound_jt_name="brief", bound_jt_params=["not", "a", "dict"])
    assert o._bound_jt is None                 # refused, not bound
    assert o._jt_refusal is not None           # refusal preserved (NOT reset to None)
    assert o._jt_refusal["name"] == "brief"


def test_malformed_bind_params_skip_the_slot_fires(env):
    """The downstream consequence of BLOCKER 1: with on_refused='skip', a malformed cron
    bind must SKIP the slot (refusal state survives to the skip gate), not run greenfield."""
    _make_jt(name="brief", schema=(jt.ParamField(name="topic", required=True),))
    o, pr = _orch(env)
    summary = o.kickoff(pr.objective, bound_jt_name="brief",
                        bound_jt_params=["not", "a", "dict"], on_refused="skip")
    assert summary.skipped_refused_jt == "brief"
    assert summary.goals == []


def test_refused_bind_renders_derive_prompt_in_block(env):
    """The converse surface: a refused bind surfaces a 'derive a fitting one' block
    (the third _job_template_block state), naming the template + the reason."""
    _make_jt(name="brief", schema=(jt.ParamField(name="topic", required=True),))
    o, pr = _orch(env)
    _resolve(o, pr, bound_jt_name="brief")  # headless, topic missing → refused
    block = o._job_template_block()
    assert "brief" in block
    assert "refused" in block.lower() or "doesn't fit" in block.lower()
    assert "derive" in block.lower()


# ── greenfield (no JT) stays byte-identical ───────────────────────────────


def test_greenfield_no_jt_leaves_state_clean(env):
    o, pr = _orch(env)  # no JTs in the library
    s = _resolve(o, pr)
    assert o._bound_jt is None
    assert o._bound_jt_params == {}
    assert s.job_slug is None  # Feature A falls back to the objective slug


# ── fuzzy match SURFACES a candidate (the Leader's choice, not auto-bind) ──


def test_objective_match_surfaces_candidate_not_bound(env):
    _make_jt(name="daily-philosophy", desc="A daily philosophy article")
    o, pr = _orch(env, objective="Write a daily philosophy article")
    s = _resolve(o, pr)
    # Using a JT is the Leader's choice — a fuzzy match is a nudge, not a bind.
    assert o._bound_jt is None
    assert ("daily-philosophy", "A daily philosophy article") in o._jt_candidates
    assert s.job_slug is None  # not bound → Feature A falls back to objective slug


def test_surfaced_candidates_appear_in_the_jt_block(env):
    _make_jt(name="daily-philosophy", desc="A daily philosophy article")
    o, pr = _orch(env, objective="Write a daily philosophy article")
    _resolve(o, pr)
    block = o._job_template_block()
    assert "your choice" in block.lower()
    assert "daily-philosophy" in block
    assert "OPTIONAL" in block  # never a requirement


# ── explicit bind (headless / cron) ───────────────────────────────────────


def test_explicit_bind_with_params_over_defaults(env):
    _make_jt(name="brief", desc="A brief",
             schema=(jt.ParamField(name="topic", required=True, prompt="Topic?"),
                     jt.ParamField(name="depth", default="shallow", prompt="Depth?")))
    o, pr = _orch(env, objective="something unrelated")
    s = _resolve(o, pr, bound_jt_name="brief", bound_jt_params={"topic": "AI"})
    assert o._bound_jt.name == "brief"
    assert o._bound_jt_params == {"topic": "AI", "depth": "shallow"}  # provided over default
    assert not s.recommendations  # required 'topic' was provided → no reservation


def test_explicit_bind_unknown_name_is_greenfield(env):
    o, pr = _orch(env)
    s = _resolve(o, pr, bound_jt_name="does-not-exist")
    assert o._bound_jt is None and s.job_slug is None


# ── run-as-always vs refresh (the conversational seam) ────────────────────


def test_run_as_always_uses_defaults_no_questions(env):
    # The interview runs on an EXPLICIT bind (operator chose the JT); headless
    # ask_operator=None → "do it like I always do it" (defaults, no questions).
    _make_jt(name="daily-philosophy",
             schema=(jt.ParamField(name="theme", default="stoicism", prompt="Theme?"),))
    o, pr = _orch(env)
    _resolve(o, pr, bound_jt_name="daily-philosophy", ask_operator=None)
    assert o._bound_jt_params == {"theme": "stoicism"}


def test_refresh_asks_operator_and_answer_overrides_default(env):
    _make_jt(name="daily-philosophy",
             schema=(jt.ParamField(name="theme", default="stoicism", prompt="Theme?"),
                     jt.ParamField(name="words", default=1200, prompt="Length?")))
    o, pr = _orch(env)
    asked = []
    def ask(q):
        asked.append(q)
        return "epicureanism" if "Theme" in q else ""  # answer theme, skip length
    _resolve(o, pr, bound_jt_name="daily-philosophy", ask_operator=ask)
    assert "Theme?" in asked and "Length?" in asked   # both non-prebound params asked
    assert o._bound_jt_params["theme"] == "epicureanism"  # answer overrides default
    assert o._bound_jt_params["words"] == 1200            # empty answer keeps default


def test_refresh_does_not_reask_prebound_params(env):
    _make_jt(name="brief",
             schema=(jt.ParamField(name="topic", prompt="Topic?"),
                     jt.ParamField(name="depth", default="deep", prompt="Depth?")))
    o, pr = _orch(env)
    asked = []
    def ask(q):
        asked.append(q)
        return "x"
    _resolve(o, pr, bound_jt_name="brief", bound_jt_params={"topic": "AI"}, ask_operator=ask)
    assert "Topic?" not in asked  # pre-bound → not re-asked
    assert "Depth?" in asked


def test_broken_ask_callback_never_breaks_binding(env):
    _make_jt(name="brief",
             schema=(jt.ParamField(name="topic", default="d", prompt="Topic?"),))
    o, pr = _orch(env)
    def boom(q):
        raise RuntimeError("UI died")
    _resolve(o, pr, bound_jt_name="brief", ask_operator=boom)
    assert o._bound_jt.name == "brief"
    assert o._bound_jt_params == {"topic": "d"}  # fell back to default, no crash


# ── missing required → honest PQR reservation ─────────────────────────────


def test_missing_required_headless_is_refused_not_bound(env):
    """#97 Decision B (was: bind-anyway + PQR warn): a headless bind missing a required
    param with no default now REFUSES — the under-specified template does not run, and a
    named refusal reservation is surfaced instead of silently mis-running."""
    _make_jt(name="brief",
             schema=(jt.ParamField(name="topic", required=True, prompt="Topic?"),))
    o, pr = _orch(env)
    s = _resolve(o, pr, bound_jt_name="brief")  # headless, no topic supplied
    assert o._bound_jt is None                   # refused, not bound
    assert o._jt_refusal is not None and "topic" in o._jt_refusal["reason"]
    assert any("topic" in r["concern"] and "brief" in r["concern"]
               for r in s.recommendations)


# ── kickoff-history carries the bound JT ──────────────────────────────────


def test_history_record_carries_bound_jt(env):
    _make_jt(name="daily-philosophy", schema=(jt.ParamField(name="theme", default="stoicism"),))
    o, pr = _orch(env, run_id="20260531T100000Z-bbb222")
    o._resolve_job_template(pr.objective, bound_jt_name="daily-philosophy",
                            bound_jt_params=None, ask_operator=None, summary=RunSummary(project=pr))
    o._record_kickoff_history(RunSummary(project=pr))
    rec = kh.recent(env)[0]
    assert rec.jt_id == "daily-philosophy"
    assert rec.bound_params == {"theme": "stoicism"}


# ── greenfield block is empty (byte-identical prompts) ────────────────────


def test_job_template_block_empty_greenfield(env):
    o, pr = _orch(env)
    _resolve(o, pr)  # no JTs in library → no candidates, not bound
    assert o._job_template_block() == ""


# ── output contract: HARD cardinality only ────────────────────────────────


def _bind(o, pr, name, **params):
    """Bind a JT explicitly (operator's choice) with given params."""
    o._resolve_job_template(pr.objective, bound_jt_name=name,
                            bound_jt_params=params or None, ask_operator=None,
                            summary=RunSummary(project=pr))


def test_jt_target_count_per_item_and_fixed():
    per = jt.JobTemplate(name="a", description="d", interview_body="b",
                         output_spec=jt.OutputSpec(cardinality="per-item", per="chapters"))
    fix = jt.JobTemplate(name="b", description="d", interview_body="b",
                         output_spec=jt.OutputSpec(cardinality="fixed:12"))
    one = jt.JobTemplate(name="c", description="d", interview_body="b",
                         output_spec=jt.OutputSpec(cardinality="one"))
    from modulatio.orchestration import Orchestrator
    f = Orchestrator._jt_target_count
    assert f(per, {"chapters": ["a", "b", "c"]}) == 3
    assert f(per, {"chapters": []}) is None       # empty list → setup error, no contract
    assert f(per, {}) is None                     # missing the list → None
    assert f(fix, {}) == 12
    assert f(one, {}) is None                     # single deliverable → no cardinality contract


def test_output_contract_per_item_demands_n_separate(env):
    _make_jt(name="anthology", desc="An anthology",
             schema=(jt.ParamField(name="stories", required=True, prompt="Which stories?"),))
    # per-item over the 'stories' list
    jt.save(jt.JobTemplate(name="anthology", description="An anthology",
                           interview_body="b",
                           output_spec=jt.OutputSpec(cardinality="per-item", per="stories",
                                                     naming="{stories} — Story"),
                           param_schema=(jt.ParamField(name="stories", required=True),)))
    o, pr = _orch(env)
    _bind(o, pr, "anthology", stories=["Nemo", "Cthulhu", "Conan"])
    block = o._job_template_block()
    assert "exactly 3 separate deliverables" in block
    assert "artifacts:" in block and "deliverable: true" in block
    assert "OVERRIDDEN" in block  # the batching rule is overridden for a hard N


def test_output_contract_one_cardinality_no_hard_block(env):
    _make_jt(name="report", desc="A single report")  # cardinality 'one', no required params
    o, pr = _orch(env)
    _bind(o, pr, "report")
    assert o._bound_jt is not None        # bound...
    assert o._job_template_block() == ""  # ...but nothing HARD to enforce → empty


# ── enforcement: verify + report firmly, never block ──────────────────────


class _DTask:
    def __init__(self, output_path, deliverable=True):
        self.output_path = output_path
        self.deliverable = deliverable


def test_short_plan_surfaces_firm_pqr_reservation(env):
    jt.save(jt.JobTemplate(name="anthology", description="d", interview_body="b",
                           output_spec=jt.OutputSpec(cardinality="per-item", per="stories"),
                           param_schema=(jt.ParamField(name="stories", required=True),)))
    o, pr = _orch(env)
    _bind(o, pr, "anthology", stories=["a", "b", "c", "d"])  # hard N = 4
    s = RunSummary(project=pr)
    s.tasks = [_DTask("s1.md"), _DTask("s2.md")]  # only 2 delivered
    o._validate_output_contract(s)
    assert any("HARD requirement of 4" in r["concern"] and "produced 2" in r["concern"]
               for r in s.recommendations)


def test_met_cardinality_no_reservation(env):
    jt.save(jt.JobTemplate(name="anthology", description="d", interview_body="b",
                           output_spec=jt.OutputSpec(cardinality="fixed:2"),
                           param_schema=()))
    o, pr = _orch(env)
    _bind(o, pr, "anthology")  # hard N = 2
    s = RunSummary(project=pr)
    s.tasks = [_DTask("a.md"), _DTask("b.md")]
    o._validate_output_contract(s)
    assert not s.recommendations  # contract met → nothing to flag


def test_greenfield_enforcement_is_noop(env):
    o, pr = _orch(env)
    _resolve(o, pr)  # no JT bound
    s = RunSummary(project=pr)
    s.tasks = [_DTask("a.md")]
    o._validate_output_contract(s)
    assert not s.recommendations
