# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Deterministic QC scaffolding gate (run-arc fix, 2026-07-02).

Run-1 gaming report: a draft opening with reply chatter + the
**Operation:**/**Definition of Done:** runbook block survived FIVE LLM QC
passes and shipped. Prose bends; the engine binds: a document-family draft
whose pre-heading head carries runbook markers is auto-rejected as a
``mechanical`` defect (QC-as-fixer edits it out) before any LLM opinion is
asked. Code-family artifacts are exempt — ``#`` comment lines look like
headings and legitimate code/docs may quote the runbook.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from modulatio import store, vault
from modulatio.orchestration import Orchestrator
from modulatio.types import Project, Task

LEAKED = (
    "I now have all the data I need. Let me write the corrected artifact.\n\n"
    "**Operation:** Produce Research Note\n"
    "**Definition of Done:** A concise research note.\n\n"
    "# Research Note: Pricing\n\nReal body content here.\n"
)


@pytest.fixture
def project(tmp_path, monkeypatch) -> Project:
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    vault.init_project("QSG", "Gate", "obj")
    return Project(
        code="QSG", name="Gate", objective="obj", leader_model="stub",
        wiki_path=str(tmp_path / "qsg"),
    )


def _orch(project: Project, qc_reply='{"passed": true, "check": "ok", "notes": ""}'):
    return Orchestrator(project, {
        "leader": lambda p: "", "planner": lambda p: "",
        "drafter": lambda p: "", "qc": lambda p: qc_reply,
        "researcher": lambda p: "",
    })


def _task(project: Project, *, kind="text", tid="QSG-T-001") -> Task:
    return Task(
        id=tid, project_id=project.id, goal_id="QSG-G-001",
        description="write a draft", artifact_kind=kind, qc_agent_id=None,
    )


def _draft(project: Project, body: str, name="draft.md") -> Path:
    artifacts = Path(project.wiki_path) / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    p = artifacts / name
    p.write_text(body)
    return p


def test_leaked_scaffold_is_rejected_before_the_llm_can_pass_it(project):
    """The QC runner says PASS; the engine gate must reject anyway —
    that is the whole point (five Sonnet passes missed it live)."""
    orch = _orch(project)  # qc stub passes everything
    task = _task(project)
    store.save_task(project.code, task)
    draft = _draft(project, LEAKED)

    verdict, notes, defect = orch._qc_review(task, draft, checksum="sha256:0")
    assert verdict.passed is False
    assert defect == "mechanical"  # QC-as-fixer routes to EDIT mode
    assert "scaffold" in (verdict.check or "").lower()
    assert "heading" in notes.lower()


def test_clean_document_draft_falls_through_to_llm_verdict(project):
    orch = _orch(project)
    task = _task(project, tid="QSG-T-002")
    store.save_task(project.code, task)
    draft = _draft(project, "# Clean Note\n\nSubstantive body.\n" * 3, "clean.md")

    verdict, _notes, _defect = orch._qc_review(task, draft, checksum="sha256:1")
    assert verdict.passed is True  # the stub LLM verdict decided


def test_code_family_artifact_is_exempt_from_the_gate(project):
    """A Python file's leading docstring mentioning Operation: followed by a
    '# section' comment line would false-match the document heuristic — the
    gate must not fire outside the document family."""
    orch = _orch(project)
    task = _task(project, kind="code", tid="QSG-T-003")
    store.save_task(project.code, task)
    body = (
        '"""Operation: batch runner."""\n'
        "# main entry\n"
        "def main():\n    pass\n"
    )
    draft = _draft(project, body, "runner.py")

    verdict, _notes, _defect = orch._qc_review(task, draft, checksum="sha256:2")
    assert verdict.passed is True
