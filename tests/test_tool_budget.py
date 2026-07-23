"""The task tool-call budget contract.

A task owns ONE durable tool-call budget: total ATTEMPTED tool calls across
every producer attempt, model fallback, retry, and task re-entry. The store —
not the isolated worker — owns the monotonic budget sequence: consumption
persists before each attempted call, a crash between consume and execution
burns the slot (never refunds it), and a stale worker/main-thread merge can
never decrease totals or restore an older fingerprint/streak tuple.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from modulatio import orchestration as orch_mod
from modulatio import runners as runners_mod
from modulatio import skills as skills_mod
from modulatio import store as store_mod
from modulatio import tools, vault
from modulatio.orchestration import Orchestrator, RunSummary
from modulatio.types import Project, Task, TaskStatus, ToolBudgetConflict


PROJECT_CODE = "TBG"
RUN_ID = "run-tbg-001"


@pytest.fixture
def project_with_run(tmp_path: Path, monkeypatch) -> Project:
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    vault.init_project(PROJECT_CODE, "budget test", "tool budget contract")
    vault.init_run(PROJECT_CODE, RUN_ID, "tool budget contract")
    return Project(
        code=PROJECT_CODE,
        name="budget test",
        objective="tool budget contract",
        leader_model="stub",
        wiki_path=str(tmp_path / PROJECT_CODE.lower()),
        run_id=RUN_ID,
    )


def _make_task(task_id: str = "TBG-T-001", **overrides) -> Task:
    fields = dict(
        id=task_id,
        project_id=uuid4(),
        goal_id="TBG-G-001",
        description="anything",
        max_retries=3,
    )
    fields.update(overrides)
    return Task(**fields)


def _saved_task(task_id: str = "TBG-T-001", **overrides) -> Task:
    task = _make_task(task_id, **overrides)
    store_mod.save_task(PROJECT_CODE, task, run_id=RUN_ID)
    return task


# ── store-owned consumption: the durable budget barrier ─────────────────────


def test_consume_increments_and_persists(project_with_run):
    _saved_task()
    updated = store_mod.consume_tool_call_budget(
        PROJECT_CODE, "TBG-T-001", expected_sequence=0,
        fingerprint="aaaa", run_id=RUN_ID)
    assert updated.tool_budget_sequence == 1
    assert updated.tool_calls_attempted == 1
    assert updated.tool_call_fingerprint == "aaaa"
    assert updated.tool_call_streak == 1
    # Durable BEFORE any execution: a fresh read observes the spent slot.
    canonical = store_mod.get_task(PROJECT_CODE, "TBG-T-001", run_id=RUN_ID)
    assert canonical.tool_calls_attempted == 1
    assert canonical.tool_budget_sequence == 1


def test_consume_same_fingerprint_extends_streak(project_with_run):
    _saved_task()
    seq = 0
    for expected_streak in (1, 2, 3):
        updated = store_mod.consume_tool_call_budget(
            PROJECT_CODE, "TBG-T-001", expected_sequence=seq,
            fingerprint="same", run_id=RUN_ID)
        seq = updated.tool_budget_sequence
        assert updated.tool_call_streak == expected_streak
    updated = store_mod.consume_tool_call_budget(
        PROJECT_CODE, "TBG-T-001", expected_sequence=seq,
        fingerprint="different", run_id=RUN_ID)
    assert updated.tool_call_streak == 1
    assert updated.tool_call_fingerprint == "different"
    assert updated.tool_calls_attempted == 4


def test_consume_stale_sequence_is_typed_conflict(project_with_run):
    _saved_task()
    store_mod.consume_tool_call_budget(
        PROJECT_CODE, "TBG-T-001", expected_sequence=0,
        fingerprint="aaaa", run_id=RUN_ID)
    with pytest.raises(ToolBudgetConflict) as exc_info:
        store_mod.consume_tool_call_budget(
            PROJECT_CODE, "TBG-T-001", expected_sequence=0,
            fingerprint="bbbb", run_id=RUN_ID)
    # The conflict carries the canonical record so the caller can resync;
    # the failed consume must not have advanced the authority.
    assert exc_info.value.canonical.tool_budget_sequence == 1
    canonical = store_mod.get_task(PROJECT_CODE, "TBG-T-001", run_id=RUN_ID)
    assert canonical.tool_calls_attempted == 1


def test_consume_missing_task_fails_closed(project_with_run):
    with pytest.raises(ToolBudgetConflict) as exc_info:
        store_mod.consume_tool_call_budget(
            PROJECT_CODE, "TBG-T-404", expected_sequence=0,
            fingerprint="aaaa", run_id=RUN_ID)
    assert exc_info.value.canonical is None
    # Fail closed means fail EMPTY: no record is conjured for a ghost task.
    assert store_mod.get_task(PROJECT_CODE, "TBG-T-404", run_id=RUN_ID) is None


def test_record_strike_bumps_strikes_not_attempted(project_with_run):
    _saved_task()
    updated = store_mod.consume_tool_call_budget(
        PROJECT_CODE, "TBG-T-001", expected_sequence=0,
        record_strike=True, run_id=RUN_ID)
    assert updated.storm_strikes == 1
    assert updated.tool_calls_attempted == 0
    assert updated.tool_budget_sequence == 1
    canonical = store_mod.get_task(PROJECT_CODE, "TBG-T-001", run_id=RUN_ID)
    assert canonical.storm_strikes == 1


def test_independent_tasks_have_independent_budgets(project_with_run):
    _saved_task("TBG-T-001")
    _saved_task("TBG-T-002")
    for _ in range(3):
        canonical = store_mod.get_task(PROJECT_CODE, "TBG-T-001", run_id=RUN_ID)
        store_mod.consume_tool_call_budget(
            PROJECT_CODE, "TBG-T-001",
            expected_sequence=canonical.tool_budget_sequence,
            fingerprint="aaaa", run_id=RUN_ID)
    other = store_mod.get_task(PROJECT_CODE, "TBG-T-002", run_id=RUN_ID)
    assert other.tool_calls_attempted == 0
    assert other.tool_budget_sequence == 0


# ── merge projection: the higher budget sequence wins, tuple intact ─────────


def test_stale_merge_cannot_decrease_budget(project_with_run):
    task = _saved_task()
    # Worker-side durable consumption advanced the canonical authority...
    seq = 0
    for fp in ("aaaa", "aaaa", "bbbb"):
        updated = store_mod.consume_tool_call_budget(
            PROJECT_CODE, "TBG-T-001", expected_sequence=seq,
            fingerprint=fp, run_id=RUN_ID)
        seq = updated.tool_budget_sequence
    # ...then a stale snapshot (pre-consumption task copy) merges.
    stale = task.model_copy(deep=True)
    assert stale.tool_budget_sequence == 0
    merged = store_mod.save_task_monotonic(PROJECT_CODE, stale, run_id=RUN_ID)
    assert merged.tool_budget_sequence == 3
    assert merged.tool_calls_attempted == 3
    assert merged.tool_call_fingerprint == "bbbb"
    assert merged.tool_call_streak == 1
    canonical = store_mod.get_task(PROJECT_CODE, "TBG-T-001", run_id=RUN_ID)
    assert canonical.tool_calls_attempted == 3


def test_stale_merge_cannot_restore_older_fingerprint_tuple(project_with_run):
    task = _saved_task()
    store_mod.consume_tool_call_budget(
        PROJECT_CODE, "TBG-T-001", expected_sequence=0,
        fingerprint="new!", run_id=RUN_ID)
    stale = task.model_copy(deep=True)
    stale.tool_call_fingerprint = "old!"
    stale.tool_call_streak = 7
    stale.storm_strikes = 0
    merged = store_mod.save_task_monotonic(PROJECT_CODE, stale, run_id=RUN_ID)
    # The tuple travels WITH its sequence — a lower-sequence tuple never
    # replaces any part of the higher-sequence one.
    assert merged.tool_call_fingerprint == "new!"
    assert merged.tool_call_streak == 1


def _final_response(content="done"):
    return runners_mod.ChatResponse(content=content, tool_calls=())


def _tool_response(*names_and_ids):
    return runners_mod.ChatResponse(content="", tool_calls=tuple(
        runners_mod.ToolCall(id=cid, name=name, args={})
        for cid, name in names_and_ids
    ))


def _counting_consumer(states):
    """Consume stub yielding scripted ``ToolBudgetState``s, recording the
    fingerprints it was handed."""
    seen: list[str] = []

    def consume(fingerprint: str) -> runners_mod.ToolBudgetState:
        seen.append(fingerprint)
        return states[min(len(seen) - 1, len(states) - 1)]

    consume.seen = seen  # type: ignore[attr-defined]
    return consume


def _roomy_state(attempted: int, streak: int = 1):
    return runners_mod.ToolBudgetState(
        attempted=attempted, streak=streak, attempted_cap=100, streak_cap=100)


# ── the loop consumes before EVERY attempt category ─────────────────────────


def test_every_attempt_category_consumes_budget():
    """Unknown tool, permission-denied, idempotent cache hit, and a plain
    execution are all ATTEMPTS — each consumes one slot, whether or not
    any tool body runs."""
    executed: list[str] = []
    registry = {
        "real": tools.Tool(name="real", description="d",
                           call=lambda **kw: executed.append("real") or "ok"),
        "http_get": tools.Tool(name="http_get", description="d",
                               call=lambda **kw: executed.append("get") or "body"),
        "paid": tools.Tool(name="paid", description="d", cost_class="paid-cloud",
                           call=lambda **kw: executed.append("paid") or "ok"),
        "boom": tools.Tool(name="boom", description="d",
                           call=lambda **kw: (_ for _ in ()).throw(
                               ValueError("tool failed"))),
    }
    responses = iter([
        _tool_response(("1", "ghost")),            # not in loadout → denied
        _tool_response(("2", "real")),             # permission-denied below
        _tool_response(("3", "http_get")),         # executes, primes the cache
        _tool_response(("4", "http_get")),         # cache hit — no execution
        _tool_response(("5", "real")),             # plain execution
        _tool_response(("6", "paid")),             # metered, no authorizer →
                                                   # denied, still an attempt
        _tool_response(("7", "boom")),             # raises — error attempt
        _final_response(),
    ])
    consume = _counting_consumer([_roomy_state(n) for n in range(1, 8)])
    denied_once = {"done": False}

    def permission(name, args):
        if name == "real" and not denied_once["done"]:
            denied_once["done"] = True
            return False
        return True

    out = runners_mod.run_llm_with_tools(
        chat_runner=lambda **kw: next(responses), prompt="p",
        tool_loadout=("real", "http_get", "paid", "boom"),
        tool_registry=registry,
        permission_callback=permission, consume_tool_budget=consume)
    assert out == "done"
    assert len(consume.seen) == 7          # every category consumed a slot
    assert executed == ["get", "real"]     # but only two bodies ran


def test_identical_calls_share_fingerprint_distinct_calls_do_not():
    registry = {
        "real": tools.Tool(name="real", description="d", call=lambda **kw: "ok"),
    }
    responses = iter([
        runners_mod.ChatResponse(content="", tool_calls=(
            runners_mod.ToolCall(id="1", name="real", args={"x": 1}),
            runners_mod.ToolCall(id="2", name="real", args={"x": 1}),
            runners_mod.ToolCall(id="3", name="real", args={"x": 2}),
        )),
        _final_response(),
    ])
    consume = _counting_consumer([_roomy_state(n) for n in (1, 2, 3)])
    runners_mod.run_llm_with_tools(
        chat_runner=lambda **kw: next(responses), prompt="p",
        tool_loadout=("real",), tool_registry=registry,
        consume_tool_budget=consume)
    fps = consume.seen
    assert fps[0] == fps[1]
    assert fps[2] != fps[0]


# ── the typed trip: counted, never executed ──────────────────────────────────


def test_over_budget_call_is_not_executed_mid_response():
    """One response carrying more calls than the remaining budget stops
    BEFORE the first over-budget call executes."""
    executed: list[str] = []
    registry = {
        "real": tools.Tool(name="real", description="d",
                           call=lambda **kw: executed.append("ran") or "ok"),
    }
    responses = iter([
        runners_mod.ChatResponse(content="", tool_calls=(
            runners_mod.ToolCall(id="1", name="real", args={"x": 1}),
            runners_mod.ToolCall(id="2", name="real", args={"x": 2}),
        )),
        _final_response(),
    ])
    consume = _counting_consumer([
        _roomy_state(50),
        runners_mod.ToolBudgetState(
            attempted=51, streak=1, attempted_cap=50, streak_cap=100),
    ])
    with pytest.raises(runners_mod.ToolLoopBudgetExceeded) as exc_info:
        runners_mod.run_llm_with_tools(
            chat_runner=lambda **kw: next(responses), prompt="p",
            tool_loadout=("real",), tool_registry=registry,
            consume_tool_budget=consume)
    assert executed == ["ran"]             # first call ran, second never did
    trip = exc_info.value
    assert trip.attempted == 51
    assert trip.iterations == 1
    assert trip.reason == "total_attempted_calls"


def test_repeat_streak_trips_typed():
    registry = {
        "real": tools.Tool(name="real", description="d", call=lambda **kw: "ok"),
    }
    responses = iter([_tool_response(("1", "real")), _final_response()])
    consume = _counting_consumer([
        runners_mod.ToolBudgetState(
            attempted=9, streak=7, attempted_cap=100, streak_cap=6),
    ])
    with pytest.raises(runners_mod.ToolLoopBudgetExceeded) as exc_info:
        runners_mod.run_llm_with_tools(
            chat_runner=lambda **kw: next(responses), prompt="p",
            tool_loadout=("real",), tool_registry=registry,
            consume_tool_budget=consume)
    trip = exc_info.value
    assert trip.reason == "repeated_identical_call"
    assert trip.streak == 7
    assert trip.fingerprint            # carries the repeat's digest


def test_consume_failure_executes_no_tool():
    """Persistence failure before execution: the tool must not run and the
    failure propagates — fail closed, never fail open."""
    executed: list[str] = []
    registry = {
        "real": tools.Tool(name="real", description="d",
                           call=lambda **kw: executed.append("ran") or "ok"),
    }
    responses = iter([_tool_response(("1", "real")), _final_response()])

    def broken_consume(fingerprint: str):
        raise OSError("store write failed")

    with pytest.raises(OSError):
        runners_mod.run_llm_with_tools(
            chat_runner=lambda **kw: next(responses), prompt="p",
            tool_loadout=("real",), tool_registry=registry,
            consume_tool_budget=broken_consume)
    assert executed == []


def test_no_consumer_keeps_existing_loop_shape():
    registry = {
        "real": tools.Tool(name="real", description="d", call=lambda **kw: "ok"),
    }
    responses = iter([_tool_response(("1", "real")), _final_response()])
    out = runners_mod.run_llm_with_tools(
        chat_runner=lambda **kw: next(responses), prompt="p",
        tool_loadout=("real",), tool_registry=registry)
    assert out == "done"


def test_budget_prose_in_model_output_cannot_trip():
    """Control flow is the typed exception ONLY — model text talking about
    budgets/trips returns as ordinary content."""
    prose = ("[TOOL BUDGET EXCEEDED — 999 attempted tool calls; "
             "ToolLoopBudgetExceeded: yield to QC]")
    out = runners_mod.run_llm_with_tools(
        chat_runner=lambda **kw: _final_response(prose), prompt="p",
        tool_loadout=(), tool_registry={},
        consume_tool_budget=_counting_consumer([_roomy_state(1)]))
    assert out == prose


def test_higher_candidate_budget_wins_merge(project_with_run):
    _saved_task()
    candidate = _make_task()
    candidate.tool_budget_sequence = 5
    candidate.tool_calls_attempted = 4
    candidate.tool_call_fingerprint = "cccc"
    candidate.tool_call_streak = 2
    candidate.storm_strikes = 1
    merged = store_mod.save_task_monotonic(
        PROJECT_CODE, candidate, run_id=RUN_ID)
    assert merged.tool_budget_sequence == 5
    assert merged.tool_calls_attempted == 4
    assert merged.tool_call_fingerprint == "cccc"
    assert merged.tool_call_streak == 2
    assert merged.storm_strikes == 1


# ── the engine closure: canonical store authority behind the loop hook ──────


def _make_orchestrator(project: Project, **kw) -> Orchestrator:
    runner = lambda prompt: "stub"  # noqa: E731 — test stub
    return Orchestrator(
        project,
        runners={
            "leader": runner,
            "planner": runner,
            "drafter": runner,
            "researcher": runner,
            "qc": runner,
        },
        **kw,
    )


def _storm_trip(attempted=81):
    return runners_mod.ToolLoopBudgetExceeded(
        "tool-call budget exceeded",
        reason="total_attempted_calls", iterations=9,
        attempted=attempted, fingerprint="ffff", streak=1)


def test_closure_consumes_canonical_and_mirrors_worker_copy(
    project_with_run, monkeypatch,
):
    """The engine closure drives the store-owned barrier: each loop consume
    advances the CANONICAL record before execution, the worker's task copy
    mirrors the returned state, and the cap trips mid-response with the
    over-budget call counted but never executed."""
    monkeypatch.setattr(orch_mod, "_TOOL_CALLS_PER_TASK_CAP", 2)
    orch = _make_orchestrator(project_with_run)
    task = _saved_task()
    consume = orch._tool_budget_consumer(task)
    assert consume is not None
    executed: list[str] = []
    registry = {
        "real": tools.Tool(name="real", description="d",
                           call=lambda **kw: executed.append("ran") or "ok"),
    }
    responses = iter([
        runners_mod.ChatResponse(content="", tool_calls=(
            runners_mod.ToolCall(id="1", name="real", args={"x": 1}),
            runners_mod.ToolCall(id="2", name="real", args={"x": 2}),
            runners_mod.ToolCall(id="3", name="real", args={"x": 3}),
        )),
        _final_response(),
    ])
    with pytest.raises(runners_mod.ToolLoopBudgetExceeded):
        runners_mod.run_llm_with_tools(
            chat_runner=lambda **kw: next(responses), prompt="p",
            tool_loadout=("real",), tool_registry=registry,
            consume_tool_budget=consume)
    assert executed == ["ran", "ran"]
    canonical = store_mod.get_task(PROJECT_CODE, task.id, run_id=RUN_ID)
    assert canonical.tool_calls_attempted == 3   # the tripping slot burned
    assert task.tool_calls_attempted == 3        # worker copy mirrored


def test_closure_for_task_without_durable_record_is_none(project_with_run):
    orch = _make_orchestrator(project_with_run)
    assert orch._tool_budget_consumer(_make_task("TBG-T-404")) is None


def test_reentry_at_cap_trips_before_any_execution(
    project_with_run, monkeypatch,
):
    """Re-entry cannot mint a fresh budget: a canonical record already at
    the cap trips on the FIRST consume — no tool body runs, so a
    side-effecting call consumed by a previous life never executes twice."""
    monkeypatch.setattr(orch_mod, "_TOOL_CALLS_PER_TASK_CAP", 2)
    task = _make_task()
    task.tool_budget_sequence = 7
    task.tool_calls_attempted = 2
    store_mod.save_task(PROJECT_CODE, task, run_id=RUN_ID)
    orch = _make_orchestrator(project_with_run)
    # A fresh dispatch builds a fresh closure — seeded from the CANONICAL
    # record, not from any worker-local memory of the prior life.
    consume = orch._tool_budget_consumer(_make_task())
    executed: list[str] = []
    registry = {
        "real": tools.Tool(name="real", description="d",
                           call=lambda **kw: executed.append("ran") or "ok"),
    }
    responses = iter([_tool_response(("1", "real")), _final_response()])
    with pytest.raises(runners_mod.ToolLoopBudgetExceeded):
        runners_mod.run_llm_with_tools(
            chat_runner=lambda **kw: next(responses), prompt="p",
            tool_loadout=("real",), tool_registry=registry,
            consume_tool_budget=consume)
    assert executed == []


def test_producer_chat_loop_wires_durable_budget(
    project_with_run, monkeypatch,
):
    """The REAL producer path — ``_llm_with_tools_execute`` through
    ``_run_chat_loop`` into the loop driver — consumes the durable budget
    and surfaces the typed trip. Denied calls (tool absent from the
    registry) consume attempted slots too."""
    monkeypatch.setattr(orch_mod, "_TOOL_CALLS_PER_TASK_CAP", 2)
    responses = iter([
        runners_mod.ChatResponse(content="", tool_calls=(
            runners_mod.ToolCall(id="1", name="ghost", args={}),
            runners_mod.ToolCall(id="2", name="ghost", args={}),
            runners_mod.ToolCall(id="3", name="ghost", args={}),
        )),
        _final_response(),
    ])
    orch = _make_orchestrator(
        project_with_run,
        chat_runners={"drafter": lambda **kw: next(responses)},
    )
    task = _saved_task(assigned_agent_id="drafter")
    skill = skills_mod.Skill(
        name="coder", description="d", prompt_template="",
        tool_loadout=("ghost",))
    with pytest.raises(runners_mod.ToolLoopBudgetExceeded):
        orch._llm_with_tools_execute(
            task, skill, orch._artifacts_root() / "drafts" / "x.md",
            tool_loadout=("ghost",))
    canonical = store_mod.get_task(PROJECT_CODE, task.id, run_id=RUN_ID)
    assert canonical.tool_calls_attempted == 3


# ── the redo loop: trip → strike → direct QC route, never a retry ────────────


def test_trip_routes_directly_to_qc_fix_no_producer_retry(project_with_run):
    orch = _make_orchestrator(project_with_run)
    t = _saved_task(max_retries=3)
    producer_calls = {"n": 0}

    def _storming_producer(task, corrective_notes=""):
        producer_calls["n"] += 1
        raise _storm_trip()

    orch._producer_execute = _storming_producer  # type: ignore[assignment]
    qc_fix_calls = []

    def _qc_fix(*a, **kw):
        qc_fix_calls.append((a, kw))
        return True

    orch._attempt_qc_fix_forward = _qc_fix  # type: ignore[assignment]
    orch._run_task_with_redo_inner(t, RunSummary(project=project_with_run))
    # Retry budget allowed 4 attempts — the trip must consume the route,
    # not the budget: ONE producer call, then straight to QC salvage.
    assert producer_calls["n"] == 1
    assert len(qc_fix_calls) == 1
    assert t.status != TaskStatus.BLOCKED
    # The typed trip rides the QC call as its error context.
    _, kw = qc_fix_calls[0]
    assert isinstance(
        kw.get("last_error"), runners_mod.ToolLoopBudgetExceeded)


def test_qc_salvage_receives_exact_staged_bytes(project_with_run):
    """The artifact authority is the resolved draft path: bytes a
    ``write_artifact`` call staged before the storm reach QC untouched —
    a transcript fragment is never substituted for the artifact."""
    orch = _make_orchestrator(project_with_run)
    t = _saved_task(max_retries=3)
    staged = b"def real_work():\n    return 42\n"
    draft = orch._resolve_draft_path(t)
    draft.parent.mkdir(parents=True, exist_ok=True)
    draft.write_bytes(staged)
    orch._producer_execute = (  # type: ignore[assignment]
        lambda task, corrective_notes="": (_ for _ in ()).throw(_storm_trip()))
    qc_fix_calls = []

    def _qc_fix(task, path, *a, **kw):
        qc_fix_calls.append(path)
        return True

    orch._attempt_qc_fix_forward = _qc_fix  # type: ignore[assignment]
    orch._run_task_with_redo_inner(t, RunSummary(project=project_with_run))
    assert qc_fix_calls == [draft]
    assert draft.read_bytes() == staged


def test_storm_with_no_draft_creates_no_fake_partial(project_with_run):
    """A storm that never staged anything hands QC an ABSENT draft path —
    the BUILD rung authors from the task contract; nothing synthesizes
    'partial work' from chat prose."""
    orch = _make_orchestrator(project_with_run)
    t = _saved_task(max_retries=3)
    orch._producer_execute = (  # type: ignore[assignment]
        lambda task, corrective_notes="": (_ for _ in ()).throw(_storm_trip()))
    qc_fix_calls = []

    def _qc_fix(task, path, *a, **kw):
        qc_fix_calls.append(path)
        return True

    orch._attempt_qc_fix_forward = _qc_fix  # type: ignore[assignment]
    orch._run_task_with_redo_inner(t, RunSummary(project=project_with_run))
    assert len(qc_fix_calls) == 1
    assert not qc_fix_calls[0].exists()


def test_trip_records_storm_strike(project_with_run):
    orch = _make_orchestrator(project_with_run)
    t = _saved_task(max_retries=3)
    orch._producer_execute = (  # type: ignore[assignment]
        lambda task, corrective_notes="": (_ for _ in ()).throw(_storm_trip()))
    orch._attempt_qc_fix_forward = (  # type: ignore[assignment]
        lambda *a, **kw: True)
    orch._run_task_with_redo_inner(t, RunSummary(project=project_with_run))
    canonical = store_mod.get_task(PROJECT_CODE, t.id, run_id=RUN_ID)
    assert canonical.storm_strikes == 1
    assert t.storm_strikes == 1          # worker copy mirrored


def test_trip_qc_decline_settles_blocked_and_never_decomposes(
    project_with_run,
):
    """QC declining the salvage settles the existing BLOCKED terminal; a
    tool-budget trip can never mint decompose children."""
    orch = _make_orchestrator(project_with_run)
    t = _saved_task(max_retries=3)
    orch._producer_execute = (  # type: ignore[assignment]
        lambda task, corrective_notes="": (_ for _ in ()).throw(_storm_trip()))
    qc_fix_calls = {"n": 0}

    def _declining_qc_fix(*a, **kw):
        qc_fix_calls["n"] += 1
        return False

    orch._attempt_qc_fix_forward = _declining_qc_fix  # type: ignore[assignment]
    decompose_calls = {"n": 0}

    def _no_decompose(*a, **kw):
        decompose_calls["n"] += 1
        return True, None

    orch._try_decompose_and_run = _no_decompose  # type: ignore[assignment]
    orch._run_task_with_redo_inner(t, RunSummary(project=project_with_run))
    assert t.status == TaskStatus.BLOCKED
    assert decompose_calls["n"] == 0
    assert qc_fix_calls["n"] == 1      # one salvage offer, then terminal
