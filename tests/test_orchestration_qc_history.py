"""Orchestrator ↔ qc_history integration tests (slice #8.1).

Writes are always-on (a local markdown append is cheap and the log is
load-bearing for #10's standards-via-QC write-side later). Retrieval
requires an ``Embedder`` kwarg on the Orchestrator; absence leaves the
QC prompt's ``{history}`` slot at a neutral "(no prior QC precedent)"
marker.

These tests exercise the loop end-to-end with stub runners that capture
the prompts they receive, so we can assert both the append-on-verdict
shape and the retrieval-into-prompt shape.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from modulatio import config, qc_history, vault  # noqa: F401
from modulatio.orchestration import Orchestrator
from modulatio.semantic_router import StubEmbedder
from modulatio.types import Project


def _write_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


PROJECT_CODE = "TST"


@pytest.fixture
def project(tmp_path: Path, monkeypatch) -> Project:
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    # qc_history's cache root is config-driven.
    monkeypatch.setattr(config, "get_cache_root", lambda: tmp_path / "_cache")
    vault.init_project(PROJECT_CODE, "QC history stub", "One essay")
    return Project(
        code=PROJECT_CODE,
        name="QC history stub",
        objective="One essay",
        leader_model="stub",
        wiki_path=str(tmp_path / PROJECT_CODE.lower()),
    )


def _leader_one_goal(prompt: str) -> str:
    if "LEADER GOAL VERIFICATION" in prompt:
        payload = {
            "verdict": "satisfied",
            "rationale": "ok",
            "report_body": "## Report\n\nok.\n",
        }
        return f"```json\n{json.dumps(payload)}\n```"
    goals = [{
        "description": "Draft one essay",
        "success_criteria": "one QC-passed essay",
        "evidence_required": [
            {"kind": "artifact", "description": "essay file exists"},
        ],
    }]
    return f"```json\n{json.dumps(goals)}\n```"


def _coordinator_one_task(prompt: str) -> str:
    tasks = [{
        "description": "Draft essay 1",
        "assignee_specialist": "drafter",
        "artifact_kind": "essay",
        "evidence_required": [
            {"kind": "artifact", "description": "essay 1 file exists"},
        ],
    }]
    return f"```json\n{json.dumps(tasks)}\n```"


def _drafter_stub(prompt: str) -> str:
    return "---\ntitle: t\n---\n\n" + " ".join(["word"] * 250) + "\n"


def _qc_pass(prompt: str) -> str:
    return (
        '```json\n'
        '{"check": "ok", "passed": true, "notes": "", "defect_type": null}\n'
        '```'
    )


def _qc_fail_substantive(prompt: str) -> str:
    return (
        '```json\n'
        '{"check": "conformance miss", "passed": false, '
        '"notes": "Missing the required thesis.", '
        '"defect_type": "substantive"}\n'
        '```'
    )


# ── Write side ────────────────────────────────────────────────────────────

def test_orchestrator_appends_qc_history_entry_on_pass(project: Project):
    """After a task's QC passes, qc-history gains one entry for its
    artifact_kind domain. Both pass and fail verdicts are logged — the
    read-side learns from both (pass history is also evidence)."""
    runners = {
        "leader": _leader_one_goal,
        "planner": _coordinator_one_task,
        "drafter": _drafter_stub,
        "qc": _qc_pass,
    }
    orch = Orchestrator(project, runners)
    orch.kickoff("one essay")

    entries = qc_history.load_verdicts("essay", project.code)
    assert len(entries) == 1
    assert entries[0].verdict == "pass"
    assert entries[0].defect_type is None
    assert entries[0].task_id  # populated
    assert entries[0].artifact_body  # captured the draft body


def test_orchestrator_appends_qc_history_entry_on_fail(project: Project):
    """Failed verdicts land in history too, carrying the defect_type
    classification so later retrieval can condition on it."""
    runners = {
        "leader": _leader_one_goal,
        "planner": _coordinator_one_task,
        "drafter": _drafter_stub,
        "qc": _qc_fail_substantive,
    }
    orch = Orchestrator(project, runners)
    orch.kickoff("one essay")

    entries = qc_history.load_verdicts("essay", project.code)
    # One entry per QC call — redo loop fires QC up to (max_retries+1) times.
    assert len(entries) >= 1
    assert all(e.verdict == "fail" for e in entries)
    assert all(e.defect_type == "substantive" for e in entries)


def test_qc_history_entries_record_agent_ids(project: Project):
    """Producer and QC agent ids are captured on each record so later
    retrieval can inspect 'who has been passing/failing on this pattern'."""
    runners = {
        "leader": _leader_one_goal,
        "planner": _coordinator_one_task,
        "drafter": _drafter_stub,
        "qc": _qc_pass,
    }
    orch = Orchestrator(project, runners)
    orch.kickoff("one essay")

    entries = qc_history.load_verdicts("essay", project.code)
    assert entries[0].producer_agent  # non-empty string
    assert entries[0].qc_agent


# ── Read side ─────────────────────────────────────────────────────────────

def test_qc_prompt_carries_neutral_marker_when_no_embedder(project: Project):
    """Without a ``qc_history_embedder`` kwarg, retrieval is skipped and
    the QC prompt still renders with a stable ``(no prior QC precedent)``
    placeholder — the slot must always be present, never KeyError out."""
    captured: list[str] = []

    def _qc_capture(prompt: str) -> str:
        captured.append(prompt)
        return _qc_pass(prompt)

    runners = {
        "leader": _leader_one_goal,
        "planner": _coordinator_one_task,
        "drafter": _drafter_stub,
        "qc": _qc_capture,
    }
    orch = Orchestrator(project, runners)
    orch.kickoff("one essay")

    assert len(captured) == 1
    assert "PRIOR QC PRECEDENT" in captured[0]
    assert "no prior QC precedent" in captured[0]


def test_qc_prompt_includes_similar_prior_verdicts_when_embedder_set(project: Project):
    """With a stub embedder and prior qc-history present, the QC prompt
    for a new task carries the top-K most-similar prior verdict
    rationales. This is the retrieval → precedent pathway that #8.1
    unlocks."""
    # Seed one prior verdict directly into qc-history for the essay domain.
    qc_history.append_verdict(
        "essay",
        project.code,
        qc_history.VerdictRecord(
            entry_id="prior-1",
            timestamp="2026-04-20T10:00:00Z",
            task_id="TST-T-prior",
            producer_agent="drafter",
            qc_agent="kimi",
            verdict="fail",
            defect_type="substantive",
            rationale="Previous draft scaffolded its plan instead of delivering.",
            artifact_body=" ".join(["word"] * 250),
        ),
    )

    captured: list[str] = []

    def _qc_capture(prompt: str) -> str:
        captured.append(prompt)
        return _qc_pass(prompt)

    runners = {
        "leader": _leader_one_goal,
        "planner": _coordinator_one_task,
        "drafter": _drafter_stub,
        "qc": _qc_capture,
    }
    orch = Orchestrator(
        project,
        runners,
        qc_history_embedder=StubEmbedder(dim=16),
    )
    orch.kickoff("one essay")

    assert len(captured) == 1
    prompt = captured[0]
    assert "PRIOR QC PRECEDENT" in prompt
    assert "Previous draft scaffolded its plan" in prompt
    assert "no prior QC precedent" not in prompt
    # Slice #12b: entry_id is surfaced so QC can cite specific prior
    # verdicts in proposed_standard.evidence_refs (slice #10 audit trail).
    assert "entry_id=prior-1" in prompt


def test_qc_prompt_history_slot_is_domain_scoped(project: Project):
    """Essay QC only sees essay precedent. Code-domain prior verdicts
    must not leak into the essay prompt."""
    qc_history.append_verdict(
        "code",
        project.code,
        qc_history.VerdictRecord(
            entry_id="code-prior",
            timestamp="2026-04-20T10:00:00Z",
            task_id="TST-T-code",
            producer_agent="drafter",
            qc_agent="kimi",
            verdict="fail",
            defect_type="mechanical",
            rationale="Syntax error in fenced Python block.",
            artifact_body="def broken(:",
        ),
    )

    captured: list[str] = []

    def _qc_capture(prompt: str) -> str:
        captured.append(prompt)
        return _qc_pass(prompt)

    runners = {
        "leader": _leader_one_goal,
        "planner": _coordinator_one_task,
        "drafter": _drafter_stub,
        "qc": _qc_capture,
    }
    orch = Orchestrator(
        project,
        runners,
        qc_history_embedder=StubEmbedder(dim=16),
    )
    orch.kickoff("one essay")

    assert len(captured) == 1
    assert "Syntax error in fenced Python block" not in captured[0]
    # The neutral marker appears instead — essay domain has no precedent.
    assert "no prior QC precedent" in captured[0]


# ── Human training notes — two slots (slice #8.2) ─────────────────────────

def test_qc_prompt_carries_neutral_markers_for_both_notes_when_absent(project: Project):
    """Two notes slots always render — neutral markers when the standing
    file is missing and no one-shot flag was passed. QC's prompt template
    must never KeyError on empty state."""
    captured: list[str] = []

    def _qc_capture(prompt: str) -> str:
        captured.append(prompt)
        return _qc_pass(prompt)

    runners = {
        "leader": _leader_one_goal,
        "planner": _coordinator_one_task,
        "drafter": _drafter_stub,
        "qc": _qc_capture,
    }
    orch = Orchestrator(project, runners)
    orch.kickoff("one essay")

    prompt = captured[0]
    assert "HUMAN TRAINING NOTES (standing" in prompt
    assert "HUMAN TRAINING NOTES (this run" in prompt
    assert "no standing training notes" in prompt
    assert "no one-shot training notes" in prompt


def test_qc_prompt_includes_standing_notes_when_file_present(project: Project):
    """Human-curated guidance at <project>/qc-notes/<domain>.md lands
    in the standing-notes slot verbatim so QC reads the human's own
    words, not a summary."""
    _write_file(
        vault.VAULT_ROOT / "tst" / "qc-notes" / "essay.md",
        "Reject any draft that uses the phrase 'In conclusion'.\n",
    )
    captured: list[str] = []

    def _qc_capture(prompt: str) -> str:
        captured.append(prompt)
        return _qc_pass(prompt)

    runners = {
        "leader": _leader_one_goal,
        "planner": _coordinator_one_task,
        "drafter": _drafter_stub,
        "qc": _qc_capture,
    }
    orch = Orchestrator(project, runners)
    orch.kickoff("one essay")

    prompt = captured[0]
    assert "Reject any draft that uses the phrase 'In conclusion'" in prompt
    assert "no standing training notes" not in prompt


def test_qc_prompt_includes_one_shot_notes_from_kwarg(project: Project):
    """Session-level guidance passed via Orchestrator kwarg (mirrors the
    CLI's ``--qc-notes`` flag) lands in the one-shot slot, separate
    from standing notes."""
    captured: list[str] = []

    def _qc_capture(prompt: str) -> str:
        captured.append(prompt)
        return _qc_pass(prompt)

    runners = {
        "leader": _leader_one_goal,
        "planner": _coordinator_one_task,
        "drafter": _drafter_stub,
        "qc": _qc_capture,
    }
    orch = Orchestrator(
        project,
        runners,
        qc_one_shot_notes="For this run, lean strict on conformance.",
    )
    orch.kickoff("one essay")

    prompt = captured[0]
    assert "For this run, lean strict on conformance" in prompt
    assert "no one-shot training notes" not in prompt


def test_qc_prompt_separates_standing_and_one_shot_notes(project: Project):
    """Both slots can be populated at once. They render in distinct
    labeled sections so QC reasons about team-level vs run-level
    guidance separately, not as one merged blob."""
    _write_file(
        vault.VAULT_ROOT / "tst" / "qc-notes" / "essay.md",
        "Standing rule: prefer concrete over abstract claims.\n",
    )
    captured: list[str] = []

    def _qc_capture(prompt: str) -> str:
        captured.append(prompt)
        return _qc_pass(prompt)

    runners = {
        "leader": _leader_one_goal,
        "planner": _coordinator_one_task,
        "drafter": _drafter_stub,
        "qc": _qc_capture,
    }
    orch = Orchestrator(
        project,
        runners,
        qc_one_shot_notes="This run only: accept shorter essays.",
    )
    orch.kickoff("one essay")

    prompt = captured[0]
    assert "prefer concrete over abstract claims" in prompt
    assert "accept shorter essays" in prompt
    # Standing section appears before the one-shot section so QC reads
    # the stable baseline first.
    standing_idx = prompt.find("HUMAN TRAINING NOTES (standing")
    one_shot_idx = prompt.find("HUMAN TRAINING NOTES (this run")
    assert standing_idx >= 0 and one_shot_idx >= 0
    assert standing_idx < one_shot_idx


def test_qc_prompt_standing_notes_are_domain_scoped(project: Project):
    """Code-domain standing notes must not appear on an essay QC call."""
    _write_file(
        vault.VAULT_ROOT / "tst" / "qc-notes" / "code.md",
        "Always require docstrings on public functions.\n",
    )
    captured: list[str] = []

    def _qc_capture(prompt: str) -> str:
        captured.append(prompt)
        return _qc_pass(prompt)

    runners = {
        "leader": _leader_one_goal,
        "planner": _coordinator_one_task,
        "drafter": _drafter_stub,
        "qc": _qc_capture,
    }
    orch = Orchestrator(project, runners)
    orch.kickoff("one essay")

    prompt = captured[0]
    assert "docstrings on public functions" not in prompt
    assert "no standing training notes" in prompt
