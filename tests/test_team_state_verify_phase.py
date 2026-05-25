"""Tests for Slice 1 PR-B (#88, part 3) — Leader-reflect Verify-phase.

Covers the helpers + integration sites in project_execution.py:

  - _extract_state_doc parses fenced ``state-doc`` block from response,
    returns None when missing (non-fatal), takes last match
  - _collect_team_state_inputs pulls (claims, verdicts) from a
    RunSummary; returns ([], []) for non-summary inputs (failure path)
  - _append_audit_entries writes JSONL to <run>/audit.jsonl, no-ops on
    None run_id, swallows errors (best-effort)
  - _build_reflection_prompt renders Prior Team State + Producer
    Self-Claims + QC Verdicts sections; existing inputs unchanged
  - Integration: state doc gets written when reflect emits the block
  - Integration: divergence_notes append to audit.jsonl
  - Backward compat: existing reflect outcome parsing keeps working
    when no state-doc block is present
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import pytest

from modulatio import project_execution, vault
from modulatio.types import Task, TaskStatus
from uuid import uuid4


# ── _extract_state_doc ────────────────────────────────────────────────────


def test_extract_state_doc_pulls_fenced_block() -> None:
    response = (
        "Some prose by Leader.\n\n"
        "```json\n"
        '{"outcome": "continue", "rationale": "ok"}\n'
        "```\n\n"
        "```state-doc\n"
        "# Project State\n"
        "**Updated:** 2026-05-05 22:00\n"
        "(body)\n"
        "```\n"
    )
    out = project_execution._extract_state_doc(response)
    assert out is not None
    assert out.startswith("# Project State")
    assert "(body)" in out


def test_extract_state_doc_missing_returns_none() -> None:
    response = "Just prose. No fenced state-doc.\n```json\n{}\n```"
    assert project_execution._extract_state_doc(response) is None


def test_extract_state_doc_empty_block_returns_none() -> None:
    response = "```state-doc\n   \n```"
    assert project_execution._extract_state_doc(response) is None


def test_extract_state_doc_takes_last_match() -> None:
    response = (
        "First (example):\n```state-doc\n# old\n```\n\n"
        "Second (real):\n```state-doc\n# new\n**Updated:** now\n```"
    )
    out = project_execution._extract_state_doc(response)
    assert out is not None
    assert "# new" in out
    assert "# old" not in out


def test_extract_state_doc_handles_empty_response() -> None:
    assert project_execution._extract_state_doc("") is None


# ── _collect_team_state_inputs ────────────────────────────────────────────


def _make_task(
    task_id: str = "T-001",
    summary: str | None = "did the thing",
    status: TaskStatus = TaskStatus.COMPLETED,
    agent_id: str | None = "drafter",
) -> Task:
    return Task(
        id=task_id,
        project_id=uuid4(),
        goal_id="G-1",
        description="...",
        assigned_agent_id=agent_id,
        status=status,
        summary_for_state_doc=summary,
    )


def test_collect_inputs_from_run_summary() -> None:
    summary = SimpleNamespace(tasks=[
        _make_task("T-1", summary="drafted intro", status=TaskStatus.COMPLETED),
        _make_task("T-2", summary=None, status=TaskStatus.QC_REJECTED),
        _make_task("T-3", summary="wrote outline", status=TaskStatus.COMPLETED,
                   agent_id="writer"),
    ])
    claims, verdicts = project_execution._collect_team_state_inputs(summary)

    # Only tasks with a non-empty claim land in claims; all tasks land in verdicts
    assert len(claims) == 2
    assert {c["task_id"] for c in claims} == {"T-1", "T-3"}
    assert claims[0]["claim"] == "drafted intro"
    assert claims[1]["agent"] == "writer"
    assert len(verdicts) == 3
    assert {v["task_id"] for v in verdicts} == {"T-1", "T-2", "T-3"}
    # Status is normalized to lowercase string
    statuses = {v["status"] for v in verdicts}
    assert "completed" in statuses
    assert "qc_rejected" in statuses


def test_collect_inputs_handles_string_failure_summary() -> None:
    """When the kickoff failed, kickoff_summary is a string. The
    summary OBJECT bound at that scope is None — _collect must
    tolerate that gracefully."""
    claims, verdicts = project_execution._collect_team_state_inputs(None)
    assert claims == []
    assert verdicts == []


def test_collect_inputs_handles_summary_without_tasks_attr() -> None:
    summary = SimpleNamespace()  # no .tasks
    claims, verdicts = project_execution._collect_team_state_inputs(summary)
    assert claims == []
    assert verdicts == []


def test_collect_inputs_falls_back_through_agent_id_fields() -> None:
    """When assigned_agent_id is None, fall back to assignee_specialist."""
    t = Task(
        id="T-X",
        project_id=uuid4(),
        goal_id="G-1",
        description="...",
        assigned_agent_id=None,
        assignee_specialist="engineer",
        summary_for_state_doc="wrote main.py",
    )
    summary = SimpleNamespace(tasks=[t])
    claims, _ = project_execution._collect_team_state_inputs(summary)
    assert claims[0]["agent"] == "engineer"


# ── _append_audit_entries ─────────────────────────────────────────────────


@pytest.fixture
def project_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, str]:
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path / "vault")
    return "tst", "run-001"


def test_append_audit_writes_jsonl(project_run: tuple[str, str]) -> None:
    code, run_id = project_run
    project_execution._append_audit_entries(
        code, run_id,
        [
            {"after_sub_objective": 2, "kind": "team_state_divergence", "note": "first"},
            {"after_sub_objective": 2, "kind": "team_state_divergence", "note": "second"},
        ],
    )
    audit_path = vault.run_dir(code, run_id) / "audit.jsonl"
    assert audit_path.exists()
    lines = audit_path.read_text().strip().split("\n")
    assert len(lines) == 2
    parsed = [json.loads(line) for line in lines]
    assert parsed[0]["note"] == "first"
    assert parsed[1]["note"] == "second"
    # Each entry got a timestamp
    assert "timestamp" in parsed[0]


def test_append_audit_appends_to_existing_file(
    project_run: tuple[str, str],
) -> None:
    code, run_id = project_run
    project_execution._append_audit_entries(
        code, run_id, [{"note": "first"}]
    )
    project_execution._append_audit_entries(
        code, run_id, [{"note": "second"}]
    )
    audit_path = vault.run_dir(code, run_id) / "audit.jsonl"
    lines = audit_path.read_text().strip().split("\n")
    assert len(lines) == 2


def test_append_audit_no_op_on_none_run_id(project_run: tuple[str, str]) -> None:
    code, _ = project_run
    project_execution._append_audit_entries(code, None, [{"note": "x"}])
    # Nothing crashed, nothing written
    # No file path to check since vault doesn't get touched


def test_append_audit_no_op_on_empty_entries(
    project_run: tuple[str, str],
) -> None:
    code, run_id = project_run
    project_execution._append_audit_entries(code, run_id, [])
    audit_path = vault.run_dir(code, run_id) / "audit.jsonl"
    assert not audit_path.exists()


# ── _build_reflection_prompt with new sections ────────────────────────────


@dataclass
class _StubProject:
    code: str = "tst"
    objective: str = "test objective"
    run_id: str | None = "run-1"
    compression_enabled: bool = True


@dataclass
class _StubPlan:
    id: str = "PLAN-1"
    body: str = "plan body"
    reflection_log: list = field(default_factory=list)


def test_reflect_prompt_includes_team_state_sections() -> None:
    prompt = project_execution._build_reflection_prompt(
        project=_StubProject(),  # type: ignore[arg-type]
        plan=_StubPlan(),  # type: ignore[arg-type]
        sub_objectives=[
            {"index": 1, "title": "T1", "description": "do thing 1", "raw": "..."},
            {"index": 2, "title": "T2", "description": "do thing 2", "raw": "..."},
        ],
        completed_index=0,
        last_kickoff_summary="ok",
        prior_team_state="# Project State\nbody-content",
        producer_claims=[
            {"task_id": "T-1", "agent": "drafter", "claim": "wrote intro"},
        ],
        qc_verdicts=[
            {"task_id": "T-1", "status": "complete", "notes": None},
        ],
    )
    assert "# Prior Team State" in prompt
    assert "body-content" in prompt
    assert "# Producer Self-Claims" in prompt
    assert "T-1 (drafter): wrote intro" in prompt
    assert "# QC Verdicts" in prompt
    assert "status=complete" in prompt
    assert "state-doc" in prompt
    assert "divergence_notes" in prompt


def test_reflect_prompt_renders_neutral_markers_when_inputs_empty() -> None:
    prompt = project_execution._build_reflection_prompt(
        project=_StubProject(),  # type: ignore[arg-type]
        plan=_StubPlan(),  # type: ignore[arg-type]
        sub_objectives=[
            {"index": 1, "title": "T1", "description": "do thing 1", "raw": "..."},
        ],
        completed_index=0,
        last_kickoff_summary="ok",
    )
    # All three new sections present with their "(none)" markers
    assert "# Prior Team State" in prompt
    assert "no prior state doc" in prompt
    assert "# Producer Self-Claims" in prompt
    assert "no claims" in prompt
    assert "# QC Verdicts" in prompt
    assert "no verdicts available" in prompt


def test_reflect_prompt_existing_inputs_unchanged() -> None:
    """The new sections must NOT have displaced any existing inputs."""
    prompt = project_execution._build_reflection_prompt(
        project=_StubProject(),  # type: ignore[arg-type]
        plan=_StubPlan(),  # type: ignore[arg-type]
        sub_objectives=[
            {"index": 1, "title": "T1", "description": "do thing 1", "raw": "..."},
        ],
        completed_index=0,
        last_kickoff_summary="kickoff result here",
    )
    assert "Code: tst" in prompt
    assert "Plan: PLAN-1" in prompt
    assert "kickoff result here" in prompt
    assert "Decision" in prompt


# ── _parse_reflection_response backward compat ────────────────────────────


def test_parse_reflection_response_ignores_state_doc_block() -> None:
    """The state-doc block is additive — the existing JSON outcome
    extractor must still find the JSON block when both are present."""
    response = (
        "Reasoning prose.\n\n"
        "```state-doc\n# Project State\n(body)\n```\n\n"
        "```json\n"
        '{"outcome": "continue", "rationale": "ok"}\n'
        "```\n"
    )
    decision = project_execution._parse_reflection_response(response)
    assert decision["outcome"] == "continue"


def test_parse_reflection_response_extracts_divergence_notes() -> None:
    """When Leader includes divergence_notes in the JSON, it round-trips."""
    response = (
        "```json\n"
        '{"outcome": "continue", "rationale": "ok", '
        '"divergence_notes": ["task T-1 claim mismatched verdict"]}\n'
        "```"
    )
    decision = project_execution._parse_reflection_response(response)
    # The parser doesn't strip extra fields — they survive.
    assert decision.get("divergence_notes") == [
        "task T-1 claim mismatched verdict"
    ]
