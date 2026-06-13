# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Brick 4 — autonomous self-codification (the Alfred loop), Leader-judges
shape. The recurrence call is the Leader's JUDGMENT, not a mechanical count: at
end-of-run the Leader reads the team's recent QC FAIL verdicts (reliably logged
in qc_history) and decides what recurred enough to codify into a skill or an
improvement. The Leader's judgment is authoritative — NO QC re-check (QC already
voted via the repeated fails the lesson is built from, and a weaker QC can't
gate the smartest seat's draft); the engine binds the invariants — versioned,
git-committed, evidence consumed. No human.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from modulatio import lessons, qc_history, recoveries, skill_git, skills, vault
from modulatio.orchestration import Orchestrator, RunSummary
from modulatio.types import Project


# ── skill versioning round-trip ───────────────────────────────────────────


def test_skill_version_round_trips(tmp_path):
    skills._SKILLS_ROOT = tmp_path / "skills"  # type: ignore[attr-defined]
    p = skills.save(skills.Skill(name="v", description="d", prompt_template="b", version="3"))
    assert "version: 3" in p.read_text()
    assert skills._parse_file(p).version == "3"


def test_seed_skills_have_no_version():
    assert skills.load_with_metadata("qc").version is None


# ── the git layer ─────────────────────────────────────────────────────────


def test_git_ensure_repo_and_commit(tmp_path):
    d = tmp_path / "lib"
    assert skill_git.ensure_repo(d) is True and skill_git.in_work_tree(d) is True
    f = d / "x.md"
    f.write_text("hello")
    assert skill_git.commit_paths(d, [f], "first")
    assert skill_git.commit_paths(d, [f], "noop") is None  # nothing changed


def test_git_commit_outside_repo_is_none(tmp_path):
    f = tmp_path / "lone.md"
    f.write_text("x")
    assert skill_git.commit_paths(tmp_path, [f], "msg") is None


# ── the fail-verdict feed + consumed markers ──────────────────────────────


@pytest.fixture
def proj(tmp_path, monkeypatch):
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path / "vault")
    monkeypatch.setattr(skills, "_SKILLS_ROOT", tmp_path / "shared" / "skills")
    code = "COD"
    vault.init_project(code, "Codify", "obj", exist_ok=True)
    return code


def _seed_fail(code, domain, rationale, eid):
    qc_history.append_verdict(domain, code, qc_history.VerdictRecord(
        entry_id=eid,
        timestamp=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        task_id="T", producer_agent="p", qc_agent="qc",
        verdict="fail", defect_type="substantive", rationale=rationale, artifact_body="x"))


def _seed_pass(code, domain, eid):
    qc_history.append_verdict(domain, code, qc_history.VerdictRecord(
        entry_id=eid,
        timestamp=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        task_id="T", producer_agent="p", qc_agent="qc",
        verdict="pass", defect_type=None, rationale="ok", artifact_body="x"))


def test_unconsumed_fails_excludes_passes_and_consumed(proj):
    for i in range(3):
        _seed_fail(proj, "research", f"claim {i} had no source", f"f{i}")
    _seed_pass(proj, "research", "p0")
    fails = lessons.unconsumed_fails(proj)
    assert {f.entry_id for f in fails} == {"f0", "f1", "f2"}  # passes excluded
    lessons.mark_consumed(proj, ["f1"])
    assert {f.entry_id for f in lessons.unconsumed_fails(proj)} == {"f0", "f2"}


# ── the Leader-judges loop end to end ─────────────────────────────────────


def _orch(code, leader_decision: dict):
    def fence(d):
        return f"```json\n{json.dumps(d)}\n```"
    runners = {
        "leader": lambda p: fence(leader_decision),
        "planner": lambda p: "", "drafter": lambda p: "",
        "qc": lambda p: "",  # QC is NOT consulted in codification — present only for the roster
        "researcher": lambda p: "",
    }
    pr = Project(code=code, name="Codify", objective="obj",
                 leader_model="stub", wiki_path=str(vault.project_dir(code)))
    return Orchestrator(pr, runners), pr


def test_leader_codifies_new_skill_versioned_committed_consumed(proj):
    for i in range(3):
        _seed_fail(proj, "research", f"claim {i} unsourced", f"f{i}")
    decision = {"codifications": [{
        "action": "create", "name": "rigorous-citation",
        "description": "Cite every factual claim.",
        "capability_tags": ["research", "rigorous-sourcing"],
        "recurring_problem": "claims shipped without citations",
        "evidence_ids": ["f0", "f1", "f2"],
        "guidance": "Every factual claim must carry an inline citation.",
    }]}
    o, pr = _orch(proj, decision)
    o._post_run_codification(RunSummary(project=pr))

    sk = skills.load_with_metadata("rigorous-citation")
    assert sk.name == "rigorous-citation" and sk.version == "1"
    assert "inline citation" in sk.prompt_template
    assert skill_git.in_work_tree(skills._SKILLS_ROOT)
    assert lessons.consumed_ids(proj) == {"f0", "f1", "f2"}  # evidence consumed
    assert lessons.unconsumed_fails(proj) == []


def test_leader_improves_existing_skill_bumps_version(proj):
    skills.create_skill(name="rigorous-citation", description="Cite claims.",
                        prompt_template="Cite every claim.", version="1")
    for i in range(3):
        _seed_fail(proj, "research", f"weak source {i}", f"f{i}")
    decision = {"codifications": [{
        "action": "improve", "name": "rigorous-citation",
        "recurring_problem": "secondary sources cited instead of primary",
        "evidence_ids": ["f0", "f1", "f2"], "capability_tags": [],
        "guidance": "Prefer primary/authoritative sources over blogs.",
    }]}
    o, pr = _orch(proj, decision)
    o._post_run_codification(RunSummary(project=pr))
    sk = skills.load_with_metadata("rigorous-citation")
    assert sk.version == "2" and "primary/authoritative" in sk.prompt_template
    assert "Learned" in sk.prompt_template


def test_improve_omits_description_still_persists(proj):
    """On an IMPROVE the Leader correctly omits the description (it's extending
    an existing skill). With QC removed from the loop, that draft must persist
    directly — a live run earlier showed real QC wrongly rejecting every such
    improve on 'missing description', which is exactly why QC was taken out."""
    skills.create_skill(name="rigorous-citation", description="Cite all claims.",
                        prompt_template="Cite.", version="1")
    for i in range(3):
        _seed_fail(proj, "research", f"weak {i}", f"f{i}")
    decision = {"codifications": [{
        "action": "improve", "name": "rigorous-citation", "description": "",
        "recurring_problem": "weak sources", "evidence_ids": ["f0", "f1", "f2"],
        "capability_tags": [], "guidance": "Prefer primary sources."}]}
    o, pr = _orch(proj, decision)
    o._post_run_codification(RunSummary(project=pr))
    sk = skills.load_with_metadata("rigorous-citation")
    assert sk.version == "2"  # persisted despite the omitted description
    assert sk.description == "Cite all claims."  # the base description is kept
    assert "Prefer primary sources" in sk.prompt_template


def test_leader_returns_nothing_when_no_pattern(proj):
    for i in range(3):
        _seed_fail(proj, "research", f"distinct one-off {i}", f"f{i}")
    o, pr = _orch(proj, {"codifications": []})
    o._post_run_codification(RunSummary(project=pr))
    assert skills.list_skills(project_code=proj) == skills.list_skills()  # nothing new
    assert lessons.unconsumed_fails(proj)  # fails NOT consumed (eligible later)


def test_leader_judgment_is_authoritative_no_qc_consulted(proj):
    """The Leader's decision persists WITHOUT any QC re-check — the QC runner is
    never invoked during codification (QC already voted via the fails)."""
    qc_calls = {"n": 0}
    for i in range(3):
        _seed_fail(proj, "code", f"bad {i}", f"f{i}")
    decision = {"codifications": [{"action": "create", "name": "leader-authored",
                "description": "d", "capability_tags": [], "recurring_problem": "x",
                "evidence_ids": ["f0", "f1", "f2"], "guidance": "g"}]}
    o, pr = _orch(proj, decision)
    o.runners["qc"] = lambda p: qc_calls.__setitem__("n", qc_calls["n"] + 1) or ""
    o._post_run_codification(RunSummary(project=pr))
    assert skills.load_with_metadata("leader-authored").name == "leader-authored"
    assert lessons.consumed_ids(proj) == {"f0", "f1", "f2"}  # evidence consumed
    assert qc_calls["n"] == 0  # QC never consulted — the Leader is authoritative


def test_swallowed_leader_error_emits_breadcrumb(proj):
    """Nemo's Brick-4 hull gap: a swallowed error must be distinguishable from
    'nothing recurred'. A Leader call that raises emits a
    skill_codification_skipped breadcrumb — and the hook still never raises."""
    for i in range(3):
        _seed_fail(proj, "code", f"bad {i}", f"f{i}")
    events = []
    o, pr = _orch(proj, {"codifications": []})
    def boom(p):
        raise RuntimeError("missing API key")
    o.runners["leader"] = boom
    o.activity_callback = lambda ev: events.append(ev)
    o._post_run_codification(RunSummary(project=pr))  # must NOT raise
    phases = [getattr(ev, "phase", "") for ev in events]
    assert any(p == "skill_codification_skipped:leader_call_failed" for p in phases), phases
    assert lessons.unconsumed_fails(proj)  # nothing consumed on the error path


def test_breadcrumb_emit_is_raise_safe(proj):
    """The breadcrumb itself must never break a run: if the activity callback
    raises, _codification_skipped swallows it."""
    o, pr = _orch(proj, {"codifications": []})
    def raising_cb(ev):
        raise RuntimeError("subscriber blew up")
    o.activity_callback = raising_cb
    o._codification_skipped("leader_call_failed")  # must not raise


def test_pre_gate_below_three_fails_no_llm_call(proj, monkeypatch):
    _seed_fail(proj, "code", "only one", "f0")
    called = {"n": 0}
    def leader(p):
        called["n"] += 1
        return '```json\n{"codifications": []}\n```'
    o, pr = _orch(proj, {"codifications": []})
    o.runners["leader"] = leader
    o._post_run_codification(RunSummary(project=pr))
    assert called["n"] == 0  # pre-gate skips the Leader call below 3 fails


def test_kill_switch_disables_codification(proj, monkeypatch):
    for i in range(3):
        _seed_fail(proj, "code", f"f {i}", f"f{i}")
    monkeypatch.setenv("MODULATIO_SKILL_CODIFICATION", "0")
    decision = {"codifications": [{"action": "create", "name": "nope", "description": "d",
                "capability_tags": [], "recurring_problem": "x", "evidence_ids": ["f0"],
                "guidance": "g"}]}
    o, pr = _orch(proj, decision)
    o._post_run_codification(RunSummary(project=pr))
    assert skills.load_with_metadata("nope").name == ""


# ── #81 Fix F: provenance + learned_from round-trip (Nemo r3 / Hero R1·R2) ──────


def test_skill_provenance_and_learned_from_roundtrip(tmp_path, monkeypatch):
    """The win loop's honesty + replay guard both die if these fields don't survive
    a save→reload. provenance='win' + the consumed cluster signatures must persist."""
    from modulatio import skills

    # isolate the shared library to a tmp dir (CI runs pytest-randomly — never write
    # to the real shared skills root, and don't depend on another test's leftover).
    monkeypatch.setattr(skills, "_SKILLS_ROOT", tmp_path / "shared" / "skills")
    sk = skills.create_skill(
        name="codify-win-roundtrip", description="d", prompt_template="body",
        version="2", provenance="win",
        learned_from=("python_code|substantive|null-guard|code:add=S:rm=0:ctrl=+:lit=0",),
        project_code=None,
    )
    assert sk.provenance == "win" and len(sk.learned_from) == 1
    reloaded = skills.load_with_metadata("codify-win-roundtrip")
    assert reloaded.provenance == "win"
    assert reloaded.learned_from == sk.learned_from  # the applied-signature survives


def test_skill_seed_has_no_provenance_or_learned_from():
    from modulatio import skills

    qc = skills.load_with_metadata("qc")
    assert qc.provenance is None and qc.learned_from == ()


# ── #81 codify-the-win — the WIN half of the Alfred loop ──────────────────────

_GUARD_BEFORE = "def f(x):\n    return x\n"
_GUARD_AFTER = "def f(x):\n    if x is None:\n        return 0\n    return x\n"


def _seed_recovery(code, *, before=_GUARD_BEFORE, after=_GUARD_AFTER,
                   rationale="missing null guard on input", task_id="T",
                   artifact_kind="python_code", defect_type="substantive"):
    return recoveries.record_recovery(
        code, kind="qc_authored", artifact_kind=artifact_kind,
        defect_type=defect_type, task_id=task_id, defects="d",
        before=before, after=after, qc_rationale=rationale,
    )


def test_win_codifies_project_local_with_provenance_and_consumes_recovery_ledger(proj):
    """The keystone: ≥floor coherent recoveries → a PROJECT-LOCAL skill with
    provenance=win + the cluster signature in learned_from + a codify-win commit +
    recovery-ids consumed in the RECOVERY ledger, NEVER in lessons."""
    for i in range(3):
        _seed_recovery(proj, task_id=f"R{i}")
    decision = {"codifications": [{
        "action": "create", "name": "null-guard-inputs",
        "description": "Guard inputs before use.", "capability_tags": ["code"],
        "recurring_problem": "unguarded None inputs",
        "guidance": "Guard against None inputs before dereferencing them."}]}
    o, pr = _orch(proj, decision)
    summary = RunSummary(project=pr)
    o._post_run_win_codification(summary)

    sk = skills.load_with_metadata("null-guard-inputs", project_code=proj)
    assert sk.name == "null-guard-inputs" and sk.provenance == "win"
    assert len(sk.learned_from) == 1  # the cluster signature is recorded
    # PROJECT-LOCAL, not the shared library (Hero R2)
    assert not (skills._SKILLS_ROOT / "null-guard-inputs.md").exists()
    # recovery ids consumed in the SEPARATE recovery ledger, never lessons (Nemo #2)
    assert recoveries.unconsumed_recoveries(proj) == []
    assert lessons.consumed_ids(proj) == set()
    # the loud, non-independent spot-check recommendation (Hero R1b)
    blob = " ".join(r["concern"] + r["suggestion"] for r in summary.recommendations)
    assert "NON-INDEPENDENT" in blob and "spot-check" in blob


def test_clean_run_zero_fails_still_learns_the_win(proj):
    """Nemo r1 #7: the fail phase's <3 early-return must NOT suppress the win phase.
    A run with ZERO fails but ≥floor recoveries still codifies its win."""
    for i in range(3):
        _seed_recovery(proj, task_id=f"R{i}")
    # NO fails seeded at all
    decision = {"codifications": [{
        "action": "create", "name": "win-from-clean-run", "description": "d",
        "capability_tags": [], "recurring_problem": "x", "guidance": "g"}]}
    o, pr = _orch(proj, decision)
    o._post_run_codification(RunSummary(project=pr))  # the OUTER hook (both phases)
    assert skills.load_with_metadata("win-from-clean-run", project_code=proj).name \
        == "win-from-clean-run"


def test_win_replay_guard_skips_duplicate_block(proj):
    """Nemo r2 #3: if the cluster signature is already in the skill's learned_from,
    the improve append is SKIPPED (idempotent across a consume-after-commit replay)."""
    sig = "python_code|substantive|missing-null-guard-input|code:add=S:rm=0:ctrl=+:lit=0"
    skills.create_skill(name="guarded", description="d", prompt_template="ORIGINAL BODY",
                        version="1", provenance="win", learned_from=(sig,),
                        project_code=proj)
    spec = {"action": "improve", "name": "guarded", "recurring_problem": "x",
            "guidance": "DUPLICATE GUIDANCE", "capability_tags": [], "evidence_ids": ["r1"]}
    o, pr = _orch(proj, {"codifications": []})
    o._persist_codification(
        spec, ["guarded"], RunSummary(project=pr),
        provenance="win", consume_fn=recoveries.mark_consumed, project_code=proj,
        commit_prefix="codify-win", cluster_signature=sig,
    )
    sk = skills.load_with_metadata("guarded", project_code=proj)
    assert "DUPLICATE GUIDANCE" not in sk.prompt_template  # the append was skipped
    assert sk.version == "1"  # not bumped
    assert recoveries.consumed_ids(proj) == {"r1"}  # still consumed (lesson IS applied)


def test_win_below_floor_no_codify_no_consume(proj):
    for i in range(2):  # below floor 3
        _seed_recovery(proj, task_id=f"R{i}")
    decision = {"codifications": [{"action": "create", "name": "should-not-exist",
                "description": "d", "capability_tags": [], "recurring_problem": "x",
                "guidance": "g"}]}
    o, pr = _orch(proj, decision)
    o._post_run_win_codification(RunSummary(project=pr))
    assert skills.load_with_metadata("should-not-exist", project_code=proj).name == ""
    assert len(recoveries.unconsumed_recoveries(proj)) == 2  # NOT consumed, eligible later


def test_win_false_merge_never_reaches_leader(proj):
    """Hero R3: three GENUINELY different fixes sharing a rationale-key do NOT cluster,
    so no ≥floor cluster forms, the Leader is never called, and nothing is codified."""
    _seed_recovery(proj, task_id="R0", before=_GUARD_BEFORE, after=_GUARD_AFTER,
                   rationale="missing edge case handling here")
    _seed_recovery(proj, task_id="R1", before="def g():\n    return 1\n",
                   after="def g():\n    a=1\n    b=2\n    c=3\n    d=4\n    e=5\n    return a+b+c+d+e\n",
                   rationale="missing edge case handling here")
    _seed_recovery(proj, task_id="R2", before="x = '5'\n", after="x = 5\n",
                   rationale="missing edge case handling here")
    calls = {"n": 0}
    decision = {"codifications": [{"action": "create", "name": "wrong-merge",
                "description": "d", "capability_tags": [], "recurring_problem": "x",
                "guidance": "g"}]}
    o, pr = _orch(proj, decision)
    base = o.runners["leader"]
    o.runners["leader"] = lambda p: (calls.__setitem__("n", calls["n"] + 1), base(p))[1]
    o._post_run_win_codification(RunSummary(project=pr))
    assert calls["n"] == 0  # no cluster reached the floor → Leader never consulted
    assert skills.load_with_metadata("wrong-merge", project_code=proj).name == ""


def test_win_kill_switch_disables_both_phases(proj, monkeypatch):
    monkeypatch.setenv("MODULATIO_SKILL_CODIFICATION", "0")
    for i in range(3):
        _seed_recovery(proj, task_id=f"R{i}")
    decision = {"codifications": [{"action": "create", "name": "nope", "description": "d",
                "capability_tags": [], "recurring_problem": "x", "guidance": "g"}]}
    o, pr = _orch(proj, decision)
    o._post_run_codification(RunSummary(project=pr))  # outer gate honors the kill-switch
    assert skills.load_with_metadata("nope", project_code=proj).name == ""
    assert len(recoveries.unconsumed_recoveries(proj)) == 3  # untouched


def test_recovery_log_failure_never_reverses_completion(proj, monkeypatch):
    """Nemo r1 #5: the recovery is logged AFTER the task completes, guarded at the
    call site — a record_recovery throw must NOT reverse a completion."""
    from uuid import uuid4

    from modulatio.types import AssertionEvidence, Task, TaskStatus

    o, pr = _orch(proj, {"codifications": []})
    draft = vault.project_dir(proj) / "draft.py"
    draft.write_text("def f(x):\n    return x + 1  # a real non-trivial committed draft\n")
    t = Task(id="T-rec", project_id=uuid4(), goal_id="G", description="fix it",
             artifact_kind="python_code", qc_agent_id="qc")
    # avoid a real LLM patch call
    monkeypatch.setattr(
        o, "_qc_patch_artifact",
        lambda *a, **k: "def f(x):\n    if x is None:\n        return 0\n    return x + 1\n",
    )
    # force the recovery log to explode
    def _boom(*a, **k):
        raise RuntimeError("disk full")
    monkeypatch.setattr(recoveries, "record_recovery", _boom)

    last_qc = (
        AssertionEvidence(producer="qc", primary=True, check="null guard", passed=False),
        "add a null guard",
    )
    out = o._attempt_qc_fix_forward(t, draft, last_qc, RunSummary(project=pr))
    assert out is True                       # the rescue still reached its terminal
    assert t.status == TaskStatus.COMPLETED  # the log failure did not reverse completion
