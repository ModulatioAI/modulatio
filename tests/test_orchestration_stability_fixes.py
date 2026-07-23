"""Regression tests for the 0.9.0 stability fixes in ``orchestration.py``.

Each test reproduces a confirmed debug finding and asserts the fix:

- #1/#6 leader auto-redo that produces zero COMPLETED tasks must still settle
  the goal to a terminal state (no permanent IN_PROGRESS hang).
- #2 the redo/resume re-run lanes must execute dependency-aware, not store
  order, and cascade dep-failures.
- #3 QC-as-fixer must not crash on a binary/media artifact.
- #4 ``_read_task_artifact`` must degrade (None) on a binary deliverable.
- #5 ``_draft_is_multifile`` must not crash on a binary draft.
- #7 the multimodal converse path must coalesce a None model content.
- #8 a converse turn that raises must leave the durable log paired.
- #9 the converse PROMPT must be windowed (bounded), not the full transcript.
- #11 ``team_status`` must degrade when a store read raises.
- #12 a corrupt prior-state file must not abort the whole kickoff at intake.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from modulatio import store, vault
from modulatio.orchestration import (
    Orchestrator,
    _draft_is_multifile,
)
from modulatio.types import (
    Goal,
    GoalStatus,
    Project,
    ProjectState,
    Task,
    TaskStatus,
)


PROJECT_CODE = "STB"



def _seed_task_record(code, task, body="", run_id=None):
    """Create-or-update seeding: the engine's ordinary saves never create,
    and tests seed records the production paths assume already exist."""
    from modulatio import store as _seed_store
    from modulatio.types import ToolBudgetConflict
    try:
        return _seed_store.create_task(code, task, body=body, run_id=run_id)
    except ToolBudgetConflict:
        return _seed_store.save_task(code, task, body=body, run_id=run_id)


@pytest.fixture
def project(tmp_path: Path, monkeypatch) -> Project:
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    vault.init_project(PROJECT_CODE, "stability fixture", "obj")
    return Project(
        code=PROJECT_CODE, name="stability fixture", objective="obj",
        state=ProjectState.ACTIVE, leader_model="stub",
        wiki_path=str(vault.project_dir(PROJECT_CODE)),
    )


def _runners() -> dict:
    return {
        "leader": lambda p: "", "planner": lambda p: "```json\n[]\n```",
        "drafter": lambda p: "", "qc": lambda p: "",
    }


def _orch(project: Project) -> Orchestrator:
    return Orchestrator(project, _runners())


def _make_task(pid, tid: str, **kw) -> Task:
    task = Task(
        id=tid, project_id=pid, goal_id="STB-G-001",
        description=f"task {tid}", **kw,
    )
    # The reopened/redo paths under test update the durable record
    # (ordinary saves never create); register at construction.
    try:
        _seed_task_record(PROJECT_CODE, task)
    except Exception:
        pass
    return task


def _make_goal(pid) -> Goal:
    return Goal(
        id="STB-G-001", project_id=pid,
        description="ship the thing", success_criteria="done",
    )


# ── #1/#6: zero-completed redo still settles the goal ────────────────────────
def test_auto_redo_with_zero_completed_settles_goal(project: Project):
    """When the redo pass leaves NO task COMPLETED, _leader_auto_redo must
    still drive the goal to a terminal state — never leave it IN_PROGRESS."""
    orch = _orch(project)
    pid = project.id
    goal = _make_goal(pid)
    goal.status = GoalStatus.IN_PROGRESS
    task = _make_task(pid, "STB-T-001", deliverable=True)
    task.status = TaskStatus.QC_REJECTED
    store.save_goal(PROJECT_CODE, goal, run_id=project.run_id)
    _seed_task_record(PROJECT_CODE, task, run_id=project.run_id)

    # Make the redo run land the task QC_REJECTED again (zero completed) and
    # never re-verify (so the only terminal must come from the new settle path).
    def _redo_run(t, summary, initial_corrective_notes=""):
        t.status = TaskStatus.QC_REJECTED

    orch._run_task_with_redo = _redo_run  # type: ignore[assignment]
    # Force the sequential redo path for determinism.
    orch._concurrent_waves_enabled = lambda *_a, **_k: False  # type: ignore[assignment]

    from modulatio.orchestration import RunSummary

    summary = RunSummary(project=project)
    orch._leader_auto_redo(
        goal, [task], "still not good enough", Path("report.md"), summary,
    )

    assert goal.status == GoalStatus.COMPLETED, (
        "a zero-completed redo must still terminate the goal"
    )
    # The reservation is surfaced to the operator.
    assert any("no completed work" in r.get("concern", "")
               for r in summary.recommendations)


# ── #2: re-run lanes are dependency-aware ────────────────────────────────────
def test_run_reopened_tasks_honors_dependency_order(project: Project):
    """_run_reopened_tasks must run a task's dependency BEFORE the task,
    regardless of store-list order (the bug ran them in store order)."""
    orch = _orch(project)
    pid = project.id
    goal = _make_goal(pid)
    t1 = _make_task(pid, "STB-T-001")
    t2 = _make_task(pid, "STB-T-002", depends_on=["STB-T-001"])
    t1.status = TaskStatus.PENDING
    t2.status = TaskStatus.PENDING

    ran: list[str] = []

    def _run(t, summary, initial_corrective_notes=""):
        ran.append(t.id)
        t.status = TaskStatus.COMPLETED

    orch._run_task_with_redo = _run  # type: ignore[assignment]
    orch._concurrent_waves_enabled = lambda *_a, **_k: False  # type: ignore[assignment]

    from modulatio.orchestration import RunSummary

    # Pass tasks in REVERSE (dependent first) — the bug would run T2 first.
    orch._run_reopened_tasks(goal, [t2, t1], RunSummary(project=project))

    assert ran == ["STB-T-001", "STB-T-002"], (
        "dependency must run before its dependent on the re-run lane"
    )


def test_run_reopened_tasks_cascades_dep_failure(project: Project):
    """A task whose dependency FAILED must be cascaded to BLOCKED with no
    producer call (no drafting against missing input)."""
    orch = _orch(project)
    pid = project.id
    goal = _make_goal(pid)
    t1 = _make_task(pid, "STB-T-001")
    t2 = _make_task(pid, "STB-T-002", depends_on=["STB-T-001"])
    # T1 already terminally failed; T2 is runnable.
    t1.status = TaskStatus.QC_REJECTED
    t2.status = TaskStatus.PENDING

    ran: list[str] = []

    def _run(t, summary, initial_corrective_notes=""):
        ran.append(t.id)
        t.status = TaskStatus.COMPLETED

    orch._run_task_with_redo = _run  # type: ignore[assignment]
    orch._concurrent_waves_enabled = lambda *_a, **_k: False  # type: ignore[assignment]

    from modulatio.orchestration import RunSummary

    orch._run_reopened_tasks(goal, [t1, t2], RunSummary(project=project))

    assert ran == [], "no producer call when the dependency failed"
    assert t2.status == TaskStatus.BLOCKED


def test_reexecute_goal_routes_reopened_tasks_dependency_ordered(project: Project):
    """Re-filed #1 lane wiring: _reexecute_goal must re-run the reopened
    (PENDING) tasks through the dependency-ordered _run_reopened_tasks path —
    not the old arbitrary store-list order.

    store.list_tasks returns tasks id-sorted, so T-001 comes first by store
    order. We make T-001 DEPEND ON T-002, so topological order is the REVERSE
    of store order: the buggy store-order lane would run T-001 (against its
    not-yet-run dependency) first; the fix must run T-002 first."""
    orch = _orch(project)
    pid = project.id
    goal = _make_goal(pid)
    goal.status = GoalStatus.IN_PROGRESS
    t1 = _make_task(pid, "STB-T-001", depends_on=["STB-T-002"])
    t2 = _make_task(pid, "STB-T-002")
    t1.status = TaskStatus.PENDING
    t2.status = TaskStatus.PENDING
    store.save_goal(PROJECT_CODE, goal, run_id=project.run_id)
    _seed_task_record(PROJECT_CODE, t1, run_id=project.run_id)
    _seed_task_record(PROJECT_CODE, t2, run_id=project.run_id)

    ran: list[str] = []

    def _run(t, summary, initial_corrective_notes=""):
        ran.append(t.id)
        t.status = TaskStatus.COMPLETED

    orch._run_task_with_redo = _run  # type: ignore[assignment]
    orch._leader_verify_goal = lambda *a, **k: None  # type: ignore[assignment]

    from modulatio.orchestration import RunSummary

    orch._reexecute_goal(goal, RunSummary(project=project))

    assert ran == ["STB-T-002", "STB-T-001"], (
        "the decline-driven re-run lane must honor dependency order, not "
        "store-list (id-sorted) order"
    )


# ── #3: QC-as-fixer survives a binary artifact ───────────────────────────────
def test_qc_fix_forward_survives_binary_artifact(project: Project, tmp_path: Path):
    """_attempt_qc_fix_forward must not crash (UnicodeDecodeError) on a binary
    draft; it falls through to the caller's graceful terminal (returns False)."""
    orch = _orch(project)
    binary = tmp_path / "clip.mp4"
    binary.write_bytes(b"\x00\x01\x02\xff\xfe" + b"\x80" * 64)

    from modulatio.orchestration import RunSummary

    task = _make_task(project.id, "STB-T-001", deliverable=True)
    # Should return False (fall-through), NOT raise.
    result = orch._attempt_qc_fix_forward(
        task, binary, None, RunSummary(project=project),
    )
    assert result is False


# ── #4: _read_task_artifact degrades on binary ───────────────────────────────
def test_read_task_artifact_returns_none_on_binary(project: Project, tmp_path: Path):
    orch = _orch(project)
    pid = project.id
    task = _make_task(pid, "STB-T-001", output_path="drafts/out.bin")
    artifacts = orch._artifacts_root()
    target = artifacts / "drafts" / "out.bin"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"\xff\xfe\x00\x80binary")

    # No raise; returns None for an unreadable binary.
    assert orch._read_task_artifact(task) is None
    # And the deliverable fingerprint that uses it does not crash either.
    assert isinstance(orch._goal_deliverable_fingerprint([task]), str)


# ── #5: _draft_is_multifile survives a binary draft ──────────────────────────
def test_draft_is_multifile_survives_binary(project: Project, tmp_path: Path):
    binary = tmp_path / "out.zip"
    binary.write_bytes(b"PK\x03\x04\x00\x80\xff" + b"\x00" * 32)
    task = _make_task(project.id, "STB-T-001")
    # Not code, not diff mode -> reads the file; binary must not raise.
    assert _draft_is_multifile(task, binary) is False


# ── #7: multimodal None content is coalesced ─────────────────────────────────
def test_multimodal_leader_coalesces_none_content(project: Project, tmp_path: Path):
    from modulatio.attachments import build_attachment

    img = tmp_path / "pic.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
    att = build_attachment(img, kind="image")

    orch = _orch(project)

    def fake_completion(*, model, messages, **kwargs):
        # A vision refusal / safety stop returns content=None.
        msg = SimpleNamespace(content=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)])

    reply = orch._run_multimodal_leader(
        prompt="describe this", attachments=[att],
        chat_completion=fake_completion,
    )
    assert reply == ""  # coalesced, never None


def test_multimodal_leader_coalesces_empty_choices(project: Project, tmp_path: Path):
    from modulatio.attachments import build_attachment

    img = tmp_path / "pic.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
    att = build_attachment(img, kind="image")
    orch = _orch(project)

    def fake_completion(*, model, messages, **kwargs):
        return SimpleNamespace(choices=[])

    reply = orch._run_multimodal_leader(
        prompt="x", attachments=[att], chat_completion=fake_completion,
    )
    assert reply == ""


def test_converse_with_image_none_content_does_not_crash(
    project: Project, tmp_path: Path, monkeypatch
):
    """End-to-end: an image turn whose model returns None content must not
    raise (the old TypeError in _redact_secrets) — the log stays paired."""
    from modulatio.attachments import build_attachment
    from modulatio.runners import ChatResponse

    img = tmp_path / "pic.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
    att = build_attachment(img, kind="image")

    orch = Orchestrator(
        project, _runners(),
        chat_runners={"leader": lambda **k: ChatResponse(
            content="unused", tool_calls=())},
    )

    def fake_multimodal(*, prompt, attachments, chat_completion, budget_role="leader-decompose"):
        return ""  # the coalesced None

    monkeypatch.setattr(orch, "_run_multimodal_leader", fake_multimodal)
    reply = orch.converse("what's in this?", attachments=[att])
    assert reply == ""
    thread = orch._load_conversation()
    assert [t["role"] for t in thread] == ["operator", "leader"]


# ── #8: converse leaves a paired log when the turn raises ─────────────────────
def test_converse_pairs_log_on_failure(project: Project, monkeypatch):
    """If the model call raises, converse must still append a leader turn (so
    the durable log never ends on an unanswered operator turn) and answer
    IN-LANE — the no-block invariant: the failure returns as the reply,
    never an exception a surface turns into a 500 or a dead worker."""
    from modulatio.runners import ChatResponse

    orch = Orchestrator(
        project, _runners(),
        chat_runners={"leader": lambda **k: ChatResponse(
            content="x", tool_calls=())},
    )

    def boom(**_):
        raise RuntimeError("budget exhausted mid-turn")

    monkeypatch.setattr(orch, "_run_chat_loop", boom)

    reply = orch.converse("please do the thing")
    assert "failed" in reply.lower()

    thread = orch._load_conversation()
    # operator turn paired with a synthetic leader-error turn.
    assert [t["role"] for t in thread] == ["operator", "leader"]
    assert "failed" in thread[1]["content"].lower()


# ── #9: converse prompt is windowed ──────────────────────────────────────────
def test_converse_prompt_is_windowed(project: Project):
    orch = _orch(project)
    window = orch._CONVERSE_PROMPT_WINDOW
    thread = [
        {"role": "operator" if i % 2 == 0 else "leader", "content": f"turn-{i}"}
        for i in range(window + 20)
    ]
    prompt = orch._build_converse_prompt(thread, "newest", [])
    # The oldest turns are dropped from the PROMPT (durable log is untouched).
    assert "turn-0" not in prompt
    assert f"turn-{window + 19}" in prompt
    assert "newest" in prompt


# ── #11: team_status degrades on a corrupt store read ────────────────────────
def test_team_status_degrades_on_corrupt_store(project: Project, monkeypatch):
    """A corrupt goal/task file must not brick the Leader's team_status view —
    it degrades to a warning line instead of raising."""
    orch = _orch(project)
    # Give it a run scope so team_status proceeds past the no-run guard.
    monkeypatch.setattr(orch, "_converse_run_scope", lambda: "run-1")

    def boom(*_a, **_k):
        raise ValueError("corrupt YAML front-matter")

    monkeypatch.setattr(store, "list_goals", boom)
    monkeypatch.setattr(store, "list_tasks", boom)
    monkeypatch.setattr(store, "list_tickets", lambda *a, **k: [])

    tools = orch._leader_function_tools()
    out = tools["team_status"].call()
    assert "could not read" in out.lower()


# ── #12: corrupt prior state does not abort the whole kickoff at intake ───────
def test_kickoff_intake_survives_corrupt_prior_state(project: Project, monkeypatch):
    """A store read that raises during the intake scans (auto-resume / drain)
    must be recorded, not abort the kickoff before new work decomposes."""
    orch = _orch(project)

    def boom(self, summary):
        raise ValueError("corrupt entity at intake")

    # The INTAKE auto-resume blows up; the kickoff must still proceed.
    monkeypatch.setattr(
        Orchestrator, "_auto_resume_refreshable_goals", boom, raising=True,
    )
    # Only the FIRST drain (intake, pre-decompose) raises; later wind-down
    # drains run normally so the test isolates the intake guard.
    real_drain = Orchestrator._drain_decided_tickets
    drain_calls = {"n": 0}

    def maybe_boom_drain(self, summary):
        drain_calls["n"] += 1
        if drain_calls["n"] == 1:
            raise ValueError("corrupt entity at intake")
        return real_drain(self, summary)

    monkeypatch.setattr(
        Orchestrator, "_drain_decided_tickets", maybe_boom_drain, raising=True,
    )
    decomposed: dict = {}

    def fake_decompose(objective, attachments=None, chat_completion=None):
        decomposed["called"] = True
        return []  # no goals -> kickoff finishes cleanly

    monkeypatch.setattr(orch, "_leader_decompose", fake_decompose)

    summary = orch.kickoff("a brand new objective")
    assert decomposed.get("called"), "kickoff must reach decompose despite intake errors"
    assert any("could not read prior state" in e for e in summary.errors)


# ── Zero-completed redo/resume lanes must settle, not strand ─────────────────

def test_settle_zero_completed_drives_goal_terminal_and_is_idempotent(project: Project):
    """The shared settle helper drives a non-terminal goal to COMPLETED with a
    PQR reservation, and is a no-op once the goal is already terminal."""
    from modulatio.orchestration import RunSummary
    orch = _orch(project)
    goal = _make_goal(project.id)
    goal.status = GoalStatus.IN_PROGRESS
    summary = RunSummary(project=project)

    orch._settle_zero_completed(
        goal, summary, concern="no completed work on the redo", rationale="settled: zero",
    )
    assert goal.status == GoalStatus.COMPLETED
    assert any("no completed work" in r.get("concern", "") for r in summary.recommendations)
    assert any(t.to_state == GoalStatus.COMPLETED.value for t in goal.transitions)

    # Idempotent: a second call on the now-terminal goal adds nothing.
    n_before = len(summary.recommendations)
    orch._settle_zero_completed(goal, summary, concern="again", rationale="again")
    assert len(summary.recommendations) == n_before


def test_reexecute_goal_zero_completed_settles_goal(project: Project):
    """A decline-driven _reexecute_goal whose reopened task re-fails
    (zero COMPLETED, no other completed task) must settle the goal terminal with
    a reservation — NOT leave it permanently IN_PROGRESS."""
    from modulatio.orchestration import RunSummary
    orch = _orch(project)
    goal = _make_goal(project.id)
    goal.status = GoalStatus.IN_PROGRESS
    task = _make_task(project.id, "STB-T-001")
    task.status = TaskStatus.PENDING  # reopened by the decline
    store.save_goal(PROJECT_CODE, goal, run_id=project.run_id)
    _seed_task_record(PROJECT_CODE, task, run_id=project.run_id)

    def _redo(t, summary, initial_corrective_notes=""):
        t.status = TaskStatus.QC_REJECTED  # re-fails again → zero completed
        _seed_task_record(PROJECT_CODE, t, run_id=project.run_id)

    orch._run_task_with_redo = _redo  # type: ignore[assignment]
    # _leader_verify_goal must NOT be the thing that terminalizes here.
    orch._leader_verify_goal = lambda *a, **k: None  # type: ignore[assignment]

    orch._reexecute_goal(goal, RunSummary(project=project))

    assert goal.status == GoalStatus.COMPLETED, "zero-completed decline redo must settle the goal"
    reloaded = store.get_goal(PROJECT_CODE, goal.id, run_id=project.run_id)
    assert reloaded.status == GoalStatus.COMPLETED


def test_auto_resume_zero_completed_settles_goal(project: Project):
    """Sibling case: budget auto-resume whose tasks all re-fail on the
    fresh budget must settle the goal terminal — not strand it IN_PROGRESS with
    an orphaned RESOLVED ticket."""
    from datetime import datetime, timedelta, timezone

    from modulatio.orchestration import RunSummary
    from modulatio.types import Ticket, TicketPriority, TicketStatus
    orch = _orch(project)
    goal = _make_goal(project.id)
    goal.status = GoalStatus.IN_PROGRESS
    task = _make_task(project.id, "STB-T-001")
    task.status = TaskStatus.QC_REJECTED
    store.save_goal(PROJECT_CODE, goal, run_id=project.run_id)
    _seed_task_record(PROJECT_CODE, task, run_id=project.run_id)

    # A BLOCKER ticket whose refresh_at has already passed, pointing at the goal.
    ticket = Ticket(
        id=f"{PROJECT_CODE}-1", project_id=project.id, priority=TicketPriority.BLOCKER,
        status=TicketStatus.OPEN, title="budget exhausted", body="b",
        affected_goal_id=goal.id,
        refresh_at=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    store._write_entity(store._ticket_path(PROJECT_CODE, ticket.id, run_id=project.run_id), ticket, "b")

    def _redo(t, summary, initial_corrective_notes=""):
        t.status = TaskStatus.QC_REJECTED  # fails again on the fresh budget
        _seed_task_record(PROJECT_CODE, t, run_id=project.run_id)

    orch._run_task_with_redo = _redo  # type: ignore[assignment]
    orch._leader_verify_goal = lambda *a, **k: None  # type: ignore[assignment]

    orch._auto_resume_refreshable_goals(RunSummary(project=project))

    reloaded = store.get_goal(PROJECT_CODE, goal.id, run_id=project.run_id)
    assert reloaded.status == GoalStatus.COMPLETED, "zero-completed auto-resume must settle the goal"

