"""Tests for the ``modulatio-memory`` CLI — review surface for QC's
proposed team-memory entries.

Mirrors ``test_cli_standards.py``. The CLI is the human review path
between QC's `proposed_team_memory` field and the actual write into
the team-memory pool.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from modulatio import cli_memory, vault
from modulatio.memory import team_memory


PROJECT_CODE = "MEM"


@pytest.fixture
def isolated_vault(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path / "projects")
    vault.init_project(PROJECT_CODE, "Memory CLI fixture", "obj")
    return tmp_path


def _stage_proposal(body: str = "test fact for the team", **kwargs) -> str:
    """Stage a pending proposal and return its id."""
    p = team_memory.propose(
        proposer_id=kwargs.pop("proposer_id", "engineer"),
        body=body,
        project_code=PROJECT_CODE,
        skill_tags=kwargs.pop("skill_tags", ("coding",)),
        artifact_kind=kwargs.pop("artifact_kind", "code"),
        capability_tags=kwargs.pop("capability_tags", ("python-coding",)),
        rationale=kwargs.pop("rationale", "applies to all coding tasks"),
    )
    return p.proposal_id


# ── list ──────────────────────────────────────────────────────────────────


def test_list_empty_project_reports_no_proposals(isolated_vault):
    runner = CliRunner()
    result = runner.invoke(cli_memory.app, ["list", "--code", PROJECT_CODE])
    assert result.exit_code == 0
    assert "No pending" in result.output
    assert PROJECT_CODE in result.output


def test_list_shows_pending_proposals(isolated_vault):
    pid = _stage_proposal(body="prefer POST /api/v2 for new endpoints")
    runner = CliRunner()
    result = runner.invoke(cli_memory.app, ["list", "--code", PROJECT_CODE])
    assert result.exit_code == 0
    assert pid in result.output
    assert "POST /api/v2" in result.output  # body snippet visible
    assert "engineer" in result.output  # proposer surfaced
    assert "applies to all coding tasks" in result.output  # rationale


def test_list_truncates_long_body_in_summary(isolated_vault):
    """Long bodies get a 60-char snippet with ellipsis. Full body is
    visible via ``show``."""
    long_body = "X" * 200
    _stage_proposal(body=long_body)
    runner = CliRunner()
    result = runner.invoke(cli_memory.app, ["list", "--code", PROJECT_CODE])
    assert result.exit_code == 0
    assert "…" in result.output  # truncation marker
    # Full 200 X's not shown in list output
    assert "X" * 200 not in result.output


# ── show ──────────────────────────────────────────────────────────────────


def test_show_prints_full_body_and_metadata(isolated_vault):
    pid = _stage_proposal(
        body="this is the full body of the proposal\nwith multiple lines",
        proposer_id="qc",
        rationale="recurring pattern across 3 prior verdicts",
    )
    runner = CliRunner()
    result = runner.invoke(
        cli_memory.app, ["show", "--code", PROJECT_CODE, pid]
    )
    assert result.exit_code == 0
    assert pid in result.output
    assert "qc" in result.output
    assert "recurring pattern" in result.output
    assert "this is the full body of the proposal" in result.output
    assert "with multiple lines" in result.output


def test_show_unknown_proposal_returns_error(isolated_vault):
    runner = CliRunner()
    result = runner.invoke(
        cli_memory.app, ["show", "--code", PROJECT_CODE, "nonexistent"]
    )
    assert result.exit_code != 0
    assert "not found" in result.output.lower()


# ── approve ───────────────────────────────────────────────────────────────


def test_approve_writes_to_team_memory_and_removes_proposal(isolated_vault):
    pid = _stage_proposal(body="canonical fact about the team")
    # Pre-condition: proposal pending, no team-memory entries yet.
    assert team_memory.list_entries(PROJECT_CODE) == []
    assert len(team_memory.list_proposals(PROJECT_CODE)) == 1

    runner = CliRunner()
    result = runner.invoke(
        cli_memory.app, ["approve", "--code", PROJECT_CODE, pid]
    )
    assert result.exit_code == 0, result.output
    assert "approved" in result.output

    # Post: proposal consumed, team-memory has the entry.
    assert team_memory.list_proposals(PROJECT_CODE) == []
    entries = team_memory.list_entries(PROJECT_CODE)
    assert len(entries) == 1
    assert "canonical fact about the team" in entries[0].body
    # Proposer attribution preserved.
    assert entries[0].proposed_by == "engineer"


def test_approve_default_writer_is_human_admin_leader_tier(isolated_vault):
    """Default --approver-tier=leader satisfies the ACL on team_memory.write."""
    pid = _stage_proposal()
    runner = CliRunner()
    result = runner.invoke(
        cli_memory.app, ["approve", "--code", PROJECT_CODE, pid]
    )
    assert result.exit_code == 0
    entries = team_memory.list_entries(PROJECT_CODE)
    assert entries[0].writer_id == "human-admin"
    assert entries[0].writer_tier == "leader"


def test_approve_explicit_approver_overrides_default(isolated_vault):
    pid = _stage_proposal()
    runner = CliRunner()
    result = runner.invoke(cli_memory.app, [
        "approve", "--code", PROJECT_CODE,
        "--approver-id", "qc-night-shift",
        "--approver-tier", "qc",
        pid,
    ])
    assert result.exit_code == 0
    entries = team_memory.list_entries(PROJECT_CODE)
    assert entries[0].writer_id == "qc-night-shift"
    assert entries[0].writer_tier == "qc"


def test_approve_rejects_non_writer_tier(isolated_vault):
    """ACL: producer / coordinator cannot approve. Must be qc or leader."""
    pid = _stage_proposal()
    runner = CliRunner()
    result = runner.invoke(cli_memory.app, [
        "approve", "--code", PROJECT_CODE,
        "--approver-tier", "producer",
        pid,
    ])
    assert result.exit_code != 0
    assert "denied" in result.output.lower() or "permission" in result.output.lower()
    # Proposal still pending; no write happened.
    assert team_memory.list_proposals(PROJECT_CODE)
    assert team_memory.list_entries(PROJECT_CODE) == []


def test_approve_unknown_proposal_returns_error(isolated_vault):
    runner = CliRunner()
    result = runner.invoke(
        cli_memory.app, ["approve", "--code", PROJECT_CODE, "ghost-id"]
    )
    assert result.exit_code != 0


# ── reject ────────────────────────────────────────────────────────────────


def test_reject_removes_proposal_without_write(isolated_vault):
    pid = _stage_proposal()
    runner = CliRunner()
    result = runner.invoke(
        cli_memory.app, ["reject", "--code", PROJECT_CODE, pid]
    )
    assert result.exit_code == 0
    assert "rejected" in result.output

    # Proposal gone, team memory unchanged.
    assert team_memory.list_proposals(PROJECT_CODE) == []
    assert team_memory.list_entries(PROJECT_CODE) == []


def test_reject_unknown_proposal_returns_error(isolated_vault):
    runner = CliRunner()
    result = runner.invoke(
        cli_memory.app, ["reject", "--code", PROJECT_CODE, "ghost"]
    )
    assert result.exit_code != 0
    assert "not found" in result.output.lower()
