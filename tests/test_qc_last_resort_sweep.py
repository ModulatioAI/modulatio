"""The goal-end QC last-resort sweep — QC repairs non-passing tasks and
PRODUCES the missing ones before a goal may ship "disappointed over a hole".

QC is the smarter, more expensive producer of last resort; a run must never
end with a missing piece while QC sits idle. The sweep drives the EXISTING
``_attempt_qc_fix_forward`` (BUILD rung authors from the contract when no
draft exists) over the goal's unfinished tasks in dependency order, feeding
each build bounded excerpts of its completed deps' artifacts.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from modulatio import store, vault
from modulatio.orchestration import Orchestrator, RunSummary
from modulatio.types import (
    Goal,
    GoalStatus,
    Project,
    StateTransition,
    Task,
    TaskStatus,
)

PROJECT_CODE = "QLS"


@pytest.fixture
def project(tmp_path: Path, monkeypatch) -> Project:
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    vault.init_project(PROJECT_CODE, "sweep fixture", "obj")
    return Project(
        code=PROJECT_CODE, name="sweep fixture", objective="obj",
        leader_model="stub",
        wiki_path=str(vault.project_dir(PROJECT_CODE)),
    )


def _runners() -> dict:
    return {
        "leader": lambda p: "", "planner": lambda p: "```json\n[]\n```",
        "drafter": lambda p: "", "qc": lambda p: "",
    }


def _orch(project: Project) -> Orchestrator:
    return Orchestrator(project, _runners())


def _goal(pid, **kw) -> Goal:
    return Goal(
        id="QLS-G-001", project_id=pid,
        description="ship the report", success_criteria="report exists", **kw,
    )


def _task(pid, tid: str, **kw) -> Task:
    return Task(
        id=tid, project_id=pid, goal_id="QLS-G-001",
        description=f"task {tid}", **kw,
    )


def _cascade_block(t: Task, dep: str) -> None:
    """Stamp the exact cascade-block signature the wave loop writes."""
    t.transitions.append(StateTransition(
        from_state=t.status.value, to_state=TaskStatus.BLOCKED.value,
        actor="planner",
        rationale=f"dependency failed: ['{dep}']; producer skipped",
    ))
    t.status = TaskStatus.BLOCKED


def _qc_answers(orch: Orchestrator, text: str = "QC-built artifact body."):
    """Stub the QC author seam; returns the captured prompts list."""
    prompts: list[str] = []

    def _call(agent_id, role, prompt, task_id=None):
        prompts.append(prompt)
        return text

    orch._run_agent_call = _call  # type: ignore[assignment]
    return prompts


# ── the sweep itself ─────────────────────────────────────────────────────────


def test_sweep_builds_a_cascade_blocked_leaf(project: Project):
    """A cascade-BLOCKED task with no draft gets QC-BUILT and completes."""
    orch = _orch(project)
    goal = _goal(project.id)
    t = _task(project.id, "QLS-T-001")
    _cascade_block(t, "QLS-T-000")
    prompts = _qc_answers(orch)
    summary = RunSummary(project=project)

    assert orch._qc_last_resort_sweep(goal, [t], summary) is True
    assert t.status is TaskStatus.COMPLETED
    assert t.qc_authored_fix is True
    assert "QLS-T-001" in summary.qc_authored_fixes
    assert len(prompts) == 1
    assert orch._resolve_draft_path(t).exists()


def test_sweep_builds_in_dep_order_and_feeds_dep_context(project: Project):
    """A missing sibling is built FIRST; the assembler is built SECOND and its
    prompt carries an excerpt of the freshly-built sibling — assembly from the
    real pieces, not fabrication."""
    orch = _orch(project)
    goal = _goal(project.id)
    sibling = _task(project.id, "QLS-T-001")
    _cascade_block(sibling, "QLS-T-000")
    assembler = _task(
        project.id, "QLS-T-002", depends_on=["QLS-T-001"], deliverable=True,
    )
    _cascade_block(assembler, "QLS-T-001")

    order: list[str] = []
    bodies = {"QLS-T-001": "SIBLING-CONTENT-XYZZY", "QLS-T-002": "ASSEMBLY"}
    prompts: dict[str, str] = {}

    def _call(agent_id, role, prompt, task_id=None):
        order.append(task_id)
        prompts[task_id] = prompt
        return bodies[task_id]

    orch._run_agent_call = _call  # type: ignore[assignment]
    summary = RunSummary(project=project)

    assert orch._qc_last_resort_sweep(goal, [sibling, assembler], summary) is True
    assert order == ["QLS-T-001", "QLS-T-002"]
    assert sibling.status is TaskStatus.COMPLETED
    assert assembler.status is TaskStatus.COMPLETED
    # the assembler's build prompt saw the sibling's actual content
    assert "SIBLING-CONTENT-XYZZY" in prompts["QLS-T-002"]


def test_sweep_skips_environmental_and_path_conflict_blocks(project: Project):
    """Environmental gaps and path-conflict blocks are NOT sweepable — QC
    authoring can't install a linter, and rebuilding both colliders recreates
    the overwrite hazard."""
    orch = _orch(project)
    goal = _goal(project.id)
    env = _task(project.id, "QLS-T-001")
    env.transitions.append(StateTransition(
        from_state="dispatched", to_state=TaskStatus.BLOCKED.value,
        actor="qc", rationale="environmental defect: linter missing",
    ))
    env.status = TaskStatus.BLOCKED
    clash = _task(project.id, "QLS-T-002")
    clash.transitions.append(StateTransition(
        from_state="pending", to_state=TaskStatus.BLOCKED.value,
        actor="planner",
        rationale="wave artifact-path conflict on 'out.md' with ['QLS-T-003']; "
                  "not run concurrently",
    ))
    clash.status = TaskStatus.BLOCKED
    prompts = _qc_answers(orch)
    summary = RunSummary(project=project)

    assert orch._qc_last_resort_sweep(goal, [env, clash], summary) is False
    assert env.status is TaskStatus.BLOCKED
    assert clash.status is TaskStatus.BLOCKED
    assert prompts == []


def test_sweep_aborts_after_two_consecutive_qc_failures(project: Project):
    """QC's own model down → two consecutive authoring failures abort the
    sweep gracefully (no third call, no crash, partial progress kept)."""
    orch = _orch(project)
    goal = _goal(project.id)
    tasks = [
        _task(project.id, f"QLS-T-00{i}") for i in (1, 2, 3)
    ]
    for t in tasks:
        _cascade_block(t, "QLS-T-000")

    calls: list[str] = []

    def _dead_qc(agent_id, role, prompt, task_id=None):
        calls.append(task_id)
        raise RuntimeError("connection error: qc model down")

    orch._run_agent_call = _dead_qc  # type: ignore[assignment]
    summary = RunSummary(project=project)

    assert orch._qc_last_resort_sweep(goal, tasks, summary) is False
    assert len(calls) == 2          # aborted after 2 consecutive failures
    assert all(t.status is TaskStatus.BLOCKED for t in tasks)


def test_sweep_respects_the_qc_fixer_flag(project: Project, monkeypatch):
    monkeypatch.setenv("MODULATIO_QC_FIXER", "0")
    orch = _orch(project)
    goal = _goal(project.id)
    t = _task(project.id, "QLS-T-001")
    _cascade_block(t, "QLS-T-000")
    prompts = _qc_answers(orch)

    assert orch._qc_last_resort_sweep(
        goal, [t], RunSummary(project=project)) is False
    assert prompts == []
    assert t.status is TaskStatus.BLOCKED


# ── Hook A: the disappointed exits sweep before shipping a hole ─────────────


def _verify_orch(project: Project, verdicts: list[str]):
    """Orchestrator whose leader returns scripted verdicts in sequence."""
    calls: list[str] = []

    def _leader(prompt: str) -> str:
        verdict = verdicts[min(len(calls), len(verdicts) - 1)]
        calls.append(verdict)
        return "```json\n" + json.dumps({
            "verdict": verdict, "rationale": "scripted", "report": "r",
        }) + "\n```"

    runners = {
        "leader": _leader, "planner": lambda p: "```json\n[]\n```",
        "drafter": lambda p: "", "qc": lambda p: "",
    }
    return Orchestrator(project, runners), calls


def test_disappointed_exit_sweeps_then_reverifies_once(project: Project):
    """Budget-exhausted disappointed goal + a sweepable hole → the sweep
    produces the piece, then EXACTLY ONE re-verify lands satisfied."""
    orch, leader_calls = _verify_orch(project, ["disappointed", "satisfied"])
    goal = _goal(project.id, status=GoalStatus.IN_PROGRESS)
    goal.retry_count = goal.max_retries        # redo budget spent → not can_redo
    t = _task(project.id, "QLS-T-001", deliverable=True)
    _cascade_block(t, "QLS-T-000")
    store.save_goal(project.code, goal)
    store.save_task(project.code, t)
    _qc_answers(orch)
    summary = RunSummary(project=project)

    orch._leader_verify_goal(goal, [t], summary)

    assert t.status is TaskStatus.COMPLETED
    assert leader_calls == ["disappointed", "satisfied"]
    assert summary.verdicts[-1]["verdict"] == "satisfied"


def test_sweep_is_one_shot_per_goal(project: Project):
    """If the goal is STILL disappointed after its one sweep, the exit chain
    proceeds — no second sweep, no recursion."""
    orch, leader_calls = _verify_orch(
        project, ["disappointed", "disappointed"])
    goal = _goal(project.id, status=GoalStatus.IN_PROGRESS)
    goal.retry_count = goal.max_retries
    t = _task(project.id, "QLS-T-001", deliverable=True)
    _cascade_block(t, "QLS-T-000")
    store.save_goal(project.code, goal)
    store.save_task(project.code, t)
    _qc_answers(orch)
    summary = RunSummary(project=project)

    orch._leader_verify_goal(goal, [t], summary)

    # verify → sweep → ONE re-verify → still disappointed → ships with
    # reservations; exactly two leader calls, goal terminal.
    assert leader_calls == ["disappointed", "disappointed"]
    assert goal.status is GoalStatus.COMPLETED


# ── Hook B: the zero-completed redo settle sweeps before settling ───────────


def test_zero_completed_redo_sweeps_instead_of_settling(project: Project):
    """A redo pass that completed nothing consults the sweep before
    _settle_zero_completed; sweep progress → re-verify, not a reservation."""
    orch, leader_calls = _verify_orch(project, ["satisfied"])
    goal = _goal(project.id, status=GoalStatus.IN_PROGRESS)
    t = _task(project.id, "QLS-T-001", deliverable=True)
    t.status = TaskStatus.QC_REJECTED
    store.save_goal(project.code, goal)
    store.save_task(project.code, t)
    _qc_answers(orch)

    def _redo_run(task, summary, initial_corrective_notes=""):
        task.status = TaskStatus.QC_REJECTED   # the pass completes nothing

    orch._run_task_with_redo = _redo_run  # type: ignore[assignment]
    orch._concurrent_waves_enabled = lambda *a, **k: False  # type: ignore[assignment]
    summary = RunSummary(project=project)

    orch._leader_auto_redo(goal, [t], "not good enough", Path("r.md"), summary)

    assert t.status is TaskStatus.COMPLETED     # QC produced it
    assert goal.status is GoalStatus.COMPLETED
    assert "satisfied" in leader_calls
    assert not any(
        "no completed work" in r.get("concern", "")
        for r in summary.recommendations
    )


# ── Cadre round 1 (Wild Bill BLOCK ×2 + Jenny MED) ──────────────────────────


def test_sweep_declines_out_of_root_output_path(project: Project, tmp_path):
    """WB BLOCK #1: a hostile/stale absolute output_path must NOT let the
    QC sweep author outside the artifacts root — the sweep declines and the
    task stays down."""
    escape = tmp_path / "wb-qc-escape.md"
    orch = _orch(project)
    goal = _goal(project.id)
    t = _task(project.id, "QLS-T-001", output_path=str(escape))
    _cascade_block(t, "QLS-T-000")
    prompts = _qc_answers(orch)
    summary = RunSummary(project=project)

    assert orch._qc_last_resort_sweep(goal, [t], summary) is False
    assert prompts == []                  # no QC call burned on a doomed task
    assert t.status is TaskStatus.BLOCKED
    assert not escape.exists()


def test_sweep_declines_traversal_output_path(project: Project, tmp_path):
    """WB BLOCK #1 (relative flavor): ../ traversal out of the artifacts root
    is refused the same way."""
    orch = _orch(project)
    goal = _goal(project.id)
    t = _task(project.id, "QLS-T-001", output_path="../../wb-escape.md")
    _cascade_block(t, "QLS-T-000")
    _qc_answers(orch)

    assert orch._qc_last_resort_sweep(
        goal, [t], RunSummary(project=project)) is False
    assert t.status is TaskStatus.BLOCKED


def test_environmental_block_anywhere_in_history_stays_down(project: Project):
    """WB BLOCK #2: an environmental block earlier in the transition history
    must not be laundered by a later transition — the sweep scans the whole
    history, not just transitions[-1]."""
    orch = _orch(project)
    goal = _goal(project.id)
    t = _task(project.id, "QLS-T-001")
    t.transitions.append(StateTransition(
        from_state="dispatched", to_state=TaskStatus.BLOCKED.value,
        actor="qc", rationale="environmental defect: linter missing",
    ))
    t.status = TaskStatus.BLOCKED
    _cascade_block(t, "QLS-T-000")        # the laundering transition
    prompts = _qc_answers(orch)

    assert orch._qc_last_resort_sweep(
        goal, [t], RunSummary(project=project)) is False
    assert prompts == []
    assert t.status is TaskStatus.BLOCKED


def test_block_writers_and_sweep_share_the_marker_constants():
    """Jenny MED: the discriminator binds on shared CONSTANTS, not free-form
    prose — the writers and the checker reference the same symbols, so a
    rationale rewording can't silently break the exclude set."""
    from modulatio import orchestration as om

    assert om._ENV_BLOCK_RATIONALE_PREFIX == "environmental defect:"
    assert om._PATH_CONFLICT_MARKER == "artifact-path conflict"
    import inspect

    env_writer = inspect.getsource(om.Orchestrator._block_for_environmental)
    assert "_ENV_BLOCK_RATIONALE_PREFIX" in env_writer
    path_writer = inspect.getsource(om.Orchestrator._block_wave_path_conflict)
    assert "_PATH_CONFLICT_MARKER" in path_writer
