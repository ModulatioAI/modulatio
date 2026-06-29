"""Continuous-pull dispatch (Phase 2) — drives the real `_run_task_waves`
loop. 4-lens cadre-signed design: docs/design/2026-06-27-continuous-pull-dispatch.md.

Tasks here are NO_CONSTRAINT (empty required_skills) so they run the legacy path
without needing a roster — except the stall-guard test, which forces a never-
assigned task via a stubbed schedule_wave.
"""
from __future__ import annotations

import threading
import time
from uuid import uuid4

from modulatio import dispatch, roster, store, vault
from modulatio.orchestration import Orchestrator, RunSummary, TaskExecutionResult
from modulatio.types import Goal, GoalStatus, Project, Task, TaskStatus


def _orch(tmp_path, monkeypatch, code="CPL"):
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    vault.init_project(code, "continuous-pull test", "obj")
    vault.init_run(code, "run-1", "obj")
    # Real producer roster: NO_CONSTRAINT tasks route through schedule_wave
    # (capability + load-balance) like a real run — there is no rosterless path.
    for pid in ("p1", "p2", "p3", "p4"):
        roster.save(
            roster.Agent(id=pid, name=pid, identity=f"{pid} id",
                         model="stub", tier="producer", capacity_cap=1),
            code,
        )
    project = Project(
        code=code, name="CP", objective="obj", leader_model="stub",
        wiki_path=str(tmp_path / code.lower()), run_id="run-1",
    )
    return Orchestrator(project, {"drafter": lambda p: "x", "qc": lambda p: "ACCEPT"})


def _task(tid, *, deps=(), skills=(), out=None):
    t = Task(
        id=tid, project_id=uuid4(), goal_id="CPL-G-001", description="t",
        depends_on=list(deps), required_skills=list(skills), output_path=out,
    )
    t.status = TaskStatus.PENDING
    return t


def _goal():
    return Goal(
        id="CPL-G-001", project_id=uuid4(), description="g",
        success_criteria="sc", status=GoalStatus.IN_PROGRESS,
    )


def _run(orch, tasks):
    for t in tasks:
        store.save_task(orch.project.code, t, run_id=orch.project.run_id)
    summary = RunSummary(project=orch.project)
    orch._run_task_waves(_goal(), tasks, summary, {t.id: t for t in tasks})
    return summary


def _completes(t, *a, **k):
    t.status = TaskStatus.COMPLETED
    return TaskExecutionResult(task=t)


def test_continuous_completes_independent_tasks(tmp_path, monkeypatch):
    orch = _orch(tmp_path, monkeypatch)
    monkeypatch.setattr(orch, "_execute_task_isolated", _completes)
    tasks = [_task("CPL-T-001"), _task("CPL-T-002"), _task("CPL-T-003")]
    _run(orch, tasks)
    assert all(t.status == TaskStatus.COMPLETED for t in tasks)


def test_continuous_dependency_gating(tmp_path, monkeypatch):
    """A task with an incomplete dep is never submitted until the dep COMPLETES;
    then it runs. Proves the pull loop preserves dependency gating."""
    orch = _orch(tmp_path, monkeypatch)
    seen_order = []

    def fake(t, *a, **k):
        seen_order.append(t.id)
        t.status = TaskStatus.COMPLETED
        return TaskExecutionResult(task=t)

    monkeypatch.setattr(orch, "_execute_task_isolated", fake)
    a = _task("CPL-T-A")
    b = _task("CPL-T-B", deps=["CPL-T-A"])
    _run(orch, [a, b])
    assert a.status == b.status == TaskStatus.COMPLETED
    assert seen_order.index("CPL-T-A") < seen_order.index("CPL-T-B"), (
        "the dependent must run after its dependency"
    )


def test_continuous_pull_before_slow_sibling(tmp_path, monkeypatch):
    """THE core claim: a freed producer pulls the next ready task immediately,
    not behind the slowest in-flight task. B is gated slow; C depends on the fast
    A. C must run (and complete) WHILE B is still in flight — under a wave barrier
    C could not start until B finished its wave."""
    orch = _orch(tmp_path, monkeypatch)
    b_started = threading.Event()
    b_release = threading.Event()
    done = []

    def fake(t, *a, **k):
        if t.id == "CPL-T-B":
            b_started.set()
            b_release.wait(timeout=10)
        t.status = TaskStatus.COMPLETED
        done.append(t.id)
        return TaskExecutionResult(task=t)

    monkeypatch.setattr(orch, "_execute_task_isolated", fake)
    a = _task("CPL-T-A")
    b = _task("CPL-T-B")
    c = _task("CPL-T-C", deps=["CPL-T-A"])
    for t in (a, b, c):
        store.save_task(orch.project.code, t, run_id=orch.project.run_id)
    summary = RunSummary(project=orch.project)

    runner = threading.Thread(
        target=orch._run_task_waves,
        args=(_goal(), [a, b, c], summary, {t.id: t for t in (a, b, c)}),
    )
    runner.start()
    assert b_started.wait(timeout=10), "B should start"
    # While B is gated (still in flight), A→C should pull through and complete.
    deadline = time.time() + 10
    while "CPL-T-C" not in done and time.time() < deadline:
        time.sleep(0.02)
    assert "CPL-T-C" in done, "C must complete WHILE B is still in flight (no barrier)"
    assert "CPL-T-B" not in done, "B must still be in flight when C finished"
    b_release.set()
    runner.join(timeout=10)
    assert a.status == b.status == c.status == TaskStatus.COMPLETED


def test_continuous_crash_isolation_and_staging_sweep(tmp_path, monkeypatch):
    """An unexpected worker crash → that task BLOCKED, its `.staging/<tid>` swept,
    its in_flight claim released (siblings unaffected). Exercises `_collect`."""
    orch = _orch(tmp_path, monkeypatch)

    def fake(t, *a, **k):
        if t.id == "CPL-T-BOOM":
            (orch._scope_root() / ".staging" / t.id).mkdir(parents=True, exist_ok=True)
            (orch._scope_root() / ".staging" / t.id / "leak.txt").write_text("x")
            raise RuntimeError("boom")
        t.status = TaskStatus.COMPLETED
        return TaskExecutionResult(task=t)

    monkeypatch.setattr(orch, "_execute_task_isolated", fake)
    boom = _task("CPL-T-BOOM")
    ok = _task("CPL-T-OK")
    _run(orch, [boom, ok])
    assert boom.status == TaskStatus.BLOCKED
    assert ok.status == TaskStatus.COMPLETED, "the sibling still completes"
    assert not (orch._scope_root() / ".staging" / "CPL-T-BOOM").exists(), (
        "the crashed worker's staging dir must be swept"
    )


def test_continuous_same_path_conflict_blocks_both(tmp_path, monkeypatch):
    """W2: two tasks declaring the SAME output_path are BLOCKED (not serialized,
    not run concurrently) and never overwrite each other. Reuses the wave loop's
    block-the-group + CRITICAL-ticket policy."""
    orch = _orch(tmp_path, monkeypatch)
    ran = []

    def fake(t, *a, **k):
        ran.append(t.id)
        t.status = TaskStatus.COMPLETED
        return TaskExecutionResult(task=t)

    monkeypatch.setattr(orch, "_execute_task_isolated", fake)
    t1 = _task("CPL-T-P1", out="drafts/same.md")
    t2 = _task("CPL-T-P2", out="drafts/same.md")
    _run(orch, [t1, t2])
    assert t1.status == t2.status == TaskStatus.BLOCKED, (
        "same-output-path tasks are blocked, not run"
    )
    assert ran == [], "neither same-path task should have run"


def test_continuous_saturated_roster_stall_guard(tmp_path, monkeypatch):
    """J2 (must-fix): a runnable task that never gets a producer slot (schedule_wave
    never assigns it) must reach BLOCKED with a surfaced error — NOT a silent
    PENDING orphan when the loop exits."""
    import types as _types

    orch = _orch(tmp_path, monkeypatch)
    # skill-routed task + a schedule_wave that always declines (roster saturated).
    monkeypatch.setattr(
        dispatch, "schedule_wave",
        lambda *a, **k: _types.SimpleNamespace(assignments={}, deferred=(), gaps=()),
    )
    monkeypatch.setattr(orch, "_execute_task_isolated", _completes)
    stuck = _task("CPL-T-STUCK", skills=["drafter"])
    summary = _run(orch, [stuck])
    assert stuck.status == TaskStatus.BLOCKED, "an unassignable task must BLOCK, not orphan"
    assert any("CPL-T-STUCK" in e for e in summary.errors), "the stall must surface an error"


def test_continuous_global_cap_limits_concurrency(tmp_path, monkeypatch):
    """The pull loop honors MODULATIO_WAVE_GLOBAL_CAP — schedule_wave gates EVERY
    task (skills or not) on the global in-flight cap through the one unified path,
    so cap=1 → only ONE worker active even with idle producers + multiple ready
    tasks. (Replaces the old mixed-skill/legacy F1 test; there's no legacy path now.)"""
    monkeypatch.setenv("MODULATIO_WAVE_GLOBAL_CAP", "1")
    orch = _orch(tmp_path, monkeypatch)  # 4 idle producers
    active = 0
    max_active = 0
    lock = threading.Lock()
    gate = threading.Event()

    def fake(t, *a, **k):
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        gate.wait(timeout=5)
        with lock:
            active -= 1
        t.status = TaskStatus.COMPLETED
        return TaskExecutionResult(task=t)

    monkeypatch.setattr(orch, "_execute_task_isolated", fake)
    t1 = _task("CPL-T-1")
    t2 = _task("CPL-T-2")  # both NO_CONSTRAINT; 4 producers free
    for t in (t1, t2):
        store.save_task(orch.project.code, t, run_id=orch.project.run_id)
    summary = RunSummary(project=orch.project)
    runner = threading.Thread(
        target=orch._run_task_waves,
        args=(_goal(), [t1, t2], summary, {t.id: t for t in (t1, t2)}),
    )
    runner.start()
    time.sleep(0.6)  # let the pump submit; without the global cap both run at once
    observed = max_active
    gate.set()
    runner.join(timeout=10)
    assert observed <= 1, f"global cap=1 violated: {observed} workers ran concurrently"
    assert t1.status == t2.status == TaskStatus.COMPLETED


def test_pull_loop_repumps_while_a_call_hangs(tmp_path, monkeypatch):
    """Op A (loop-wedge): a hung in-flight call must NOT stall the whole loop. A
    task that only becomes dispatchable on a LATER pump still gets dispatched and
    completes WHILE another worker hangs — the loop re-pumps on a tick, not only on
    a future completion. Without the wait() timeout the loop blocks forever on the
    hung future and the second task never runs."""
    import types as _types

    from modulatio import orchestration
    monkeypatch.setattr(orchestration, "_PUMP_TICK_S", 0.1)
    orch = _orch(tmp_path, monkeypatch)
    a_hanging = threading.Event()
    a_release = threading.Event()
    b_done = threading.Event()

    # schedule_wave assigns A on the first pump; B only from the 2nd pump onward —
    # so B can ONLY dispatch if the loop re-pumps while A is hung (no future done).
    calls = {"n": 0}

    def sched(tasks, *a, **k):
        calls["n"] += 1
        assigns = {}
        for t in tasks:
            if t.id == "CPL-T-A":
                assigns[t.id] = "p1"
            elif t.id == "CPL-T-B" and calls["n"] >= 2:
                assigns[t.id] = "p2"
        return _types.SimpleNamespace(assignments=assigns, deferred=(), gaps=())

    monkeypatch.setattr(dispatch, "schedule_wave", sched)

    def fake(t, *a, **k):
        if t.id == "CPL-T-A":
            a_hanging.set()
            a_release.wait(timeout=10)
        t.status = TaskStatus.COMPLETED
        if t.id == "CPL-T-B":
            b_done.set()
        return TaskExecutionResult(task=t)

    monkeypatch.setattr(orch, "_execute_task_isolated", fake)
    a = _task("CPL-T-A", skills=["drafter"])
    b = _task("CPL-T-B", skills=["drafter"])
    for t in (a, b):
        store.save_task(orch.project.code, t, run_id=orch.project.run_id)
    summary = RunSummary(project=orch.project)
    runner = threading.Thread(
        target=orch._run_task_waves,
        args=(_goal(), [a, b], summary, {t.id: t for t in (a, b)}),
    )
    runner.start()
    assert a_hanging.wait(timeout=5), "A should start and hang"
    assert b_done.wait(timeout=5), (
        "B must dispatch + complete WHILE A hangs — the loop must re-pump on a tick, "
        "not block forever on the hung A future"
    )
    a_release.set()
    runner.join(timeout=10)
