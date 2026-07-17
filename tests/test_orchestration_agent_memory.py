# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Per-agent episodic memory from the run path (Feng-Tui refinement arc, W5b).

Agents never accumulated memories from jobs: ``agent_memory.add_episodic`` had
ZERO orchestration callers — only chat and the TUI manual-add wrote it, so the
MEMORY tab's "accrues episodic memory as it works" promise was false. The
``_run_task_with_redo`` wrapper now records one deterministic episodic entry
per party (producer + QC) when a task reaches a terminal status — synthesized
purely from task fields, no LLM call, routed through ``_store_write_deferrable``
so the isolated-worker contract (no worker store writes) holds.
"""

from __future__ import annotations

import pytest

from modulatio import vault
from modulatio.memory import agent_memory
from modulatio.orchestration import Orchestrator, RunSummary
from modulatio.types import Project, Task, TaskStatus


@pytest.fixture
def orch(tmp_path, monkeypatch):
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path / "vault")
    vault.init_project("MEM", "Mem", "obj", exist_ok=True)
    pr = Project(
        code="MEM", name="Mem", objective="obj", leader_model="stub",
        run_id="20260703T220000Z-abc123",
        wiki_path=str(vault.project_dir("MEM")),
    )
    return Orchestrator(pr, {"leader": lambda p: "", "planner": lambda p: "",
                             "drafter": lambda p: "", "qc": lambda p: ""})


def _task(status=TaskStatus.COMPLETED, *, agent="prod-1", qc="qc-1", **kw):
    fields = dict(
        id="MEM-T-001", project_id=None, goal_id="MEM-G-001",
        description="Research the sports betting post-PASPA landscape",
        artifact_kind="research", operation="research",
        assigned_agent_id=agent, qc_agent_id=qc,
        lifetime_attempts=2, status=status,
    )
    fields.update(kw)
    t = Task(**{k: v for k, v in fields.items() if k != "project_id"},
             project_id=__import__("uuid").uuid4())
    return t


def _run_with_stubbed_inner(orch, task, monkeypatch, *, end_status):
    """Drive the wrapper with the inner body stubbed to just stamp a status."""
    def _inner(self, t, summary, initial_corrective_notes=""):
        t.status = end_status

    monkeypatch.setattr(Orchestrator, "_run_task_with_redo_inner", _inner)
    orch._run_task_with_redo(task, RunSummary(project=orch.project))


def test_completed_task_writes_producer_and_qc_episodes(orch, monkeypatch):
    t = _task()
    _run_with_stubbed_inner(orch, t, monkeypatch, end_status=TaskStatus.COMPLETED)

    prod = agent_memory.get_episodic("prod-1", project_code="MEM")
    assert len(prod) == 1
    assert "completed" in prod[0].content
    assert "Research the sports betting" in prod[0].content
    assert "2 producer attempt(s)" in prod[0].content
    assert prod[0].tags == ["research"]  # artifact_kind == operation → deduped, not doubled

    qc = agent_memory.get_episodic("qc-1", project_code="MEM")
    assert len(qc) == 1
    assert "reviewed MEM-T-001" in qc[0].content


def test_rejected_terminal_status_writes(orch, monkeypatch):
    t = _task()
    _run_with_stubbed_inner(orch, t, monkeypatch, end_status=TaskStatus.QC_REJECTED)
    prod = agent_memory.get_episodic("prod-1", project_code="MEM")
    assert len(prod) == 1 and "qc_rejected" in prod[0].content


def test_qc_authored_fix_is_named_in_the_episode(orch, monkeypatch):
    t = _task(qc_authored_fix=True)
    _run_with_stubbed_inner(orch, t, monkeypatch, end_status=TaskStatus.COMPLETED)
    prod = agent_memory.get_episodic("prod-1", project_code="MEM")
    assert "(QC-authored fix)" in prod[0].content


def test_none_agent_ids_are_skipped(orch, monkeypatch):
    t = _task(agent=None, qc=None)
    _run_with_stubbed_inner(orch, t, monkeypatch, end_status=TaskStatus.COMPLETED)
    # nothing written anywhere — the memory dir for the project stays empty
    mem_root = vault.project_dir("MEM") / "memory"
    files = list(mem_root.rglob("episodic.json")) if mem_root.exists() else []
    assert files == []


def test_non_terminal_status_writes_nothing(orch, monkeypatch):
    t = _task()
    _run_with_stubbed_inner(orch, t, monkeypatch, end_status=TaskStatus.PENDING)
    assert agent_memory.get_episodic("prod-1", project_code="MEM") == []


def test_isolated_worker_defers_the_write(orch, monkeypatch):
    """Worker-isolation contract: with a deferred-writes buffer active, the
    episode must NOT hit disk until the buffered writes are merged."""
    t = _task()
    orch._tls.deferred_writes = []
    _run_with_stubbed_inner(orch, t, monkeypatch, end_status=TaskStatus.COMPLETED)
    assert agent_memory.get_episodic("prod-1", project_code="MEM") == []
    for fn in orch._tls.deferred_writes:
        fn()
    del orch._tls.deferred_writes
    assert len(agent_memory.get_episodic("prod-1", project_code="MEM")) == 1


def test_abandoned_task_records_the_episode(orch, monkeypatch):
    """ABANDONED is the fourth terminal status (a
    Leader-iterate drop) — the producer still worked the task and remembers it."""
    t = _task()
    _run_with_stubbed_inner(orch, t, monkeypatch, end_status=TaskStatus.ABANDONED)
    prod = agent_memory.get_episodic("prod-1", project_code="MEM")
    assert len(prod) == 1 and "abandoned" in prod[0].content


def test_memory_write_failure_never_raises_or_changes_outcome(orch, monkeypatch):
    """The episodic side-channel is
    best-effort — a failing memory disk must not crash the wrapper after the
    task already completed."""
    t = _task()

    def _boom(*a, **k):
        raise OSError("memory disk failed")

    monkeypatch.setattr(agent_memory, "add_episodic", _boom)
    _run_with_stubbed_inner(orch, t, monkeypatch, end_status=TaskStatus.COMPLETED)
    assert t.status == TaskStatus.COMPLETED  # outcome untouched, nothing raised
