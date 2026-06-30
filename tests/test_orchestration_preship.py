"""0.9.0 pre-ship sweep regressions for src/modulatio/orchestration.py.

Each test pins a CONFIRMED finding from docs/design/0.9.0-preship-sweep-findings.md
that lived in orchestration.py. They reuse the fixtures/stubs from
test_orchestration.py where helpful but are self-contained otherwise.
"""
from __future__ import annotations

from uuid import uuid4

from modulatio import vault
from modulatio.orchestration import Orchestrator, RunSummary
from modulatio.types import (
    Goal,
    GoalStatus,
    Project,
    Task,
    TaskStatus,
)


def _orch(tmp_path, monkeypatch, *, code="PRE", leader=None):
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    vault.init_project(code, "preship test", "obj")
    vault.init_run(code, "run-1", "obj")
    project = Project(
        code=code, name="Preship", objective="obj", leader_model="stub",
        wiki_path=str(tmp_path / code.lower()), run_id="run-1",
    )
    runners = {"drafter": lambda p: "x", "qc": lambda p: "ACCEPT"}
    if leader is not None:
        runners["leader"] = leader
    return Orchestrator(project, runners)


def _deliverable(code, *, output) -> Task:
    t = Task(
        id=f"{code}-T-001", project_id=uuid4(), goal_id=f"{code}-G-001",
        description="d", depends_on=[],
    )
    t.status = TaskStatus.COMPLETED
    t.deliverable = True
    t.output_path = output
    return t


# ── #9392: redo loop-breaker fingerprint must see BINARY content changes ─────


def test_fingerprint_tracks_binary_deliverable_changes(tmp_path, monkeypatch):
    """A binary/media deliverable (non-UTF-8) used to collapse to "" every round
    so the loop-breaker forced a false 'stalled' after one redo. The fingerprint
    must now move when the raw bytes change."""
    orch = _orch(tmp_path, monkeypatch)
    art = orch._artifacts_root()
    art.mkdir(parents=True, exist_ok=True)
    task = _deliverable("PRE", output="deck.pptx")

    # Non-UTF-8 bytes (a NUL + high byte) — _read_task_artifact returns None here.
    (art / "deck.pptx").write_bytes(b"PK\x03\x04\x00\xff\xfe binary-v1")
    fp1 = orch._goal_deliverable_fingerprint([task])
    assert fp1 == orch._goal_deliverable_fingerprint([task]), "stable when unchanged"

    (art / "deck.pptx").write_bytes(b"PK\x03\x04\x00\xff\xfe binary-v2-different")
    fp2 = orch._goal_deliverable_fingerprint([task])
    assert fp2 != fp1, "binary deliverable change must move the fingerprint"


def test_fingerprint_distinguishes_two_distinct_binaries(tmp_path, monkeypatch):
    """Two different binary deliverables must NOT hash identically (the old ""
    collapse made every binary identical)."""
    orch = _orch(tmp_path, monkeypatch)
    art = orch._artifacts_root()
    art.mkdir(parents=True, exist_ok=True)
    a = _deliverable("PRE", output="a.bin")
    (art / "a.bin").write_bytes(b"\x00\x01\x02 alpha")
    fp_a = orch._goal_deliverable_fingerprint([a])
    (art / "a.bin").write_bytes(b"\x00\x01\x02 beta-distinct")
    fp_b = orch._goal_deliverable_fingerprint([a])
    assert fp_a != fp_b


# ── #8651: Leader recommendations deduped across redo rounds ──────────────────


def test_record_recommendations_dedups_across_rounds(tmp_path, monkeypatch):
    orch = _orch(tmp_path, monkeypatch)
    summary = RunSummary(project=orch.project)
    goal = Goal(
        id="PRE-G-001", project_id=uuid4(), description="d",
        success_criteria="s", status=GoalStatus.IN_PROGRESS,
    )
    recs = [{"concern": "tone is off", "suggestion": "warm it up"}]
    # The recursive redo verify re-runs _record_recommendations each round.
    orch._record_recommendations(goal, recs, summary)
    orch._record_recommendations(goal, recs, summary)
    orch._record_recommendations(goal, recs, summary)
    matching = [
        r for r in summary.recommendations
        if r.get("concern") == "tone is off"
    ]
    assert len(matching) == 1, "identical reservation must appear once, not per round"


def test_record_recommendations_keeps_distinct_entries(tmp_path, monkeypatch):
    orch = _orch(tmp_path, monkeypatch)
    summary = RunSummary(project=orch.project)
    goal = Goal(
        id="PRE-G-001", project_id=uuid4(), description="d",
        success_criteria="s", status=GoalStatus.IN_PROGRESS,
    )
    orch._record_recommendations(goal, [{"concern": "a", "suggestion": "x"}], summary)
    orch._record_recommendations(goal, [{"concern": "b", "suggestion": "y"}], summary)
    concerns = {r["concern"] for r in summary.recommendations}
    assert {"a", "b"} <= concerns, "distinct reservations must all be kept"


# ── #11219: _pin_attachments tolerates a non-encodable attachment ─────────────


def test_pin_attachments_skips_unencodable_without_aborting(tmp_path, monkeypatch):
    """A UnicodeEncodeError (ValueError) on one attachment must not abort pinning
    the rest. Forcing a non-UTF-8 default locale encoding would be flaky, so we
    assert the explicit-utf-8 write succeeds for legitimate non-ASCII content
    (the case that previously crashed under a C-locale)."""
    orch = _orch(tmp_path, monkeypatch)

    class _Att:
        def __init__(self, name, content):
            self.name = name
            self.kind = "document"
            self.path = None
            self.content = content

    good = _Att("good.md", "résumé — em–dash ✓")  # non-ASCII, valid UTF-8
    orch._pin_attachments([good])
    assert "good.md" in orch._pinned_files
    dest = orch._shared_artifacts_root() / "good.md"
    assert dest.read_text(encoding="utf-8") == "résumé — em–dash ✓"


# ── #10755: reopened cross-goal dep keeps intra-goal ordering ─────────────────


def test_reopened_tasks_preserve_order_with_cross_goal_dep(tmp_path, monkeypatch):
    """A reopened goal whose task depends on a task in ANOTHER goal must still be
    topo-ordered within the goal, not silently collapse to store order."""
    orch = _orch(tmp_path, monkeypatch)
    summary = RunSummary(project=orch.project)
    goal = Goal(
        id="PRE-G-002", project_id=uuid4(), description="d",
        success_criteria="s", status=GoalStatus.IN_PROGRESS,
    )

    ran: list[str] = []

    def _fake_run_task_with_redo(t, _summary, **_kw):
        ran.append(t.id)
        t.status = TaskStatus.COMPLETED

    monkeypatch.setattr(orch, "_run_task_with_redo", _fake_run_task_with_redo)

    def _save(t):
        pass

    monkeypatch.setattr("modulatio.store.save_task", lambda *a, **k: None)
    # X-T-999 is a REAL prior-goal task (COMPLETED) — a legitimate cross-goal dep
    # resolves in the store. (An UNRESOLVABLE/typo id is now fail-closed-blocked;
    # see test_nemo_resweep_remediation — so a valid cross-goal dep must resolve.)
    _xdep = Task(id="X-T-999", project_id=uuid4(), goal_id="PRE-G-001",
                 description="prior-goal unit", depends_on=[],
                 status=TaskStatus.COMPLETED)
    monkeypatch.setattr(
        "modulatio.store.get_task",
        lambda code, tid, run_id=None: _xdep if tid == "X-T-999" else None,
    )

    # B depends on A (intra-goal) AND on X-T-999 (a real, COMPLETED cross-goal dep).
    a = Task(id="PRE-T-A", project_id=uuid4(), goal_id=goal.id,
             description="a", depends_on=[], status=TaskStatus.PENDING)
    b = Task(id="PRE-T-B", project_id=uuid4(), goal_id=goal.id,
             description="b", depends_on=["PRE-T-A", "X-T-999"],
             status=TaskStatus.PENDING)
    # Store order deliberately puts B before A.
    orch._run_reopened_tasks(goal, [b, a], summary)
    assert ran == ["PRE-T-A", "PRE-T-B"], (
        "A must run before B despite the cross-goal dep; got " + str(ran)
    )


# ── #8592: unparseable verify verdict settles the goal terminally ─────────────


def test_verify_unparseable_settles_goal(tmp_path, monkeypatch):
    """A leader-verify response that won't parse used to return leaving the goal
    IN_PROGRESS forever. It must now settle terminally with a PQR reservation."""
    orch = _orch(tmp_path, monkeypatch, leader=lambda p: "not json at all {{{")
    summary = RunSummary(project=orch.project)
    goal = Goal(
        id="PRE-G-003", project_id=uuid4(), description="d",
        success_criteria="s", status=GoalStatus.IN_PROGRESS,
    )
    task = _deliverable("PRE", output="out.md")
    art = orch._artifacts_root()
    art.mkdir(parents=True, exist_ok=True)
    (art / "out.md").write_text("body")

    orch._leader_verify_goal(goal, [task], summary)

    assert goal.status in (GoalStatus.COMPLETED, GoalStatus.BLOCKED), (
        "goal must not be stranded IN_PROGRESS on an unparseable verdict"
    )
    assert any(
        "could not be parsed" in str(r.get("concern", "")).lower()
        or "unparse" in str(r.get("concern", "")).lower()
        for r in summary.recommendations
    )


# ── #8396: dead method removed ───────────────────────────────────────────────


def test_dead_daily_budget_method_removed():
    assert not hasattr(Orchestrator, "_refresh_daily_budget_if_new_day")


# ── #5326: converse offline guard is branch-aware for the vision path ─────────


def test_converse_image_turn_not_offline_when_multimodal_model_set(
    tmp_path, monkeypatch
):
    """An image turn must NOT report 'offline' merely because the TEXT chat runner
    is unwired — the vision path resolves agent_models['leader'] / leader_model."""
    orch = _orch(tmp_path, monkeypatch)
    # No chat runner for leader (text path would be offline)…
    monkeypatch.setattr(orch, "_resolve_chat_runner", lambda *_a, **_k: None)
    # …but a multimodal model IS configured.
    orch.project.agent_models = {"leader": "stub-vision"}

    captured = {}

    def _fake_multimodal(*, prompt, attachments, chat_completion, budget_role):
        captured["called"] = True
        return "vision reply"

    monkeypatch.setattr(orch, "_run_multimodal_leader", _fake_multimodal)

    class _Img:
        name = "pic.png"
        kind = "image"
        path = str(tmp_path / "pic.png")

    reply = orch.converse("look at this", attachments=[_Img()])
    assert captured.get("called"), "vision path must dispatch, not short-circuit offline"
    assert "offline" not in reply.lower()


def test_converse_text_turn_offline_when_no_chat_runner(tmp_path, monkeypatch):
    """The text path keeps its chat-runner-based offline guard."""
    orch = _orch(tmp_path, monkeypatch)
    monkeypatch.setattr(orch, "_resolve_chat_runner", lambda *_a, **_k: None)
    reply = orch.converse("hello", attachments=[])
    assert "offline" in reply.lower()
