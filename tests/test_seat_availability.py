"""Seat availability handling in the engine — availability-class failures
back off instead of burning retries in a second, cool the seat so dispatch
stops feeding a dead endpoint, and route exhaustion to the QC backstop
instead of a terminal BLOCKED.

Born from live run 20260704T081413Z: a crashed LM Studio model burned whole
retry budgets in ~1s per task and stayed "best-available" the entire outage.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from litellm.exceptions import BadRequestError

from modulatio import roster, vault
from modulatio.orchestration import (
    _AVAILABILITY_RETRY_BACKOFF_S,
    Orchestrator,
    RunSummary,
)
from modulatio.types import Project, Task, TaskStatus

PROJECT_CODE = "SAV"


@pytest.fixture
def project(tmp_path: Path, monkeypatch) -> Project:
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    vault.init_project(PROJECT_CODE, "availability fixture", "obj")
    return Project(
        code=PROJECT_CODE, name="availability fixture", objective="obj",
        leader_model="stub",
        wiki_path=str(vault.project_dir(PROJECT_CODE)),
    )


def _runners() -> dict:
    return {
        "leader": lambda p: "", "planner": lambda p: "```json\n[]\n```",
        "drafter": lambda p: "", "qc": lambda p: "",
    }


def _crash_400() -> BadRequestError:
    return BadRequestError(
        message="Error code: 400 - {'error': 'The model has crashed without "
                "additional information. (Exit code: null)'}",
        model="m", llm_provider="openai",
    )


def _task(pid, tid="SAV-T-001", **kw) -> Task:
    # Explicit retry budget: these tests observe the BETWEEN-ATTEMPT backoff,
    # which needs redo attempts to exist (the shipped default is 0 = one QC
    # verdict, then QC-as-fixer).
    kw.setdefault("max_retries", 3)
    return Task(
        id=tid, project_id=pid, goal_id="SAV-G-001",
        description="produce the piece", assigned_agent_id="james",
        status=TaskStatus.DISPATCHED, **kw,
    )


def test_availability_failure_backs_off_and_cools_the_seat(project: Project):
    """An availability-class producer failure waits the bounded backoff
    (abort-event wait, so F8 stays responsive) and records the seat as
    cooling; a plain logic error does neither."""
    orch = Orchestrator(project, _runners())
    waits: list[float] = []
    orch.abort_event.wait = lambda s: waits.append(s) or False  # type: ignore[method-assign]

    def _dead_producer(task, corrective_notes=""):
        raise _crash_400()

    orch._producer_execute = _dead_producer  # type: ignore[assignment]
    orch._attempt_qc_fix_forward = (  # keep the terminal path deterministic
        lambda *a, **k: False)  # type: ignore[assignment]
    t = _task(project.id)
    orch._run_task_with_redo(t, RunSummary(project=project))

    assert waits == list(
        _AVAILABILITY_RETRY_BACKOFF_S[:len(waits)]
    ) and len(waits) >= 2, f"expected bounded backoff waits, got {waits}"
    assert orch._seat_in_cooldown("james")


def test_logic_error_neither_waits_nor_cools(project: Project):
    orch = Orchestrator(project, _runners())
    waits: list[float] = []
    orch.abort_event.wait = lambda s: waits.append(s) or False  # type: ignore[method-assign]

    def _buggy_producer(task, corrective_notes=""):
        raise ValueError("template placeholder missing")

    orch._producer_execute = _buggy_producer  # type: ignore[assignment]
    orch._attempt_qc_fix_forward = lambda *a, **k: False  # type: ignore[assignment]
    t = _task(project.id)
    orch._run_task_with_redo(t, RunSummary(project=project))

    assert waits == []
    assert not orch._seat_in_cooldown("james")


def test_qc_phase_outage_does_not_cool_the_producer_seat(project: Project):
    """Attribution guard: the QC seat's outage must not poison the PRODUCER's
    health record."""
    orch = Orchestrator(project, _runners())
    orch.abort_event.wait = lambda s: False  # type: ignore[method-assign]

    def _fine_producer(task, corrective_notes=""):
        p = orch._resolve_draft_path(task)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("draft body")
        return p, "sha256:x", 3

    def _dead_qc(task, draft_path, checksum):
        raise _crash_400()

    orch._producer_execute = _fine_producer  # type: ignore[assignment]
    orch._qc_review = _dead_qc  # type: ignore[assignment]
    orch._attempt_qc_fix_forward = lambda *a, **k: False  # type: ignore[assignment]
    t = _task(project.id)
    orch._run_task_with_redo(t, RunSummary(project=project))

    assert not orch._seat_in_cooldown("james")


def test_availability_exhaustion_routes_to_qc_backstop(project: Project):
    """A seat that stayed dead through every attempt is an availability
    exhaustion — the QC backstop produces the piece instead of a terminal
    BLOCKED (the #18 doctrine: recover, don't escalate)."""
    orch = Orchestrator(project, _runners())
    orch.abort_event.wait = lambda s: False  # type: ignore[method-assign]

    def _dead_producer(task, corrective_notes=""):
        raise _crash_400()

    rescued: list[str] = []

    def _qc_rescue(task, draft_path, last_qc, summary, **kw):
        rescued.append(task.id)
        task.status = TaskStatus.COMPLETED
        return True

    orch._producer_execute = _dead_producer  # type: ignore[assignment]
    orch._attempt_qc_fix_forward = _qc_rescue  # type: ignore[assignment]
    t = _task(project.id)
    orch._run_task_with_redo(t, RunSummary(project=project))

    assert rescued == ["SAV-T-001"]
    assert t.status is TaskStatus.COMPLETED


def test_cooldown_window_is_ninety_seconds(project: Project):
    """The seat sits out long enough for a provider to recover but short
    enough that a healthy producer is not stranded through a whole wave —
    the window the cooling seat is actually parked for."""
    import time as _time

    orch = Orchestrator(project, _runners())
    before = _time.monotonic()
    orch._note_seat_unavailable("james")
    parked = orch._seat_unavailable_until["james"] - before

    assert 89.0 <= parked <= 91.0, f"parked for {parked:.1f}s"


def test_cooling_a_seat_announces_the_endpoint_cooldown(project: Project):
    """A seat leaving the pool must say WHY. Without it the operator sees an
    idle producer and reads the cause off whatever event came last — the
    no-progress break — which is a different failure entirely."""
    seen: list = []
    orch = Orchestrator(project, _runners(), activity_callback=seen.append)

    orch._note_seat_unavailable("james")

    (event,) = [e for e in seen if e.phase == "seat_cooldown"]
    assert event.agent_id == "james"
    assert event.detail == {"seconds": 90.0}


def test_no_progress_break_does_not_cool_the_seat(project: Project):
    """The no-progress breaker ends a redo loop; it does not park the seat.
    Only an unavailable endpoint does, so the two must not be conflated."""
    orch = Orchestrator(project, _runners())

    orch._emit_activity(
        role=orch.default_producer_role, phase="redo_no_progress",
        task_id="T-1", agent_id="james",
    )

    assert not orch._seat_in_cooldown("james")


def test_dispatch_pool_excludes_cooling_seats(project: Project, monkeypatch):
    """A cooling seat leaves the dispatch pool for the cooldown window and
    returns after expiry; an all-producers-cooling pool degrades to the full
    roster (a cooling seat beats blocking every task)."""
    for aid, tier in (("james", "producer"), ("olivia", "producer"),
                      ("qc", "qc"), ("leader", "leader")):
        roster.add_agent(
            project_code=PROJECT_CODE, agent_id=aid, name=aid.title(),
            identity=f"You are {aid}.", skills=["drafter"], model="stub",
            tier=tier,
        )
    agents = roster.list_agents(PROJECT_CODE)
    orch = Orchestrator(project, _runners())

    assert {a.id for a in orch._dispatch_pool(agents)} == {
        "james", "olivia", "qc", "leader"}

    orch._note_seat_unavailable("james")
    pool = orch._dispatch_pool(agents)
    assert "james" not in {a.id for a in pool}
    assert "olivia" in {a.id for a in pool}

    # every producer cooling → degrade to the full roster, never starve
    orch._note_seat_unavailable("olivia")
    assert {a.id for a in orch._dispatch_pool(agents)} == {
        "james", "olivia", "qc", "leader"}

    # cooldown expiry brings the seat back
    import time as _time
    orch._seat_unavailable_until["james"] = _time.monotonic() - 1
    orch._seat_unavailable_until["olivia"] = _time.monotonic() - 1
    assert not orch._seat_in_cooldown("james")
    assert {a.id for a in orch._dispatch_pool(agents)} == {
        "james", "olivia", "qc", "leader"}


def test_hard_timeout_backs_off_and_cools_the_seat(project: Project):
    """The kill-boundary's SeatCallHardTimeout is availability-class: the
    redo loop backs off and the seat cools, same as a crashed model."""
    from modulatio.runners import SeatCallHardTimeout

    orch = Orchestrator(project, _runners())
    waits: list[float] = []
    orch.abort_event.wait = lambda s: waits.append(s) or False  # type: ignore[method-assign]

    def _wedged_producer(task, corrective_notes=""):
        raise SeatCallHardTimeout("chat test-model: no result within 0.1s")

    orch._producer_execute = _wedged_producer  # type: ignore[assignment]
    orch._attempt_qc_fix_forward = lambda *a, **k: False  # type: ignore[assignment]
    t = _task(project.id)
    orch._run_task_with_redo(t, RunSummary(project=project))

    assert len(waits) >= 2 and waits[0] == _AVAILABILITY_RETRY_BACKOFF_S[0]
    assert orch._seat_in_cooldown("james")


def test_hard_timeout_exhaustion_routes_to_qc_backstop(project: Project):
    from modulatio.runners import SeatCallHardTimeout

    orch = Orchestrator(project, _runners())
    orch.abort_event.wait = lambda s: False  # type: ignore[method-assign]

    def _wedged_producer(task, corrective_notes=""):
        raise SeatCallHardTimeout("chat test-model: no result within 0.1s")

    rescued: list[str] = []

    def _qc_rescue(task, draft_path, last_qc, summary, **kw):
        rescued.append(task.id)
        task.status = TaskStatus.COMPLETED
        return True

    orch._producer_execute = _wedged_producer  # type: ignore[assignment]
    orch._attempt_qc_fix_forward = _qc_rescue  # type: ignore[assignment]
    t = _task(project.id)
    orch._run_task_with_redo(t, RunSummary(project=project))

    assert rescued == ["SAV-T-001"]
    assert t.status is TaskStatus.COMPLETED
