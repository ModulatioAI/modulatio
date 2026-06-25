"""Alpha (W1) regression tests — RecoverableContextError handling.

Layer 2 (context_budget) raises RecoverableContextError when a prompt
exceeds the model's window even after compression. Wave 1 + Wave 2
audits didn't catch the bug that the orchestrator never explicitly
caught this exception — it fell through to the generic ``except
Exception`` in the redo loop, which retried (futilely, against the
same prompt) until the retry budget exhausted, then settled BLOCKED
with no ticket. Decomposition (the only real fix) never got triggered.

W1 fix path A (locked, long-term): orchestrator catches
RecoverableContextError at the per-task boundary, opens a CRITICAL
ticket carrying the checkpoint path, marks the task BLOCKED, and
breaks out of the redo loop. Leader-reflect's between-sub-objective
turn sees the ticket and routes to ``revise-major`` (decompose) or
``pause`` (escalate to user). The checkpoint is an audit artifact +
Leader decomposition input — NOT a re-input source.

These tests pin that contract.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from modulatio import context_budget, store, vault
from modulatio.orchestration import Orchestrator, RunSummary
from modulatio.types import Project, Task, TaskStatus, TicketPriority


PROJECT_CODE = "CTX"


@pytest.fixture
def project(tmp_path: Path, monkeypatch) -> Project:
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    vault.init_project(PROJECT_CODE, "ctx-budget test", "exhaustion path")
    return Project(
        code=PROJECT_CODE,
        name="ctx-budget test",
        objective="exhaustion path",
        leader_model="stub",
        wiki_path=str(tmp_path / PROJECT_CODE.lower()),
    )


@pytest.fixture
def project_with_run(tmp_path: Path, monkeypatch) -> Project:
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    vault.init_project(PROJECT_CODE, "ctx-budget test", "exhaustion path")
    run_id = "run-ctx-001"
    vault.init_run(PROJECT_CODE, run_id, "exhaustion path")
    return Project(
        code=PROJECT_CODE,
        name="ctx-budget test",
        objective="exhaustion path",
        leader_model="stub",
        wiki_path=str(tmp_path / PROJECT_CODE.lower()),
        run_id=run_id,
    )


def _make_task() -> Task:
    return Task(
        id="CTX-T-001",
        project_id=uuid4(),
        goal_id="CTX-G-001",
        description="anything",
        max_retries=3,
    )


def _make_orchestrator(project: Project) -> Orchestrator:
    """Minimal orchestrator with stub runners. No real LLM calls; the
    test paths monkeypatch _producer_execute to raise."""
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
    )


# ── core W1 contract ─────────────────────────────────────────────────────


def test_recoverable_context_error_breaks_out_of_redo_loop(project_with_run, monkeypatch, tmp_path):
    """A RecoverableContextError on the first attempt must NOT burn
    the rest of the retry budget — the same prompt would hit the same
    wall on every retry. The redo loop returns immediately."""
    orch = _make_orchestrator(project_with_run)
    task = _make_task()
    summary = RunSummary(project=project_with_run)

    call_count = {"n": 0}

    def fake_producer(self, t, corrective_notes=""):
        call_count["n"] += 1
        raise context_budget.RecoverableContextError(
            model="stub-model",
            estimated_tokens=200_000,
            max_input_tokens=128_000,
            checkpoint_path=tmp_path / "checkpoint.json",
        )

    monkeypatch.setattr(Orchestrator, "_producer_execute", fake_producer)

    orch._run_task_with_redo(task, summary)

    assert call_count["n"] == 1, (
        "RecoverableContextError must NOT trigger retries — it's not "
        "a transient state. Got "
        f"{call_count['n']} producer invocations; expected 1."
    )
    assert task.status == TaskStatus.BLOCKED


def test_recoverable_context_error_opens_critical_ticket(project_with_run, monkeypatch, tmp_path):
    """A CRITICAL ticket must land in the ticket store carrying the
    checkpoint path so Leader-reflect can route to revise-major."""
    orch = _make_orchestrator(project_with_run)
    task = _make_task()
    summary = RunSummary(project=project_with_run)
    checkpoint = tmp_path / "checkpoint-1.json"

    def fake_producer(self, t, corrective_notes=""):
        raise context_budget.RecoverableContextError(
            model="grok-4-2",
            estimated_tokens=205_000,
            max_input_tokens=200_000,
            checkpoint_path=checkpoint,
        )

    monkeypatch.setattr(Orchestrator, "_producer_execute", fake_producer)
    orch._run_task_with_redo(task, summary)

    tickets = store.list_tickets(
        project_with_run.code, run_id=project_with_run.run_id
    )
    matching = [t for t in tickets if t.affected_task_id == task.id]
    assert len(matching) == 1, (
        f"Expected exactly one ticket for {task.id}; got {len(matching)}"
    )
    ticket = matching[0]
    assert ticket.priority == TicketPriority.CRITICAL
    assert "context-budget exhausted" in ticket.title
    assert "grok-4-2" in ticket.title
    assert "205000" in ticket.title or "205_000" in ticket.title or "205,000" in ticket.title
    # Checkpoint path surfaces in the body so Leader-reflect can see it
    assert str(checkpoint) in ticket.body
    # Decomposition framing is the load-bearing message for Leader-reflect
    assert "decompose" in ticket.body.lower()


def test_recoverable_context_error_records_summary_error(project_with_run, monkeypatch, tmp_path):
    """The run summary's error list must record the budget-exhaustion
    failure mode distinctly (not just generic exception text) so the
    daemon log + Leader-reflect input both see the right framing."""
    orch = _make_orchestrator(project_with_run)
    task = _make_task()
    summary = RunSummary(project=project_with_run)

    def fake_producer(self, t, corrective_notes=""):
        raise context_budget.RecoverableContextError(
            model="claude-haiku-4-5",
            estimated_tokens=300_000,
            max_input_tokens=200_000,
            checkpoint_path=tmp_path / "cp.json",
        )

    monkeypatch.setattr(Orchestrator, "_producer_execute", fake_producer)
    orch._run_task_with_redo(task, summary)

    assert any(
        "context-budget exhausted" in line and task.id in line
        for line in summary.errors
    ), f"summary.errors did not capture the exhaustion event: {summary.errors!r}"


def test_recoverable_context_error_writes_state_transition(project_with_run, monkeypatch, tmp_path):
    """The task's transition log must show the BLOCKED transition
    with a rationale that names context-budget exhaustion + the
    decompose-required framing — Leader-reflect reads this to decide
    revise-major vs pause."""
    orch = _make_orchestrator(project_with_run)
    task = _make_task()
    summary = RunSummary(project=project_with_run)

    def fake_producer(self, t, corrective_notes=""):
        raise context_budget.RecoverableContextError(
            model="m",
            estimated_tokens=100,
            max_input_tokens=50,
            checkpoint_path=tmp_path / "cp.json",
        )

    monkeypatch.setattr(Orchestrator, "_producer_execute", fake_producer)
    orch._run_task_with_redo(task, summary)

    # Last transition is the BLOCKED-with-decompose-rationale one.
    final = task.transitions[-1]
    assert final.to_state == TaskStatus.BLOCKED.value
    assert "context-budget exhausted" in final.rationale
    assert "decompose" in final.rationale.lower()
    # F6 audit follow-up: checkpoint path lives in the rationale so the
    # audit trail survives ticket deletion.
    assert "cp.json" in final.rationale or "checkpoint at" in final.rationale
    # F10 audit follow-up: engine-triggered block uses actor="orchestrator"
    # because the engine itself fires this transition (no agent decision
    # involved). Pinned so audit-log consumers can filter cleanly.
    assert final.actor == "orchestrator"


def test_recoverable_context_error_handles_missing_checkpoint(project_with_run, monkeypatch):
    """If Layer 2 couldn't write the checkpoint (filesystem full,
    permission denied), checkpoint_path is None. The orchestrator
    must still open the ticket cleanly — the checkpoint path absence
    isn't a crash vector."""
    orch = _make_orchestrator(project_with_run)
    task = _make_task()
    summary = RunSummary(project=project_with_run)

    def fake_producer(self, t, corrective_notes=""):
        raise context_budget.RecoverableContextError(
            model="m",
            estimated_tokens=100,
            max_input_tokens=50,
            checkpoint_path=None,  # the no-checkpoint path
        )

    monkeypatch.setattr(Orchestrator, "_producer_execute", fake_producer)
    orch._run_task_with_redo(task, summary)

    assert task.status == TaskStatus.BLOCKED
    tickets = store.list_tickets(
        project_with_run.code, run_id=project_with_run.run_id
    )
    matching = [t for t in tickets if t.affected_task_id == task.id]
    assert len(matching) == 1


# ── F1 (audit follow-up) — production wire-up of Layer 1 + Layer 2 ─────


def test_kickoff_binds_context_budget_config_for_run(project_with_run):
    """F1: Orchestrator.kickoff must bind a ContextBudgetConfig so the
    runner-side gate (and the W1 catch built on top of it) actually
    fires in real runs. Pre-fix this binding never happened — the
    gate was silent in production. Test by reading current_config()
    from inside a stubbed leader_decompose."""
    captured: dict[str, object | None] = {"ctx": "unset", "ts": "unset"}

    orch = _make_orchestrator(project_with_run)

    def _peek_at_config(self, *args, **kwargs):
        # Called from inside Orchestrator.kickoff; the binding should
        # be active here.
        captured["ctx"] = context_budget.current_config()
        from modulatio import tool_summarization as _ts
        captured["ts"] = _ts.current_config()
        return []  # no goals -> kickoff returns trivially

    from unittest.mock import patch
    with patch.object(Orchestrator, "_leader_decompose", _peek_at_config):
        orch.kickoff("anything")

    ctx_cfg = captured["ctx"]
    ts_cfg = captured["ts"]
    assert ctx_cfg is not None, (
        "Orchestrator.kickoff must bind ContextBudgetConfig; got None "
        "(F1 regression — Layer 2 silent in production again)"
    )
    assert ts_cfg is not None, (
        "Orchestrator.kickoff must bind ToolSummarizationConfig (F1)"
    )
    # Checkpoints + tool_calls dirs land under the run workspace.
    assert ctx_cfg.checkpoints_dir is not None
    assert "checkpoints" in str(ctx_cfg.checkpoints_dir)
    assert ts_cfg.tool_calls_dir is not None
    assert "tool_calls" in str(ts_cfg.tool_calls_dir)


def test_kickoff_unbinds_after_run(project_with_run):
    """F1: bindings must restore on kickoff exit — context-pollution
    across runs would let a stale checkpoints_dir from a previous
    project follow the next one."""
    orch = _make_orchestrator(project_with_run)

    # Sanity: nothing bound before kickoff
    assert context_budget.current_config() is None

    from unittest.mock import patch
    with patch.object(Orchestrator, "_leader_decompose", lambda self, *a, **k: []):
        orch.kickoff("anything")

    # Nothing bound after kickoff returns
    assert context_budget.current_config() is None, (
        "F1 regression: ContextBudgetConfig leaked past kickoff"
    )


def test_kickoff_delivers_and_completes_before_codification(project_with_run):
    """B1: delivery + the kickoff_ended completion signal must fire BEFORE the
    best-effort post-run codification, so a slow/hung codification (e.g. a Clay
    leader call) can't block or delay the user's deliverable + end-of-run report.
    Reproduces the live no-end-report stall: codification ran first and hung."""
    from unittest.mock import patch

    orch = _make_orchestrator(project_with_run)
    orch._deliver_products = True
    order: list[str] = []

    def _rec(label):
        def _f(self, *a, **k):
            order.append(label)
        return _f

    def _emit_rec(self, *a, **k):
        if k.get("phase") == "kickoff_ended":
            order.append("kickoff_ended")

    with patch.object(Orchestrator, "_leader_decompose", lambda self, *a, **k: []), \
         patch.object(Orchestrator, "_deliver_finished_products", _rec("deliver")), \
         patch.object(Orchestrator, "_post_run_codification", _rec("codify")), \
         patch.object(Orchestrator, "_post_run_jt_codification", _rec("jt_codify")), \
         patch.object(Orchestrator, "_emit_activity", _emit_rec):
        orch.kickoff("anything")

    assert {"deliver", "kickoff_ended", "codify", "jt_codify"} <= set(order), order
    assert order.index("deliver") < order.index("codify")
    assert order.index("kickoff_ended") < order.index("codify")
    assert order.index("kickoff_ended") < order.index("jt_codify")


def test_run_chat_loop_threads_model_to_run_llm_with_tools(project_with_run, monkeypatch):
    """F11 audit follow-up: Orchestrator._run_chat_loop must pass
    ``model=`` to ``runners.run_llm_with_tools`` so the Layer 1 +
    Layer 2 gate conditions (which require a non-None model) actually
    fire. Pre-fix the binding existed but the gate was still a no-op
    because model was never threaded — Round 1 caught the binding
    gap, Round 2 caught this delivery gap."""
    captured: dict = {}

    def fake_run_llm_with_tools(*, chat_runner, prompt, tool_loadout,
                                tool_registry, max_iters=16,
                                on_tool_call=None, model=None,
                                summarizer_chat_runner_factory=None,
                                **_):
        captured["model"] = model
        captured["summarizer_factory"] = summarizer_chat_runner_factory
        return "ok"

    from modulatio import runners as _runners_mod
    monkeypatch.setattr(_runners_mod, "run_llm_with_tools", fake_run_llm_with_tools)

    orch = _make_orchestrator(project_with_run)
    # Wire the model + factory the way _make_default_kickoff does.
    orch.chat_runner = lambda *a, **k: None
    orch.chat_runner_models = {"writer": "gpt-4o-mini"}
    orch.chat_runner_default_model = "fallback-model"
    sentinel_factory = lambda m: (lambda text: "sum")  # noqa: E731
    orch.summarizer_chat_runner_factory = sentinel_factory

    out = orch._run_chat_loop(
        prompt="hi",
        tool_loadout=("read_tool_result",),
        role="writer",
        agent_id="writer",
        task_id="T1",
        transcript_path=Path("/tmp/_f11_transcript.jsonl"),
        skill_name="test",
    )
    assert out == "ok"
    assert captured["model"] == "gpt-4o-mini", (
        "F11 regression: per-agent model not threaded to "
        "run_llm_with_tools (gate would be a no-op)"
    )
    assert captured["summarizer_factory"] is sentinel_factory


def test_run_chat_loop_threads_tool_sink_into_seat_context(project_with_run, monkeypatch):
    """R2 wiring: ``_run_chat_loop`` enters the Clay seat context with the SAME
    ``on_tool_call`` audit sink it hands ``run_llm_with_tools`` — so a Clay seat's
    in-sandbox tool calls (which read ``seat_activity_var``) land in the same
    transcript + activity feed instead of a None black hole. Asserting equality
    of the two references is what makes this a wiring test, not a part test."""
    from modulatio import claude_cli
    captured: dict = {}

    def fake_run_llm_with_tools(*, chat_runner, prompt, tool_loadout,
                                tool_registry, max_iters=16,
                                on_tool_call=None, model=None, **_):
        # We are INSIDE the `with self._seat_context(on_tool_call=...)` block,
        # so the contextvar must already hold the very sink passed to us here.
        captured["sink_in_context"] = claude_cli.seat_activity_var.get()
        captured["sink_arg"] = on_tool_call
        return "ok"

    from modulatio import runners as _runners_mod
    monkeypatch.setattr(_runners_mod, "run_llm_with_tools", fake_run_llm_with_tools)

    orch = _make_orchestrator(project_with_run)
    orch.chat_runner = lambda *a, **k: None
    orch.chat_runner_models = {"writer": "gpt-4o-mini"}

    orch._run_chat_loop(
        prompt="hi",
        tool_loadout=("read_tool_result",),
        role="writer",
        agent_id="writer",
        task_id="T1",
        transcript_path=Path("/tmp/_r2_transcript.jsonl"),
        skill_name="test",
    )
    assert callable(captured["sink_arg"]), "an audit sink was passed to the runner"
    assert captured["sink_in_context"] is captured["sink_arg"], (
        "R2 regression: the seat context did not receive the tool-call sink, so a "
        "Clay seat's tool events would vanish"
    )
    # And it is cleared once the seat context exits.
    assert claude_cli.seat_activity_var.get() is None


def test_run_chat_loop_confines_producer_seat_not_leader(project_with_run, monkeypatch):
    """#1 WIRING: a kickoff producer/QC chat-loop seat runs CONFINED (the seat
    context carries confined=True, which a Clay chat runner reads to restrict its
    tools); the converse/verify Leader stays UNCONFINED. Keyed on role != 'leader'.
    Asserting the contextvar's value INSIDE the runner call is what makes this a
    wiring test — the unit-on-the-part (argv builder) missed this lane gap."""
    from modulatio import claude_cli
    from modulatio import runners as _runners_mod
    seen: list = []

    def fake_run_llm_with_tools(*, chat_runner, **_):
        seen.append(claude_cli.current_confined_mode())
        return "ok"

    monkeypatch.setattr(_runners_mod, "run_llm_with_tools", fake_run_llm_with_tools)
    orch = _make_orchestrator(project_with_run)
    orch.chat_runner = lambda *a, **k: None
    orch.chat_runner_models = {"writer": "m", "leader": "m"}

    orch._run_chat_loop(prompt="x", tool_loadout=("read_tool_result",), role="writer",
                        agent_id="writer", task_id="T1",
                        transcript_path=Path("/tmp/_c1.jsonl"), skill_name="t")
    orch._run_chat_loop(prompt="x", tool_loadout=("read_tool_result",), role="leader",
                        agent_id="leader", task_id="T2",
                        transcript_path=Path("/tmp/_c2.jsonl"), skill_name="t")

    assert seen == [True, False], "producer seat must confine, leader must not"
    assert claude_cli.current_confined_mode() is False  # reset after the wrap


def test_run_chat_loop_falls_back_to_default_model(project_with_run, monkeypatch):
    """F11: when no per-agent model is registered, use the
    chat_runner_default_model so the gate still has something to
    pass through."""
    captured: dict = {}

    def fake_run_llm_with_tools(*, chat_runner, prompt, tool_loadout,
                                tool_registry, max_iters=16,
                                on_tool_call=None, model=None,
                                summarizer_chat_runner_factory=None,
                                **_):
        captured["model"] = model
        return "ok"

    from modulatio import runners as _runners_mod
    monkeypatch.setattr(_runners_mod, "run_llm_with_tools", fake_run_llm_with_tools)

    orch = _make_orchestrator(project_with_run)
    orch.chat_runner = lambda *a, **k: None
    orch.chat_runner_default_model = "fallback-model"
    # No per-agent entry for "stranger"
    orch._run_chat_loop(
        prompt="hi", tool_loadout=("read_tool_result",),
        role="writer", agent_id="stranger",
        task_id="T1", transcript_path=Path("/tmp/_f11b_transcript.jsonl"),
        skill_name="test",
    )
    assert captured["model"] == "fallback-model"


def test_run_chat_loop_overflow_blocks_task_with_recoverable_error(
    project_with_run, monkeypatch,
):
    """F11 + F12 end-to-end: a context-budget overflow inside the
    actual ``_run_chat_loop`` path (not just direct check_and_compress
    calls) must surface as a BLOCKED task with a CRITICAL ticket.
    Wild Bill's Round 2 ask: prove the gate fires through the
    real production path, not just that ContextVars are bound.

    Stub the chat runner to never tool-call but build a prompt big
    enough to trip the bound config."""
    from modulatio import runners as _runners_mod, store as _store

    # Tight cap to force overflow on a small prompt.
    bound_cfg = context_budget.ContextBudgetConfig(
        max_input_tokens=10,
        prune_at_pct=0.80,
        pad_pct=0.0,
        keep_recent=1,
        checkpoints_dir=None,  # path elided is fine for this assertion
    )

    chat_runner_calls = {"n": 0}
    def _stub_chat_runner(*, messages, tools):
        chat_runner_calls["n"] += 1
        return _runners_mod.ChatResponse(content="never reached", tool_calls=())

    orch = _make_orchestrator(project_with_run)
    orch.chat_runner = _stub_chat_runner
    orch.chat_runner_default_model = "gpt-4o-mini"

    # Bypass the kickoff-level binding so we can pin our own tight
    # config; assert the gate fires from inside _run_chat_loop.
    big_prompt = "x" * 10_000
    task = _make_task()
    summary = RunSummary(project=project_with_run)

    def fake_producer(self, t, corrective_notes=""):
        with context_budget.with_config(bound_cfg):
            self._run_chat_loop(
                prompt=big_prompt,
                tool_loadout=(),
                role="writer",
                agent_id=t.id,
                task_id=t.id,
                transcript_path=tmp_transcript,
                skill_name="test",
            )
        return Path("/tmp/_unused"), "sha256:0", 1

    import tempfile
    tmp_transcript = Path(tempfile.gettempdir()) / "_f11_e2e.jsonl"
    monkeypatch.setattr(Orchestrator, "_producer_execute", fake_producer)
    orch._run_task_with_redo(task, summary)

    # The chat runner never gets called because the gate refuses
    # before reaching it.
    assert chat_runner_calls["n"] == 0, (
        "F11/F12 regression: gate did not refuse before chat call"
    )
    # Task BLOCKED with the W1-style decompose-required framing.
    assert task.status == TaskStatus.BLOCKED
    tickets = _store.list_tickets(
        project_with_run.code, run_id=project_with_run.run_id
    )
    matching = [t for t in tickets if t.affected_task_id == task.id]
    assert len(matching) == 1
    assert "context-budget" in matching[0].title.lower()


def test_kickoff_skips_binding_when_no_run_id(project):
    """F1: when project has no run_id (test stubs, in-process plan
    with no workspace yet), binding is skipped — pre-no-op
    behavior preserved so existing tests keep working."""
    orch = _make_orchestrator(project)
    assert project.run_id is None  # fixture pin

    from unittest.mock import patch
    with patch.object(Orchestrator, "_leader_decompose", lambda self, *a, **k: []):
        orch.kickoff("anything")

    # Still unbound after — no surprise binding from a no-run kickoff.
    assert context_budget.current_config() is None


def test_generic_exception_still_uses_retry_budget(project_with_run, monkeypatch):
    """The W1 fix must NOT change behavior for generic exceptions —
    those still flow through the retry loop. Only RecoverableContextError
    short-circuits."""
    orch = _make_orchestrator(project_with_run)
    task = _make_task()
    summary = RunSummary(project=project_with_run)

    call_count = {"n": 0}

    def fake_producer(self, t, corrective_notes=""):
        call_count["n"] += 1
        raise RuntimeError("transient")

    monkeypatch.setattr(Orchestrator, "_producer_execute", fake_producer)
    orch._run_task_with_redo(task, summary)

    # Generic exceptions retry up to max_retries + 1 times (the +1 is
    # the initial attempt). max_retries=3 → 4 invocations.
    assert call_count["n"] == 4, (
        "generic exceptions must still go through the retry loop; "
        f"got {call_count['n']} invocations, expected 4"
    )


def test_compression_churn_counts_as_a_try_and_redoes(project_with_run, monkeypatch):
    """A CompressionChurnExceeded (a thrashing attempt) is NOT a
    RecoverableContextError: it must take the retry path (count the try, redo
    with feedback) — max_retries+1 invocations, then BLOCKED — rather than the
    one-and-done decompose path (Clif 2026-06-25, plan A)."""
    orch = _make_orchestrator(project_with_run)
    task = _make_task()
    summary = RunSummary(project=project_with_run)

    call_count = {"n": 0}

    def fake_producer(self, t, corrective_notes=""):
        call_count["n"] += 1
        raise context_budget.CompressionChurnExceeded(compressions=4, limit=3)

    monkeypatch.setattr(Orchestrator, "_producer_execute", fake_producer)
    orch._run_task_with_redo(task, summary)

    assert call_count["n"] == 4, (
        "compression churn must retry like a generic exception (counts as a "
        f"try), not decompose one-and-done; got {call_count['n']}, expected 4"
    )
    assert task.status == TaskStatus.BLOCKED


# ── overflow → decompose (2026-05-30) ────────────────────────────────────

def _ctx_err(tmp_path, est=200_000, cap=16_000):
    return context_budget.RecoverableContextError(
        model="m", estimated_tokens=est, max_input_tokens=cap,
        checkpoint_path=tmp_path / "cp.json",
    )


def _planner_returns(monkeypatch, payload: str):
    """Mock _run so the planner re-decompose call returns `payload`."""
    monkeypatch.setattr(
        Orchestrator, "_run",
        lambda self, role, prompt, **kw: payload,
    )


def test_attempt_decompose_splits_into_children(project_with_run, monkeypatch, tmp_path):
    """The planner's split → child Tasks that inherit goal/deps, carry
    depth+1, and get parent-derived ids."""
    orch = _make_orchestrator(project_with_run)
    _planner_returns(monkeypatch,
        '[{"description":"research aider","output_path":"drafts/aider.md"},'
        '{"description":"research swe-agent","output_path":"drafts/swe.md"}]')
    parent = _make_task()
    children = orch._attempt_decompose(parent, _ctx_err(tmp_path))
    assert children is not None and len(children) == 2
    assert [c.id for c in children] == [f"{parent.id}-D1", f"{parent.id}-D2"]
    assert all(c.goal_id == parent.goal_id for c in children)
    assert all(c.depends_on == parent.depends_on for c in children)
    assert all(c.decompose_depth == parent.decompose_depth + 1 for c in children)
    assert children[0].description == "research aider"
    assert children[0].output_path == "drafts/aider.md"


def test_attempt_decompose_recursion_cap(project_with_run, monkeypatch, tmp_path):
    """At the depth cap, don't split again — escalate (genuine stuck)."""
    orch = _make_orchestrator(project_with_run)
    _planner_returns(monkeypatch, '[{"description":"a"},{"description":"b"}]')
    parent = _make_task()
    parent.decompose_depth = orch._MAX_DECOMPOSE_DEPTH
    assert orch._attempt_decompose(parent, _ctx_err(tmp_path)) is None


def test_attempt_decompose_junk_returns_none(project_with_run, monkeypatch, tmp_path):
    """Planner returns non-JSON → no split → None (falls through to ticket)."""
    orch = _make_orchestrator(project_with_run)
    _planner_returns(monkeypatch, "I cannot split this further.")
    assert orch._attempt_decompose(_make_task(), _ctx_err(tmp_path)) is None


def test_attempt_decompose_single_child_is_not_a_split(project_with_run, monkeypatch, tmp_path):
    """Fewer than 2 children isn't a real split → None."""
    orch = _make_orchestrator(project_with_run)
    _planner_returns(monkeypatch, '[{"description":"only one"}]')
    assert orch._attempt_decompose(_make_task(), _ctx_err(tmp_path)) is None


def test_decompose_and_run_parent_completes_via_children(project_with_run, monkeypatch, tmp_path):
    """All children complete → parent settles COMPLETED (container), no ticket."""
    orch = _make_orchestrator(project_with_run)
    _planner_returns(monkeypatch, '[{"description":"a"},{"description":"b"}]')
    monkeypatch.setattr(Orchestrator, "_run_task_with_redo",
        lambda self, t, summary, **kw: setattr(t, "status", TaskStatus.COMPLETED))
    parent = _make_task()
    summary = RunSummary(project=project_with_run)
    handled = orch._try_decompose_and_run(parent, _ctx_err(tmp_path), summary)
    assert handled is True
    assert parent.status == TaskStatus.COMPLETED
    tickets = store.list_tickets(project_with_run.code, run_id=project_with_run.run_id)
    assert not [tk for tk in tickets if tk.affected_task_id == parent.id]


def test_decompose_and_run_parent_blocks_if_a_child_fails(project_with_run, monkeypatch, tmp_path):
    """A child that doesn't complete → parent BLOCKED (handled, not silent)."""
    orch = _make_orchestrator(project_with_run)
    _planner_returns(monkeypatch, '[{"description":"a"},{"description":"b"}]')
    monkeypatch.setattr(Orchestrator, "_run_task_with_redo",
        lambda self, t, summary, **kw: setattr(
            t, "status",
            TaskStatus.BLOCKED if t.id.endswith("-D2") else TaskStatus.COMPLETED))
    parent = _make_task()
    summary = RunSummary(project=project_with_run)
    handled = orch._try_decompose_and_run(parent, _ctx_err(tmp_path), summary)
    assert handled is True
    assert parent.status == TaskStatus.BLOCKED


def test_decompose_and_run_falls_through_when_cannot_split(project_with_run, monkeypatch, tmp_path):
    """Planner can't split → _try_decompose_and_run returns False (caller then
    blocks + tickets — the genuine-stuck path)."""
    orch = _make_orchestrator(project_with_run)
    _planner_returns(monkeypatch, "no split possible")
    summary = RunSummary(project=project_with_run)
    assert orch._try_decompose_and_run(_make_task(), _ctx_err(tmp_path), summary) is False
