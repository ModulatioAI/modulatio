# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Brick B4 — the setup-side Alfred loop. When a KIND of job keeps coming back,
the engine surfaces the recurrence (binds the trigger) and the Leader JUDGES
whether to codify a Job Template (its choice). Mirrors the skill self-
codification but reads kickoff-history job shapes, not QC fails."""

from __future__ import annotations

import json

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


def _orch(code, decision):
    def fence(d):
        return f"```json\n{json.dumps(d)}\n```"
    runners = {"leader": lambda p: fence(decision), "planner": lambda p: "",
               "drafter": lambda p: "", "qc": lambda p: "", "researcher": lambda p: ""}
    pr = Project(code=code, name="Philosophy", objective="o", leader_model="stub",
                 wiki_path=str(vault.project_dir(code)))
    return Orchestrator(pr, runners), pr


def _seed_runs(code, objective, n, redo=False):
    for i in range(n):
        kh.record(code, f"2026053{i}T09000{i}Z-{i:06d}", objective=objective,
                  operator_redo=redo)


_CREATE = {"codifications": [{
    "action": "create", "name": "daily-philosophy-article",
    "description": "A daily philosophy article",
    "recurring_shape": "daily philosophy article",
    "evidence_slugs": ["write a daily philosophy article"],
    "capability_preferences": ["long-form-writing"],
    "param_schema": [{"name": "theme", "type": "str", "required": False,
                      "default": "stoicism", "prompt": "Today's theme?"}],
    "output": {"cardinality": "one", "artifact_kind": "document", "naming": "{theme} — Essay"},
    "interview_body": "Confirm today's theme.",
}]}


# ── kill-switch + pre-gate ─────────────────────────────────────────────────


def test_kill_switch_disables(env, monkeypatch):
    monkeypatch.setenv("MODULATIO_JT_CODIFICATION", "0")
    _seed_runs(env, "Write a daily philosophy article", 3)
    o, pr = _orch(env, _CREATE)
    o._post_run_jt_codification(RunSummary(project=pr))
    assert jt.load_with_metadata("daily-philosophy-article").name == ""


def test_pregate_below_three_no_leader_call(env):
    _seed_runs(env, "Write a daily philosophy article", 2)  # only 2, no redo
    called = {"n": 0}
    o, pr = _orch(env, _CREATE)
    o.runners["leader"] = lambda p: called.__setitem__("n", called["n"] + 1) or "```json\n{}\n```"
    o._post_run_jt_codification(RunSummary(project=pr))
    assert called["n"] == 0  # pre-gate skipped the Leader entirely


def test_no_history_is_noop(env):
    o, pr = _orch(env, _CREATE)
    o._post_run_jt_codification(RunSummary(project=pr))
    assert jt.load_with_metadata("daily-philosophy-article").name == ""


# ── recurrence → create ────────────────────────────────────────────────────


def test_recurrence_creates_jt_versioned_committed_consumed(env):
    _seed_runs(env, "Write a daily philosophy article", 3)
    o, pr = _orch(env, _CREATE)
    o._post_run_jt_codification(RunSummary(project=pr))
    t = jt.load_with_metadata("daily-philosophy-article")
    assert t.name == "daily-philosophy-article" and t.version == "1"
    assert t.param_schema and t.param_schema[0].name == "theme"
    assert t.output_spec.naming == "{theme} — Essay"
    # evidence slug consumed → won't re-propose
    assert "write a daily philosophy article" in kh.consumed_slugs(env)


def test_consume_constrained_to_real_recurring_keys(env):
    # A paraphrased/typo'd evidence slug must NOT consume — only a
    # real recurring group key does (else a bad slug silently buries a shape, or
    # a missed key re-fires forever).
    _seed_runs(env, "Write a daily philosophy article", 3)
    bad_evidence = json.loads(json.dumps(_CREATE))
    bad_evidence["codifications"][0]["evidence_slugs"] = ["a-paraphrased-slug-that-does-not-match"]
    o, pr = _orch(env, bad_evidence)
    o._post_run_jt_codification(RunSummary(project=pr))
    # the JT still got created (the Leader's judgment stands)...
    assert jt.load_with_metadata("daily-philosophy-article").name == "daily-philosophy-article"
    # ...but the bogus slug was NOT consumed (only real recurring keys are)
    assert "a-paraphrased-slug-that-does-not-match" not in kh.consumed_slugs(env)
    assert kh.consumed_slugs(env) == set()  # nothing wrongly buried


def test_operator_redo_fires_below_three(env):
    _seed_runs(env, "Write a daily philosophy article", 1, redo=True)  # 1 run, but a redo
    o, pr = _orch(env, _CREATE)
    o._post_run_jt_codification(RunSummary(project=pr))
    assert jt.load_with_metadata("daily-philosophy-article").name == "daily-philosophy-article"


def test_consumed_slug_not_reproposed(env):
    _seed_runs(env, "Write a daily philosophy article", 3)
    kh.mark_consumed_slugs(env, ["write a daily philosophy article"])
    called = {"n": 0}
    o, pr = _orch(env, _CREATE)
    o.runners["leader"] = lambda p: called.__setitem__("n", called["n"] + 1) or "```json\n{}\n```"
    o._post_run_jt_codification(RunSummary(project=pr))
    assert called["n"] == 0  # already consumed → no recurrence left → no call


# ── improve + version-skew guard ───────────────────────────────────────────


def test_improve_bumps_version_and_merges_params(env):
    jt.create_job_template(name="daily-philosophy-article", description="d",
                           interview_body="base",
                           param_schema=(jt.ParamField(name="theme", default="stoicism"),))
    _seed_runs(env, "Write a daily philosophy article", 3)
    improve = {"codifications": [{
        "action": "improve", "name": "daily-philosophy-article",
        "recurring_shape": "add a length param",
        "evidence_slugs": ["write a daily philosophy article"],
        "param_schema": [{"name": "words", "type": "int", "required": False,
                          "default": 1200, "prompt": "Length?"}],
        "output": {"cardinality": "one"}, "interview_body": "also confirm length",
    }]}
    o, pr = _orch(env, improve)
    o._post_run_jt_codification(RunSummary(project=pr))
    t = jt.load_with_metadata("daily-philosophy-article")
    assert t.version == "2"
    names = {p.name for p in t.param_schema}
    assert names == {"theme", "words"}  # merged
    assert "Refined" in t.interview_body


def test_version_skew_guard_demotes_new_required_without_default(env):
    jt.create_job_template(name="brief", description="d", interview_body="b",
                           param_schema=(jt.ParamField(name="topic", default="AI"),))
    _seed_runs(env, "Write a brief", 3)
    improve = {"codifications": [{
        "action": "improve", "name": "brief", "recurring_shape": "x",
        "evidence_slugs": ["write a brief"],
        # a NEW required param with no default would break every bound cron at 3am
        "param_schema": [{"name": "deadline", "type": "str", "required": True, "default": None}],
        "output": {"cardinality": "one"},
    }]}
    o, pr = _orch(env, improve)
    o._post_run_jt_codification(RunSummary(project=pr))
    t = jt.load_with_metadata("brief")
    deadline = next(p for p in t.param_schema if p.name == "deadline")
    assert deadline.required is False  # demoted — additive-only for required


# ── breadcrumb on a swallowed error ────────────────────────────────────────


def test_swallowed_leader_error_emits_breadcrumb(env):
    _seed_runs(env, "Write a daily philosophy article", 3)
    events = []
    o, pr = _orch(env, _CREATE)
    def boom(p):
        raise RuntimeError("bad key")
    o.runners["leader"] = boom
    o.activity_callback = lambda ev: events.append(ev)
    o._post_run_jt_codification(RunSummary(project=pr))  # must not raise
    assert any(getattr(e, "phase", "") == "jt_codification_skipped:leader_call_failed" for e in events)


def test_existing_jt_index_excludes_jt_create(env):
    jt.create_job_template(name="real-jt", description="A real one", interview_body="b")
    o, pr = _orch(env, _CREATE)
    idx, names = o._existing_jt_index()
    assert "real-jt" in names and "jt-create" not in names
