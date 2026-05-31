# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Brick B1b — kickoff history. The per-run record that lets the team notice a
job recurring (the setup-side analogue of the QC fail-verdict feed). Writer is
wired silently at end-of-kickoff; reader (windowed) is read by B4."""

from __future__ import annotations

import json

import pytest

from modulatio import kickoff_history as kh
from modulatio import vault


@pytest.fixture
def proj(tmp_path, monkeypatch):
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path / "vault")
    code = "PHI"
    vault.init_project(code, "Philosophy", "obj", exist_ok=True)
    return code


def _rid(n: int) -> str:
    # sortable run ids (oldest→newest as n grows)
    return f"2026053{n}T090000Z-{n:06d}"


# ── objective slug (the coarse grouping key) ──────────────────────────────


def test_objective_slug_normalizes():
    assert kh.objective_slug("Write a Philosophy Article!") == "write a philosophy article"
    assert kh.objective_slug("Write a philosophy article.") == kh.objective_slug("write a PHILOSOPHY article")
    assert kh.objective_slug("") == ""


# ── writer ────────────────────────────────────────────────────────────────


def test_record_writes_kickoff_json(proj):
    p = kh.record(proj, _rid(1), objective="Write a philosophy article")
    assert p is not None and p.name == "kickoff.json"
    data = json.loads(p.read_text())
    assert data["objective"] == "Write a philosophy article"
    assert data["objective_slug"] == "write a philosophy article"
    assert data["outcome"] == "completed"
    assert data["operator_redo"] is False
    assert data["jt_id"] is None
    assert data["created_at"]  # stamped


def test_record_noop_without_run_id(proj):
    assert kh.record(proj, None, objective="x") is None


def test_record_carries_jt_and_outcome_fields(proj):
    p = kh.record(proj, _rid(2), objective="x", outcome="failed",
                  jt_id="daily-essay", jt_version="1",
                  bound_params={"theme": "stoicism"}, operator_redo=True)
    data = json.loads(p.read_text())
    assert data["outcome"] == "failed"
    assert data["jt_id"] == "daily-essay"
    assert data["jt_version"] == "1"
    assert data["bound_params"] == {"theme": "stoicism"}
    assert data["operator_redo"] is True


def test_record_is_best_effort_never_raises(monkeypatch, proj):
    # A blow-up inside the write path returns None, never propagates.
    monkeypatch.setattr(kh.vault, "run_dir", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert kh.record(proj, _rid(3), objective="x") is None


# ── reader (windowed, most-recent-first) ──────────────────────────────────


def test_recent_most_recent_first(proj):
    for n in range(1, 4):
        kh.record(proj, _rid(n), objective=f"job {n}")
    recs = kh.recent(proj)
    assert [r.run_id for r in recs] == [_rid(3), _rid(2), _rid(1)]


def test_recent_limit_window(proj):
    for n in range(1, 6):
        kh.record(proj, _rid(n), objective=f"job {n}")
    recs = kh.recent(proj, limit=2)
    assert len(recs) == 2
    assert recs[0].run_id == _rid(5) and recs[1].run_id == _rid(4)


def test_recent_skips_missing_and_malformed(proj):
    kh.record(proj, _rid(1), objective="good")
    # a run dir with NO kickoff.json
    vault.run_dir(proj, _rid(2)).mkdir(parents=True, exist_ok=True)
    # a run dir with a malformed kickoff.json
    bad = vault.run_dir(proj, _rid(3))
    bad.mkdir(parents=True, exist_ok=True)
    (bad / "kickoff.json").write_text("{not json")
    recs = kh.recent(proj)
    assert [r.run_id for r in recs] == [_rid(1)]  # only the good one


def test_recent_empty_when_no_runs(proj):
    assert kh.recent(proj) == []


# ── the orchestrator wiring (silent record at end of kickoff) ─────────────


def test_orchestrator_records_history_at_kickoff_end(proj, monkeypatch):
    from modulatio.orchestration import Orchestrator, RunSummary
    from modulatio.types import Project
    run_id = _rid(7)
    pr = Project(code=proj, name="Philosophy", objective="Write a philosophy article",
                 leader_model="stub", run_id=run_id, wiki_path=str(vault.project_dir(proj)))
    o = Orchestrator(pr, {"leader": lambda p: "", "planner": lambda p: "",
                          "drafter": lambda p: "", "qc": lambda p: "", "researcher": lambda p: ""})
    o._record_kickoff_history(RunSummary(project=pr))
    rec = kh.recent(proj)[0]
    assert rec.run_id == run_id
    assert rec.objective == "Write a philosophy article"
    assert rec.outcome == "completed"


def test_orchestrator_records_failed_outcome_on_errors(proj):
    from modulatio.orchestration import Orchestrator, RunSummary
    from modulatio.types import Project
    run_id = _rid(8)
    pr = Project(code=proj, name="P", objective="o", leader_model="stub",
                 run_id=run_id, wiki_path=str(vault.project_dir(proj)))
    o = Orchestrator(pr, {"leader": lambda p: "", "planner": lambda p: "",
                          "drafter": lambda p: "", "qc": lambda p: "", "researcher": lambda p: ""})
    summary = RunSummary(project=pr)
    summary.errors = ["something broke"]
    o._record_kickoff_history(summary)
    assert kh.recent(proj)[0].outcome == "failed"
