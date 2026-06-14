"""Regression tests for the 0.9.0 LOW-severity audit fixes in
``orchestration.py``.

- #68 a vision CONVERSE turn must be billed against ``leader-chat``, not the
  ``leader-decompose`` budget role (the vision-in-kickoff default).
- #69 ``_pin_attachments`` must not crash on binary attachment content (a
  bytes payload or an undecodable file on disk) — it skips the bad one.
- #70 a non-positive ``max_retries`` must still give a task ONE attempt rather
  than skip the loop and crash the escalation None-unpack with a TypeError.
- #71 a crashed wave worker must tear down its per-task staging directory
  rather than leak it under ``.staging/``.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from modulatio import vault
from modulatio.orchestration import Orchestrator, RunSummary
from modulatio.types import (
    Project,
    ProjectState,
    Task,
    TaskStatus,
)


PROJECT_CODE = "LOW"


@pytest.fixture
def project(tmp_path: Path, monkeypatch) -> Project:
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    vault.init_project(PROJECT_CODE, "low-audit fixture", "obj")
    return Project(
        code=PROJECT_CODE, name="low-audit fixture", objective="obj",
        state=ProjectState.ACTIVE, leader_model="stub",
        run_id="run-low",
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
    return Task(
        id=tid, project_id=pid, goal_id="LOW-G-001",
        description=f"task {tid}", **kw,
    )


# ── #68: vision converse turn bills against leader-chat ───────────────────────
def test_vision_converse_bills_leader_chat_not_decompose(project: Project):
    """A converse turn carrying an image must run _run_multimodal_leader with
    ``budget_role='leader-chat'`` — not the ``leader-decompose`` default."""
    orch = _orch(project)
    captured: dict = {}

    def _fake_multimodal(*, prompt, attachments, chat_completion,
                         budget_role="leader-decompose"):
        captured["budget_role"] = budget_role
        return "ok"

    orch._run_multimodal_leader = _fake_multimodal  # type: ignore[assignment]
    # Pretend a leader chat runner is wired so converse doesn't short-circuit.
    orch._resolve_chat_runner = lambda role: (lambda p: "")  # type: ignore[assignment]

    image = SimpleNamespace(kind="image", name="pic.png", path="pic.png",
                            content=None)
    reply = orch.converse("look at this", attachments=[image])

    assert reply == "ok"
    assert captured["budget_role"] == "leader-chat", (
        "a vision converse turn must bill against leader-chat"
    )


def test_run_multimodal_leader_default_budget_role_unchanged():
    """The decompose caller passes no budget_role — the default must remain
    'leader-decompose' so the kickoff vision path is byte-identical."""
    import inspect

    sig = inspect.signature(Orchestrator._run_multimodal_leader)
    assert sig.parameters["budget_role"].default == "leader-decompose"


# ── #69: _pin_attachments survives binary content ─────────────────────────────
def test_pin_attachments_skips_bytes_content(project: Project):
    """A document attachment whose ``content`` is bytes (binary) must be
    skipped (write_text raises TypeError) — not crash the whole pin step."""
    orch = _orch(project)
    good = SimpleNamespace(kind="document", name="ok.md", path="ok.md",
                           content="hello")
    binary = SimpleNamespace(kind="document", name="bin.docx", path="bin.docx",
                             content=b"\x00\x01\x02binary")

    # Must not raise; the good one still pins.
    orch._pin_attachments([binary, good])

    assert "ok.md" in [str(p) for p in orch._pinned_files]
    assert "bin.docx" not in [str(p) for p in orch._pinned_files]


def test_pin_attachments_skips_undecodable_disk_file(project: Project, tmp_path):
    """A document attachment with content=None pointing at an undecodable
    file on disk (UnicodeDecodeError <: ValueError) must be skipped."""
    orch = _orch(project)
    blob = tmp_path / "raw.bin"
    blob.write_bytes(b"\xff\xfe\x00\x80not-utf8")
    bad = SimpleNamespace(kind="document", name="raw.bin", path=str(blob),
                          content=None)
    good = SimpleNamespace(kind="document", name="ok.md",
                           path=str(tmp_path / "ok.md"), content="hi")

    orch._pin_attachments([bad, good])

    names = [str(p) for p in orch._pinned_files]
    assert "ok.md" in names
    assert "raw.bin" not in names


# ── #70: non-positive max_retries still runs one attempt (no None-unpack) ──────
def test_negative_max_retries_runs_one_attempt(project: Project):
    """A task with max_retries < 0 must still get ONE producer attempt — the
    loop body must run rather than fall through to the escalation None-unpack
    (which raised TypeError on last_qc=None)."""
    orch = _orch(project)
    pid = project.id
    task = _make_task(pid, "LOW-T-001", deliverable=True, max_retries=-1)
    task.status = TaskStatus.PENDING

    attempts: list[int] = []

    # Stub the producer so the loop body executes and QC passes on the one
    # attempt it gets — proving the body ran (and no None-unpack crash).
    def _producer_execute(t, corrective_notes=""):
        attempts.append(t.retry_count)
        return (Path("draft.md"), "csum", 100)

    def _qc_review(t, draft_path, checksum, token_count):
        from modulatio.types import AssertionEvidence
        return (AssertionEvidence(producer="qc", primary=True,
                                  check="ok", passed=True), "", None)

    orch._producer_execute = _producer_execute  # type: ignore[assignment]
    orch._qc_review = _qc_review  # type: ignore[assignment]

    summary = RunSummary(project=project)
    # Must not raise TypeError from the escalation last_qc unpack.
    orch._run_task_with_redo(task, summary)

    assert attempts == [0], "a task must always get at least one attempt"


# ── #71: a crashed wave worker tears down its staging dir ─────────────────────
def test_crashed_wave_worker_cleans_staging(project: Project):
    """If _run_task_with_redo escapes with an unexpected exception, the
    per-task staging directory must be removed (not leaked) before the
    exception propagates to the wave collector."""
    orch = _orch(project)
    pid = project.id
    task = _make_task(pid, "LOW-T-009", deliverable=True)

    staging_dir = orch._scope_root() / ".staging" / task.id

    def _boom(t, summary, initial_corrective_notes=""):
        # The staging dir exists at this point (seeded by the caller).
        assert staging_dir.exists()
        raise RuntimeError("engine bug in the worker")

    orch._run_task_with_redo = _boom  # type: ignore[assignment]

    with pytest.raises(RuntimeError, match="engine bug"):
        orch._execute_task_isolated(task, "")

    assert not staging_dir.exists(), (
        "a crashed worker must not leak its staging directory"
    )
