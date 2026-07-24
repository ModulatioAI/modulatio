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
    store_mod.create_task(PROJECT_CODE, task, run_id=RUN_ID)
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


def test_candidate_ahead_of_canonical_is_typed_conflict(project_with_run):
    """Only the store consume barrier advances the budget sequence — a
    worker candidate can never legitimately lead the canonical record, so
    a candidate-ahead merge is a typed conflict, never a mint."""
    _saved_task()
    candidate = _make_task()
    candidate.tool_budget_sequence = 5
    candidate.tool_calls_attempted = 4
    with pytest.raises(ToolBudgetConflict) as exc_info:
        store_mod.save_task_monotonic(PROJECT_CODE, candidate, run_id=RUN_ID)
    assert exc_info.value.canonical.tool_budget_sequence == 0
    canonical = store_mod.get_task(PROJECT_CODE, "TBG-T-001", run_id=RUN_ID)
    assert canonical.tool_calls_attempted == 0  # nothing minted


def test_equal_sequence_mismatched_tuple_is_typed_conflict(project_with_run):
    _saved_task()
    store_mod.consume_tool_call_budget(
        PROJECT_CODE, "TBG-T-001", expected_sequence=0,
        fingerprint="aaaa", run_id=RUN_ID)
    candidate = _make_task()
    candidate.tool_budget_sequence = 1
    candidate.tool_calls_attempted = 9      # same sequence, foreign tuple
    candidate.tool_call_fingerprint = "aaaa"
    candidate.tool_call_streak = 1
    with pytest.raises(ToolBudgetConflict):
        store_mod.save_task_monotonic(PROJECT_CODE, candidate, run_id=RUN_ID)


def test_plain_save_mirrors_canonical_budget_never_advances(project_with_run):
    """Every ordinary task save treats the canonical budget tuple as
    authoritative: a stale candidate mirrors it, a candidate-ahead one is
    a typed conflict — no direct save path can erase or mint a consume."""
    task = _saved_task()
    store_mod.consume_tool_call_budget(
        PROJECT_CODE, "TBG-T-001", expected_sequence=0,
        fingerprint="aaaa", run_id=RUN_ID)
    stale = task.model_copy(deep=True)
    assert stale.tool_budget_sequence == 0
    store_mod.save_task(PROJECT_CODE, stale, run_id=RUN_ID)
    canonical = store_mod.get_task(PROJECT_CODE, "TBG-T-001", run_id=RUN_ID)
    assert canonical.tool_budget_sequence == 1   # consume survived the save
    assert canonical.tool_calls_attempted == 1
    ahead = task.model_copy(deep=True)
    ahead.tool_budget_sequence = 9
    with pytest.raises(ToolBudgetConflict):
        store_mod.save_task(PROJECT_CODE, ahead, run_id=RUN_ID)


def test_interleaved_consume_cannot_be_erased_by_merge(project_with_run):
    """A consume that lands while a monotonic merge is in flight is never
    overwritten: the merge's read+project+write and the consume serialize
    on one store lock. The merge's canonical read is deterministically
    delayed; the consume must wait for it and apply on top."""
    import threading
    import time as _time

    task = _saved_task()
    store_mod.consume_tool_call_budget(
        PROJECT_CODE, "TBG-T-001", expected_sequence=0,
        fingerprint="aaaa", run_id=RUN_ID)
    stale = task.model_copy(deep=True)          # pre-consume snapshot

    real_read = store_mod._read_canonical_task_authority
    merge_thread_id = threading.get_ident()
    merge_read_started = threading.Event()

    def _slow_read(code, task_id, run_id):
        result = real_read(code, task_id, run_id)
        if threading.get_ident() == merge_thread_id:
            merge_read_started.set()
            _time.sleep(0.3)                    # the interleave window
        return result

    consume_done = {"sequence": None}

    def _late_consume():
        merge_read_started.wait(timeout=5)
        updated = store_mod.consume_tool_call_budget(
            PROJECT_CODE, "TBG-T-001", expected_sequence=1,
            fingerprint="bbbb", run_id=RUN_ID)
        consume_done["sequence"] = updated.tool_budget_sequence

    consumer = threading.Thread(target=_late_consume)
    consumer.start()
    store_mod._read_canonical_task_authority = _slow_read
    try:
        store_mod.save_task_monotonic(PROJECT_CODE, stale, run_id=RUN_ID)
    finally:
        store_mod._read_canonical_task_authority = real_read
        consumer.join(timeout=10)

    canonical = store_mod.get_task(PROJECT_CODE, "TBG-T-001", run_id=RUN_ID)
    assert consume_done["sequence"] == 2
    assert canonical.tool_budget_sequence == 2   # the slot survived
    assert canonical.tool_calls_attempted == 2


def test_racing_consumes_and_saves_count_every_slot_once(project_with_run):
    """Two consumers and a stale-saver race: every successful consume is
    represented exactly once in the final canonical total."""
    import threading

    task = _saved_task()
    stale = task.model_copy(deep=True)
    consumed = []
    lock = threading.Lock()

    def _consumer():
        for _ in range(25):
            while True:
                canonical = store_mod.get_task(
                    PROJECT_CODE, "TBG-T-001", run_id=RUN_ID)
                try:
                    updated = store_mod.consume_tool_call_budget(
                        PROJECT_CODE, "TBG-T-001",
                        expected_sequence=canonical.tool_budget_sequence,
                        fingerprint="racer", run_id=RUN_ID)
                except ToolBudgetConflict:
                    continue                    # stale view — resync, retry
                with lock:
                    consumed.append(updated.tool_budget_sequence)
                break

    def _saver():
        for _ in range(25):
            store_mod.save_task_monotonic(
                PROJECT_CODE, stale.model_copy(deep=True), run_id=RUN_ID)

    threads = [threading.Thread(target=_consumer) for _ in range(2)]
    threads.append(threading.Thread(target=_saver))
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    canonical = store_mod.get_task(PROJECT_CODE, "TBG-T-001", run_id=RUN_ID)
    assert len(consumed) == 50
    assert canonical.tool_calls_attempted == 50
    assert canonical.tool_budget_sequence == 50


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


def test_closure_for_task_without_durable_record_fails_closed(
    project_with_run,
):
    """No canonical record (unsaved, deleted, or quarantined-corrupt) is
    NOT permission to run uncapped — the hook build refuses typed before
    any model call. The moment persistence is least trustworthy is the
    moment the cap matters most."""
    orch = _make_orchestrator(project_with_run)
    with pytest.raises(ToolBudgetConflict) as exc_info:
        orch._tool_budget_consumer(_make_task("TBG-T-404"))
    assert exc_info.value.canonical is None


def test_unsaved_task_producer_makes_zero_model_calls(project_with_run):
    """A tool-using task producer without a durable budget record starts
    ZERO model calls and zero tools — the typed refusal fires before the
    chat loop."""
    from modulatio import skills as skills_mod

    model_calls = {"n": 0}

    def _chat(**kw):
        model_calls["n"] += 1
        return runners_mod.ChatResponse(content="done", tool_calls=())

    orch = _make_orchestrator(
        project_with_run, chat_runners={"drafter": _chat})
    unsaved = _make_task("TBG-T-405", assigned_agent_id="drafter")
    skill = skills_mod.Skill(
        name="coder", description="d", prompt_template="",
        tool_loadout=("ghost",))
    with pytest.raises(ToolBudgetConflict):
        orch._llm_with_tools_execute(
            unsaved, skill, orch._artifacts_root() / "drafts" / "x.md",
            tool_loadout=("ghost",))
    assert model_calls["n"] == 0


def test_budget_store_failure_routes_typed_no_retry_no_strike(
    project_with_run, monkeypatch,
):
    """A consume persistence/CAS failure is a budget-store outcome, not a
    storm and not a generic producer crash: ONE producer invocation (no
    model re-asks while the authority store is down), no strike, no QC
    salvage — the task settles through the typed terminal."""
    monkeypatch.setenv("MODULATIO_TASK_MAX_RETRIES", "3")
    orch = _make_orchestrator(project_with_run)
    t = _saved_task("TBG-T-406", max_retries=3)
    producer_calls = {"n": 0}

    def _failing_producer(task, corrective_notes=""):
        producer_calls["n"] += 1
        raise ToolBudgetConflict(
            "tool-budget store failure: disk unavailable", canonical=None)

    orch._producer_execute = _failing_producer  # type: ignore[assignment]
    qc_fix = {"n": 0}
    orch._attempt_qc_fix_forward = (  # type: ignore[assignment]
        lambda *a, **kw: qc_fix.__setitem__("n", qc_fix["n"] + 1) or True)
    events = []
    orch.activity_callback = lambda e: events.append(e)
    from modulatio.orchestration import RunSummary
    from modulatio.types import TaskStatus
    orch._run_task_with_redo_inner(t, RunSummary(project=project_with_run))
    assert producer_calls["n"] == 1        # no repeated model attempts
    assert qc_fix["n"] == 0                # not the storm/QC-salvage route
    assert t.status == TaskStatus.BLOCKED
    canonical = store_mod.get_task(PROJECT_CODE, t.id, run_id=RUN_ID)
    assert canonical.storm_strikes == 0    # a store failure is not a storm
    assert "tool_budget_store_failure" in [e.phase for e in events]


def test_consume_hook_wraps_store_errors_typed(project_with_run, monkeypatch):
    orch = _make_orchestrator(project_with_run)
    task = _saved_task("TBG-T-407")
    consume = orch._tool_budget_consumer(task)
    monkeypatch.setattr(
        store_mod, "consume_tool_call_budget",
        lambda *a, **kw: (_ for _ in ()).throw(OSError("disk gone")))
    with pytest.raises(ToolBudgetConflict):
        consume("aaaa")


# ── creation vs update: an ordinary save can never resurrect authority ──────


def _task_file(task_id: str = "TBG-T-001") -> Path:
    from modulatio import vault
    return Path(vault.VAULT_ROOT) / PROJECT_CODE.lower() / "runs" / RUN_ID \
        / "tasks" / f"{task_id}.md"


def test_ordinary_save_never_recreates_a_missing_record(project_with_run):
    """Delete the canonical record after a consume; an ordinary save of a
    stale pre-consume copy must NOT recreate the task at sequence zero —
    a missing authority record is a typed refusal, never fresh budget."""
    task = _saved_task()
    stale = task.model_copy(deep=True)
    store_mod.consume_tool_call_budget(
        PROJECT_CODE, task.id, expected_sequence=0,
        fingerprint="aaaa", run_id=RUN_ID)
    path = _task_file()
    assert path.exists()
    path.unlink()
    with pytest.raises(ToolBudgetConflict):
        store_mod.save_task(PROJECT_CODE, stale, run_id=RUN_ID)
    assert not path.exists()               # nothing resurrected
    with pytest.raises(ToolBudgetConflict):
        store_mod.save_task_monotonic(PROJECT_CODE, stale, run_id=RUN_ID)
    assert not path.exists()


def test_quarantined_record_is_not_replaced_by_stale_copy(project_with_run):
    task = _saved_task()
    stale = task.model_copy(deep=True)
    path = _task_file()
    quarantine = path.with_suffix(".broken.md")
    path.rename(quarantine)
    quarantined_bytes = quarantine.read_bytes()
    with pytest.raises(ToolBudgetConflict):
        store_mod.save_task(PROJECT_CODE, stale, run_id=RUN_ID)
    assert quarantine.read_bytes() == quarantined_bytes
    assert not path.exists()


def test_unreadable_record_raises_typed_and_stays_untouched(
    project_with_run,
):
    task = _saved_task()
    stale = task.model_copy(deep=True)
    path = _task_file()
    original = path.read_bytes()
    path.chmod(0o000)
    try:
        with pytest.raises(ToolBudgetConflict):
            store_mod.save_task(PROJECT_CODE, stale, run_id=RUN_ID)
    finally:
        path.chmod(0o600)
    assert path.read_bytes() == original


def test_corrupt_record_raises_typed_never_overwritten(project_with_run):
    task = _saved_task()
    stale = task.model_copy(deep=True)
    path = _task_file()
    path.write_text("---\nnot: [valid frontmatter\n", encoding="utf-8")
    corrupt = path.read_bytes()
    with pytest.raises(ToolBudgetConflict):
        store_mod.save_task(PROJECT_CODE, stale, run_id=RUN_ID)
    assert path.read_bytes() == corrupt


def test_unknown_quarantine_state_never_authorizes_creation(
    project_with_run, monkeypatch,
):
    """An OSError while DISCOVERING quarantine state is not absence: the
    strict reader must fail typed, create nothing, and leave preserved
    quarantine bytes alone — inability to prove genuine absence is never
    permission to mint fresh authority."""
    task = _saved_task("TBG-T-510")
    path = _task_file("TBG-T-510")
    quarantine = path.with_suffix(".broken.md")
    path.rename(quarantine)                     # canonical gone, marker kept
    preserved = quarantine.read_bytes()

    real_glob = Path.glob

    def _blind_glob(self, pattern):
        if "TBG-T-510" in pattern:
            raise PermissionError("EACCES")
        return real_glob(self, pattern)

    monkeypatch.setattr(Path, "glob", _blind_glob)
    fresh = task.model_copy(deep=True)
    fresh.tool_budget_sequence = 0
    with pytest.raises(ToolBudgetConflict):
        store_mod.create_task(PROJECT_CODE, fresh, run_id=RUN_ID)
    monkeypatch.setattr(Path, "glob", real_glob)
    assert not path.exists()                    # nothing minted
    assert quarantine.read_bytes() == preserved


def test_unknown_quarantine_state_fails_closed_without_sibling(
    project_with_run, monkeypatch,
):
    """Same rule with NO quarantine sibling on disk and a generic OSError:
    unknown is unknown — still a typed refusal, still nothing created."""
    real_glob = Path.glob

    def _blind_glob(self, pattern):
        if "TBG-T-511" in pattern:
            raise OSError("io error")
        return real_glob(self, pattern)

    monkeypatch.setattr(Path, "glob", _blind_glob)
    with pytest.raises(ToolBudgetConflict):
        store_mod.create_task(
            PROJECT_CODE, _make_task("TBG-T-511"), run_id=RUN_ID)
    monkeypatch.setattr(Path, "glob", real_glob)
    assert not _task_file("TBG-T-511").exists()


def test_create_task_is_the_only_creation_seam(project_with_run):
    """Explicit creation succeeds on genuine absence with a zeroed budget
    tuple; creation never overwrites an existing record; a non-zero
    budget tuple is not creatable (budget state only ever comes from the
    consume barrier)."""
    fresh = _make_task("TBG-T-500")
    created = store_mod.create_task(PROJECT_CODE, fresh, run_id=RUN_ID)
    assert created.tool_budget_sequence == 0
    assert store_mod.get_task(
        PROJECT_CODE, "TBG-T-500", run_id=RUN_ID) is not None
    with pytest.raises(ToolBudgetConflict):
        store_mod.create_task(PROJECT_CODE, fresh, run_id=RUN_ID)
    seeded = _make_task("TBG-T-501")
    seeded.tool_budget_sequence = 3
    seeded.tool_calls_attempted = 3
    with pytest.raises(ToolBudgetConflict):
        store_mod.create_task(PROJECT_CODE, seeded, run_id=RUN_ID)


def test_settlement_after_record_loss_does_not_resurrect(project_with_run):
    """End to end: record deleted mid-run → the typed budget failure
    settles the task → neither the settlement save nor a merge recreates
    the record, and re-entry finds no sequence-zero authority."""
    orch = _make_orchestrator(project_with_run)
    t = _saved_task("TBG-T-502", max_retries=1)
    store_mod.consume_tool_call_budget(
        PROJECT_CODE, t.id, expected_sequence=0,
        fingerprint="aaaa", run_id=RUN_ID)
    t.tool_budget_sequence = 1             # worker mirrored the consume
    t.tool_calls_attempted = 1
    _task_file(t.id).unlink()

    orch._producer_execute = (  # type: ignore[assignment]
        lambda task, corrective_notes="": (_ for _ in ()).throw(
            ToolBudgetConflict("no durable record", canonical=None)))
    from modulatio.orchestration import RunSummary
    orch._run_task_with_redo_inner(t, RunSummary(project=project_with_run))
    assert not _task_file(t.id).exists()   # settled without resurrection


# ── Clay: no pre-execution enforcement seam → refuse/fallback, never an
# unbudgeted native loop ─────────────────────────────────────────────────────


def _clay_marked_runner(invocations):
    def run(**kw):
        invocations.append(kw)
        return runners_mod.ChatResponse(content="native", tool_calls=())
    run.runs_native_tool_loop = True
    return run


def test_clay_seat_refused_for_budgeted_tool_producer(project_with_run):
    """A budgeted tool-using producer on a Clay seat never launches the
    native loop: the seat is refused as unavailable (the fallback-chain
    signal) with zero Clay invocations."""
    from modulatio import claude_cli as clay
    from modulatio import skills as skills_mod

    clay_calls: list = []
    orch = _make_orchestrator(
        project_with_run,
        chat_runners={"drafter": _clay_marked_runner(clay_calls)})
    task = _saved_task("TBG-T-408", assigned_agent_id="drafter")
    skill = skills_mod.Skill(
        name="coder", description="d", prompt_template="",
        tool_loadout=("ghost",))
    with pytest.raises(clay.ClaudeUnavailable):
        orch._llm_with_tools_execute(
            task, skill, orch._artifacts_root() / "drafts" / "x.md",
            tool_loadout=("ghost",))
    assert clay_calls == []


def test_clay_primary_falls_back_continuing_canonical_budget(
    project_with_run, monkeypatch,
):
    """Clay-primary with a function-loop fallback: the refusal advances
    the chain, the fallback seat runs, and its consumes continue the SAME
    canonical sequence — no fresh budget on the fallback."""
    from modulatio import skills as skills_mod
    from modulatio.orchestration import Orchestrator

    clay_calls: list = []
    responses = iter([
        runners_mod.ChatResponse(content="", tool_calls=(
            runners_mod.ToolCall(id="1", name="ghost", args={}),
            runners_mod.ToolCall(id="2", name="ghost", args={}),
        )),
        runners_mod.ChatResponse(content="done", tool_calls=()),
    ])

    def _function_runner(**kw):
        return next(responses)

    orch = _make_orchestrator(
        project_with_run,
        chat_runners={"drafter": _clay_marked_runner(clay_calls)})
    monkeypatch.setattr(
        Orchestrator, "_seat_fallback_chain",
        lambda self, agent_id, primary_model, primary_runner: [
            ("clay-model", primary_runner),
            ("fallback-model", _function_runner),
        ])
    task = _saved_task("TBG-T-409", assigned_agent_id="drafter")
    store_mod.consume_tool_call_budget(
        PROJECT_CODE, task.id, expected_sequence=0,
        fingerprint="prior", run_id=RUN_ID)   # one slot spent pre-fallback
    skill = skills_mod.Skill(
        name="coder", description="d", prompt_template="",
        tool_loadout=("ghost",))
    draft = orch._artifacts_root() / "drafts" / "x.md"
    draft.parent.mkdir(parents=True, exist_ok=True)
    orch._llm_with_tools_execute(
        task, skill, draft, tool_loadout=("ghost",))
    assert clay_calls == []
    canonical = store_mod.get_task(PROJECT_CODE, task.id, run_id=RUN_ID)
    assert canonical.tool_calls_attempted == 3   # 1 prior + 2 on fallback


def test_native_loop_refusal_does_not_cool_the_seat(project_with_run):
    """The architectural refusal is availability-SHAPED (the chain must
    advance) but not an availability EVENT: a healthy seat refused for one
    task shape must not sit out the pool like a dead endpoint."""
    from modulatio import claude_cli as clay
    from modulatio import skills as skills_mod
    from modulatio.orchestration import RunSummary

    clay_calls: list = []
    orch = _make_orchestrator(
        project_with_run,
        chat_runners={"drafter": _clay_marked_runner(clay_calls)})
    t = _saved_task("TBG-T-411", assigned_agent_id="drafter", max_retries=0)
    skill = skills_mod.Skill(
        name="coder", description="d", prompt_template="",
        tool_loadout=("ghost",))

    def _producer(task, corrective_notes=""):
        return orch._llm_with_tools_execute(
            task, skill, orch._artifacts_root() / "drafts" / "x.md",
            tool_loadout=("ghost",))

    orch._producer_execute = _producer  # type: ignore[assignment]
    orch._attempt_qc_fix_forward = (  # type: ignore[assignment]
        lambda *a, **kw: True)
    orch._run_task_with_redo_inner(t, RunSummary(project=project_with_run))
    assert clay_calls == []
    assert isinstance(  # the refusal is a typed subclass, still chain-advancing
        clay.NativeToolLoopRefused("x"), clay.ClaudeUnavailable)
    assert not orch._seat_in_cooldown("drafter")


def test_clay_runner_without_budget_hook_is_not_refused(project_with_run):
    """Unbudgeted lanes (QC, leader converse) keep their Clay seats — the
    refusal binds exactly where the budget authority does."""
    clay_calls: list = []
    orch = _make_orchestrator(
        project_with_run,
        chat_runners={"qc": _clay_marked_runner(clay_calls)})
    reply = orch._run_chat_loop(
        prompt="p", tool_loadout=(), role="qc", agent_id="qc",
        task_id="TBG-T-410",
        transcript_path=orch._artifacts_root() / "tool_calls" / "t.jsonl",
        skill_name="review")
    assert reply == "native"
    assert len(clay_calls) == 1


def test_reentry_at_cap_trips_before_any_execution(
    project_with_run, monkeypatch,
):
    """Re-entry cannot mint a fresh budget: a canonical record already at
    the cap trips on the FIRST consume — no tool body runs, so a
    side-effecting call consumed by a previous life never executes twice."""
    monkeypatch.setattr(orch_mod, "_TOOL_CALLS_PER_TASK_CAP", 2)
    task = _saved_task()
    sequence = 0
    for fp in ("prior-1", "prior-2"):      # the prior life spent the cap
        updated = store_mod.consume_tool_call_budget(
            PROJECT_CODE, task.id, expected_sequence=sequence,
            fingerprint=fp, run_id=RUN_ID)
        sequence = updated.tool_budget_sequence
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
