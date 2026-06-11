"""Tests for Slice #82 PR-B — leader between-task reflection.

Originally landed as the "iterative Coordinator" hook; migrated in
Step 0 (2026-05-15) to a Leader-keyed runner call, since between-task
preference-driven reflection is conceptually the Leader's job.

Covers:
  - _extract_iterate_decision: parses fenced JSON, validates outcome,
    None on failure paths (no JSON, malformed JSON, missing/invalid
    outcome, empty response, non-dict result)
  - _LEADER_ITERATE_PROMPT renders required slots without raise
  - _leader_iterate dispatches to the runner only when the feature
    would invoke it; passes the right context to the prompt
  - _apply_iterate_revise updates description / artifact_kind /
    assignee_specialist; appends transition row
  - _apply_iterate_drop marks task ABANDONED; appends transition row
  - Integration: dispatch loop honors MODULATIO_LEADER_ITERATE env
    var (off → no iterate calls; on → iterate runs after each task)
"""

from __future__ import annotations

from uuid import uuid4

from modulatio import orchestration
from modulatio.types import Task, TaskStatus


# ── _extract_iterate_decision ─────────────────────────────────────────────


def test_extract_iterate_continue() -> None:
    response = (
        "Reasoning prose.\n\n"
        "```json\n"
        '{"outcome": "continue", "rationale": "next task fits as-is"}\n'
        "```"
    )
    out = orchestration._extract_iterate_decision(response)
    assert out is not None
    assert out["outcome"] == "continue"
    assert out["rationale"] == "next task fits as-is"


def test_extract_iterate_revise_task_payload_preserved() -> None:
    response = (
        "```json\n"
        '{"outcome": "revise-task", "rationale": "tighten",'
        ' "revise_task": {"task_id": "T-2", "description": "new"}}\n'
        "```"
    )
    out = orchestration._extract_iterate_decision(response)
    assert out is not None
    assert out["outcome"] == "revise-task"
    assert out["revise_task"]["task_id"] == "T-2"


def test_extract_iterate_drop_task_payload_preserved() -> None:
    response = (
        "```json\n"
        '{"outcome": "drop-task", "rationale": "covered already",'
        ' "drop_task": {"task_id": "T-3"}}\n'
        "```"
    )
    out = orchestration._extract_iterate_decision(response)
    assert out is not None
    assert out["outcome"] == "drop-task"
    assert out["drop_task"]["task_id"] == "T-3"


def test_extract_iterate_returns_none_on_no_json() -> None:
    assert orchestration._extract_iterate_decision("just prose") is None


def test_extract_iterate_returns_none_on_empty() -> None:
    assert orchestration._extract_iterate_decision("") is None


def test_extract_iterate_returns_none_on_invalid_outcome() -> None:
    response = (
        '```json\n{"outcome": "bogus", "rationale": "x"}\n```'
    )
    assert orchestration._extract_iterate_decision(response) is None


def test_extract_iterate_returns_none_on_missing_outcome() -> None:
    response = '```json\n{"rationale": "no outcome"}\n```'
    assert orchestration._extract_iterate_decision(response) is None


def test_extract_iterate_returns_none_on_list_top_level() -> None:
    response = '```json\n[{"outcome": "continue"}]\n```'
    assert orchestration._extract_iterate_decision(response) is None


def test_extract_iterate_returns_none_on_malformed_json() -> None:
    response = '```json\n{"outcome": "continue",,, broken}\n```'
    assert orchestration._extract_iterate_decision(response) is None


def test_extract_iterate_handles_balanced_object_without_fence() -> None:
    """Mirrors _extract_json's resolution order — bare JSON works too."""
    response = '{"outcome": "continue", "rationale": "ok"}'
    out = orchestration._extract_iterate_decision(response)
    assert out is not None
    assert out["outcome"] == "continue"


# ── _apply_iterate_revise / _apply_iterate_drop ───────────────────────────


def _make_task(
    task_id: str = "T-001",
    description: str = "original description",
    status: TaskStatus = TaskStatus.DISPATCHED,
) -> Task:
    return Task(
        id=task_id,
        project_id=uuid4(),
        goal_id="G-1",
        description=description,
        status=status,
        artifact_kind="text",
        assignee_specialist="drafter",
    )


class _StubOrchestrator:
    """Stand-in carrying just the methods we need to exercise. The
    real Orchestrator has heavy __init__; binding the methods directly
    via the class keeps these unit tests fast."""

    pass


def test_apply_iterate_revise_updates_description() -> None:
    task = _make_task()
    decision = {
        "outcome": "revise-task",
        "rationale": "tighten",
        "revise_task": {
            "task_id": task.id,
            "description": "tighter description",
        },
    }
    orchestration.Orchestrator._apply_iterate_revise(
        _StubOrchestrator(), decision, task  # type: ignore[arg-type]
    )
    assert task.description == "tighter description"
    # Transition row appended capturing the rewrite
    assert any(
        t.actor == "leader-iterate" for t in task.transitions
    )


def test_apply_iterate_revise_ignores_routing_fields() -> None:
    """Step 0 M5 (audit): revise-task narrowed
    to description-only. Pre-M5 the apply method also mutated
    ``artifact_kind`` and ``assignee_specialist``, which silently
    broke dispatch routing (capability floors, domain standards,
    audit-class QC fallback already selected before iterate runs).
    Those fields must now be IGNORED, and the ignore is recorded in
    the transition rationale so an auditor can see what the Leader
    asked for vs what the orchestrator honored."""
    task = _make_task()
    original_kind = task.artifact_kind
    decision = {
        "outcome": "revise-task",
        "rationale": "kind shift",
        "revise_task": {
            "task_id": task.id,
            "description": "new desc",
            "artifact_kind": "code",
        },
    }
    orchestration.Orchestrator._apply_iterate_revise(
        _StubOrchestrator(), decision, task  # type: ignore[arg-type]
    )
    # Description revised, the route-significant artifact_kind untouched.
    assert task.description == "new desc"
    assert task.artifact_kind == original_kind
    # The ignored field shows up in the transition rationale for audit.
    rationale = task.transitions[-1].rationale
    assert "ignored route-significant fields" in rationale
    assert "artifact_kind" in rationale


def test_apply_iterate_revise_no_op_on_empty_description() -> None:
    """Malformed payload (empty description) shouldn't corrupt the task."""
    task = _make_task()
    original = task.description
    decision = {
        "outcome": "revise-task",
        "rationale": "x",
        "revise_task": {"task_id": task.id, "description": "   "},
    }
    orchestration.Orchestrator._apply_iterate_revise(
        _StubOrchestrator(), decision, task  # type: ignore[arg-type]
    )
    assert task.description == original


def test_apply_iterate_drop_marks_abandoned() -> None:
    task = _make_task(status=TaskStatus.DISPATCHED)
    decision = {
        "outcome": "drop-task",
        "rationale": "covered by prior task",
        "drop_task": {"task_id": task.id},
    }
    orchestration.Orchestrator._apply_iterate_drop(
        _StubOrchestrator(), decision, task  # type: ignore[arg-type]
    )
    assert task.status == TaskStatus.ABANDONED
    assert any(
        t.actor == "leader-iterate" and "drop-task" in t.rationale
        for t in task.transitions
    )


# ── prompt template renders ───────────────────────────────────────────────


# Step 0 M7 (audit): the constant-renders test
# below was the only prompt-shape coverage we had — it exercised the
# fallback constant, not the seed body that production actually loads
# via _prompt(). That gap let two pre-existing bugs ship: unescaped
# JSON braces in the seed (crashed .format()) and missing placeholders
# in the seed (LLM never saw task context). The seed-body test that
# follows formats skills.load("leader-iterate") with the exact kwargs
# the orchestrator passes — both bugs would have failed it.

_ITERATE_RENDER_KWARGS: dict = {
    "code": "tst",
    "goal_id": "G-1",
    "goal_description": "ship the thing",
    "completed_tasks": "  - T-1 [completed] wrote intro",
    "next_task_id": "T-2",
    "next_task_description": "add tests",
    "next_task_artifact_kind": "code",
    "next_task_assignee": "engineer",
    "remaining_tasks": "  - T-3 [pending] write README",
    "repo_map": "## Repo map\n(empty)",
    "inbox_notes": "(no inbox notes this turn)",
    "pending_candidates": "(no pending candidates this turn)",
    "operator_context": "(operator-context test stub)",
}


def test_iterate_prompt_constant_renders() -> None:
    """Fallback coverage: the in-code _LEADER_ITERATE_PROMPT constant
    must format cleanly with every named kwarg the orchestrator passes.
    Production rarely hits this constant (the seed body is loaded
    instead); see test_iterate_prompt_seed_body_renders for the
    primary coverage."""
    out = orchestration._LEADER_ITERATE_PROMPT.format(**_ITERATE_RENDER_KWARGS)
    assert "G-1" in out
    assert "T-2" in out
    assert "add tests" in out
    assert "drop-task" in out
    assert "revise-task" in out


def test_iterate_prompt_seed_body_renders() -> None:
    """PRIMARY coverage: the seed body that production loads via
    _prompt('leader-iterate', ...) must format cleanly with the exact
    kwargs the orchestrator passes (see Orchestrator._leader_iterate).
    The kwargs must reach the LLM (placeholders present) AND the JSON
    code-block examples must survive .format() (braces escaped)."""
    from modulatio import skills

    body = skills.load("leader-iterate")
    assert body.strip(), "seed body must not be empty"
    out = body.format(**_ITERATE_RENDER_KWARGS)
    # Kwargs reached the rendered prompt.
    assert "G-1" in out
    assert "T-2" in out
    assert "add tests" in out
    assert "engineer" in out
    # Decision schema survived rendering.
    assert "drop-task" in out
    assert "revise-task" in out
    assert "continue" in out


def test_plan_prompt_seed_body_renders() -> None:
    """Same shape as the iterate seed test, for the task-plan seed
    (renamed from coordinator.md in Step 0 #7669aaf). Uses the kwargs
    set the orchestrator actually passes at the _plan_tasks site."""
    from modulatio import skills

    body = skills.load("task-plan")
    assert body.strip(), "seed body must not be empty"
    out = body.format(
        code="tst",
        goal_id="G-1",
        description="ship the thing",
        success_criteria="1 artifact",
        evidence_required="- artifact: a file",
        design_intent="(none)",
        available_skills="(none)",
        available_capabilities="(none)",
        team_capacity="(2 producers)",
        inbox_notes="(no inbox notes this turn)",
    )
    # Decision schema + scope discipline rules present.
    assert "required_skills" in out
    assert "required_capabilities" in out
    assert "Scope discipline" in out


# ── env-var gating — real dispatch-loop integration ──────────────────────
#
# Step 0 L1 (audit): the previous tests under
# this header only asserted `os.environ.get(...)` values — they did
# NOT exercise the dispatch loop, monkeypatch `_leader_iterate`, or
# prove the loop calls or skips the path. They were env-var read
# sanity at best. Replaced with two behavioral tests that drive a
# minimal multi-task kickoff and assert iterate is called the right
# number of times for the env state.


def _two_task_kickoff_runners() -> dict:
    """Stub runner set shaped for a single goal with two tasks. Used
    by the gating tests below to prove _leader_iterate fires (or
    doesn't) under MODULATIO_LEADER_ITERATE."""
    import json as _json

    leader_goals = [{
        "description": "two-task goal",
        "success_criteria": "two text artifacts",
        "evidence_required": [
            {"kind": "artifact", "description": "first text"},
            {"kind": "artifact", "description": "second text"},
        ],
    }]
    leader_verdict = {
        "verdict": "satisfied",
        "rationale": "stub",
        "report_body": "## Report\n\nstub.\n",
    }
    plan = [
        {"description": f"task {i}",
         "assignee_specialist": "drafter",
         "artifact_kind": "text",
         "required_skills": [],
         "evidence_required": [
             {"kind": "artifact", "description": f"text {i}"}
         ]}
        for i in (1, 2)
    ]
    qc_verdict = {
        "check": "ok", "passed": True, "notes": "", "defect_type": None,
    }

    def _leader(prompt: str) -> str:
        if "LEADER GOAL VERIFICATION" in prompt:
            return f"```json\n{_json.dumps(leader_verdict)}\n```"
        return f"```json\n{_json.dumps(leader_goals)}\n```"

    return {
        "leader": _leader,
        "planner": lambda _p: f"```json\n{_json.dumps(plan)}\n```",
        "drafter": lambda _p: "body\n",
        "qc": lambda _p: f"```json\n{_json.dumps(qc_verdict)}\n```",
        "researcher": lambda _p: "",
    }


def _make_two_task_project(tmp_path, monkeypatch):
    """Set up a real Project + Orchestrator pointed at a tmp vault.
    Wipes the seeded agents dir so dispatch falls through to
    role-keyed runners (matches the smoke driver pattern)."""
    import shutil as _shutil
    from modulatio import vault as _vault
    from modulatio.types import Project

    monkeypatch.setattr(_vault, "VAULT_ROOT", tmp_path)
    code = "LIT"
    _vault.init_project(code, "iterate-int fixture", "two-task goal")
    agents = tmp_path / code.lower() / "agents"
    _shutil.rmtree(agents, ignore_errors=True)
    agents.mkdir()
    return Project(
        code=code,
        name="iterate-int fixture",
        objective="two-task goal",
        leader_model="stub",
        wiki_path=str(tmp_path / code.lower()),
    )


def test_iterate_fires_when_operator_present_and_env_unset(
    tmp_path, monkeypatch,
) -> None:
    """#80 slice 7 (Q5 alignment): with an operator PRESENT and
    MODULATIO_LEADER_ITERATE unset, _leader_iterate now FIRES by default —
    presence governs visibility, not discovery-rate. (Pre-#80 it was suppressed
    under a watching operator, which leaked presence into the fix-rate; the
    watched-run authority-widening is bound separately, not by skipping
    discovery.)"""
    monkeypatch.delenv("MODULATIO_LEADER_ITERATE", raising=False)
    project = _make_two_task_project(tmp_path, monkeypatch)
    runners = _two_task_kickoff_runners()
    orch = orchestration.Orchestrator(project, runners, operator_present=True)

    calls: list = []

    def _spy(goal, all_tasks, next_task):  # type: ignore[no-untyped-def]
        calls.append(next_task.id)
        return None  # parse-fail path → orchestrator default-continues

    monkeypatch.setattr(orch, "_leader_iterate", _spy)
    orch.kickoff("two-task goal")
    # 2 tasks → 1 between-task reflection → fires once even under an operator.
    assert len(calls) == 1


def test_iterate_fires_when_autonomous_and_env_unset(
    tmp_path, monkeypatch,
) -> None:
    """Brick C — THE flip: with NO operator (autonomous, the daemon /
    cron / JT default) and MODULATIO_LEADER_ITERATE unset, _leader_iterate
    now runs by DEFAULT — the Leader is the only judgment past QC, so its
    self-correction is on without needing the env var."""
    monkeypatch.delenv("MODULATIO_LEADER_ITERATE", raising=False)
    project = _make_two_task_project(tmp_path, monkeypatch)
    runners = _two_task_kickoff_runners()
    # operator_present defaults to False (autonomous) — the production
    # daemon/cron/JT path. No env var set.
    orch = orchestration.Orchestrator(project, runners)
    assert orch._autonomous() is True

    calls: list = []

    def _spy(goal, all_tasks, next_task):  # type: ignore[no-untyped-def]
        calls.append(next_task.id)
        return None  # parse-fail path → orchestrator default-continues

    monkeypatch.setattr(orch, "_leader_iterate", _spy)
    orch.kickoff("two-task goal")
    # 2 tasks → 1 between-task reflection → 1 call, no env var needed.
    assert len(calls) == 1
    assert calls[0].endswith("T-002"), f"expected T-002 in {calls!r}"


def test_env_var_enabled_fires_iterate_between_tasks(tmp_path, monkeypatch) -> None:
    """MODULATIO_LEADER_ITERATE=1 → _leader_iterate called exactly
    (n_tasks - 1) times: once after T-1 (deciding T-2), no further
    call after the final task."""
    monkeypatch.setenv("MODULATIO_LEADER_ITERATE", "1")
    project = _make_two_task_project(tmp_path, monkeypatch)
    runners = _two_task_kickoff_runners()
    orch = orchestration.Orchestrator(project, runners)

    calls: list = []

    def _spy(goal, all_tasks, next_task):  # type: ignore[no-untyped-def]
        calls.append(next_task.id)
        return None  # parse-fail path → orchestrator default-continues

    monkeypatch.setattr(orch, "_leader_iterate", _spy)
    orch.kickoff("two-task goal")
    # 2 tasks → 1 between-task reflection → 1 call.
    assert len(calls) == 1
    # The single call decided on the SECOND task (T-2).
    assert calls[0].endswith("T-002"), f"expected T-002 in {calls!r}"
