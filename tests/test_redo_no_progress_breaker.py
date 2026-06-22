# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Task-level no-progress breaker for ``_run_task_with_redo``.

A producer that reproduces BYTE-IDENTICAL output on consecutive rejected
attempts — despite carrying the QC corrective notes from the prior round — is
stuck against the same wall. The redo loop must stop burning the retry budget
and break EARLY into the existing escalation + QC-authored-fix rescue (a higher
tier or QC's own patch is the path forward, not another identical pass).

This mirrors the goal-level ``stalled`` deliverable-fingerprint breaker
(_leader_verify_goal) at the task grain, and gets a stuck producer to the
QC-as-fixer rescue sooner instead of grinding identical drafts.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from modulatio import vault
from modulatio.orchestration import Orchestrator, RunSummary
from modulatio.types import AssertionEvidence, Project, Task, TaskStatus

PROJECT_CODE = "NPB"


@pytest.fixture
def project_with_run(tmp_path: Path, monkeypatch) -> Project:
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    vault.init_project(PROJECT_CODE, "no-progress breaker", "stuck producer")
    run_id = "run-npb-001"
    vault.init_run(PROJECT_CODE, run_id, "stuck producer")
    return Project(
        code=PROJECT_CODE, name="no-progress breaker", objective="stuck producer",
        leader_model="stub", wiki_path=str(tmp_path / PROJECT_CODE.lower()),
        run_id=run_id,
    )


def _make_task() -> Task:
    # max_retries=3 → range(4) → up to 4 producer attempts absent the breaker.
    return Task(id="NPB-T-001", project_id=uuid4(), goal_id="NPB-G-001",
                description="anything", max_retries=3)


def _orch(project: Project) -> Orchestrator:
    runner = lambda prompt: "stub"  # noqa: E731 — test stub
    return Orchestrator(project, runners=dict.fromkeys(
        ("leader", "planner", "drafter", "researcher", "qc"), runner))


def _reject(check="missing sources", notes="add real sources"):
    return (AssertionEvidence(producer="qc", primary=True, check=check, passed=False),
            notes, "content")


def test_identical_rejected_output_breaks_early_into_rescue(project_with_run, monkeypatch):
    orch = _orch(project_with_run)
    task = _make_task()
    summary = RunSummary(project=project_with_run)
    calls = {"producer": 0, "escalation": 0}

    def fake_producer(self, t, corrective_notes=""):
        calls["producer"] += 1
        return (Path("draft.md"), "IDENTICAL_CSUM", 100)  # same bytes every pass

    def fake_qc(self, task, draft_path, checksum):
        return _reject()

    def fake_escalation(self, t, summary, last_qc):
        calls["escalation"] += 1
        return _reject(check="still missing", notes="still missing")

    monkeypatch.setattr(Orchestrator, "_producer_execute", fake_producer)
    monkeypatch.setattr(Orchestrator, "_qc_review", fake_qc)
    monkeypatch.setattr(Orchestrator, "_run_escalation_attempt", fake_escalation)
    monkeypatch.setattr(Orchestrator, "_attempt_qc_fix_forward", lambda self, *a, **k: False)

    orch._run_task_with_redo(task, summary)

    # attempt 0 (csum X, reject) + attempt 1 (csum X identical → BREAK). The loop
    # must NOT reach attempts 2 and 3.
    assert calls["producer"] == 2, (
        f"identical rejected output must break early; got {calls['producer']} "
        "producer attempts (expected 2)"
    )
    # The break still routes into the rescue chain (escalation ran once).
    assert calls["escalation"] == 1
    assert task.status == TaskStatus.QC_REJECTED


def test_changing_output_runs_full_budget(project_with_run, monkeypatch):
    """The breaker must NOT fire when the producer makes real progress (distinct
    output each attempt) — only byte-identical no-progress is caught."""
    orch = _orch(project_with_run)
    task = _make_task()
    summary = RunSummary(project=project_with_run)
    calls = {"producer": 0}

    def fake_producer(self, t, corrective_notes=""):
        calls["producer"] += 1
        return (Path("draft.md"), f"csum-{calls['producer']}", 100)  # distinct

    monkeypatch.setattr(Orchestrator, "_producer_execute", fake_producer)
    monkeypatch.setattr(Orchestrator, "_qc_review", lambda self, t, dp, cs: _reject())
    monkeypatch.setattr(Orchestrator, "_run_escalation_attempt",
                        lambda self, t, s, lq: _reject())
    monkeypatch.setattr(Orchestrator, "_attempt_qc_fix_forward", lambda self, *a, **k: False)

    orch._run_task_with_redo(task, summary)

    assert calls["producer"] == 4, (
        f"distinct output must run the full retry budget; got {calls['producer']} "
        "(expected 4)"
    )
    assert task.status == TaskStatus.QC_REJECTED
