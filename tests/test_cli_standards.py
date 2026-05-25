"""Tests for the modulatio-standards CLI (slice #10)."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from modulatio import cli_standards, standards_proposals, vault


PROJECT_CODE = "TST"


@pytest.fixture
def project_vault(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    vault.init_project(PROJECT_CODE, "Test", "Test objective")
    return tmp_path / PROJECT_CODE.lower()


def _runner() -> CliRunner:
    return CliRunner()


# ── list ───────────────────────────────────────────────────────────────────

def test_list_empty_reports_nothing_pending(project_vault):
    result = _runner().invoke(cli_standards.app, ["list", "--code", PROJECT_CODE])
    assert result.exit_code == 0
    assert "No pending proposals" in result.stdout


def test_list_enumerates_pending_proposals(project_vault):
    standards_proposals.save(
        standards_proposals.Proposal(
            domain="text",
            title="A clear title",
            rule_body="Body.",
            evidence_refs=(),
            rationale="because reasons",
        ),
        project_code=PROJECT_CODE,
    )
    result = _runner().invoke(cli_standards.app, ["list", "--code", PROJECT_CODE])
    assert result.exit_code == 0
    assert "A clear title" in result.stdout
    assert "(text)" in result.stdout
    assert "because reasons" in result.stdout


# ── show ───────────────────────────────────────────────────────────────────

def test_show_prints_full_proposal_body(project_vault):
    path = standards_proposals.save(
        standards_proposals.Proposal(
            domain="code",
            title="Lint cleanly",
            rule_body="All code must pass the project's linter with no warnings.",
            evidence_refs=("v-1", "v-2"),
            rationale="3 rejections in a row",
        ),
        project_code=PROJECT_CODE,
    )
    pid = path.stem
    result = _runner().invoke(
        cli_standards.app, ["show", "--code", PROJECT_CODE, pid]
    )
    assert result.exit_code == 0
    assert "Lint cleanly" in result.stdout
    assert "All code must pass" in result.stdout
    assert "v-1" in result.stdout


def test_show_missing_proposal_exits_nonzero(project_vault):
    result = _runner().invoke(
        cli_standards.app, ["show", "--code", PROJECT_CODE, "nope"]
    )
    assert result.exit_code != 0


# ── approve ────────────────────────────────────────────────────────────────

def test_approve_moves_rule_to_project_standards(project_vault):
    path = standards_proposals.save(
        standards_proposals.Proposal(
            domain="text",
            title="No leaked scaffolds",
            rule_body="Final output must not include outlines.",
            evidence_refs=(),
        ),
        project_code=PROJECT_CODE,
    )
    pid = path.stem
    result = _runner().invoke(
        cli_standards.app, ["approve", "--code", PROJECT_CODE, pid]
    )
    assert result.exit_code == 0
    assert "approved" in result.stdout

    standards_file = project_vault / "standards" / "text.md"
    assert standards_file.exists()
    content = standards_file.read_text()
    assert "No leaked scaffolds" in content
    assert "Team-approved rules" in content
    # Proposal file consumed.
    assert not path.exists()


def test_approve_missing_id_exits_nonzero(project_vault):
    result = _runner().invoke(
        cli_standards.app, ["approve", "--code", PROJECT_CODE, "ghost"]
    )
    assert result.exit_code != 0


# ── reject ─────────────────────────────────────────────────────────────────

def test_reject_deletes_proposal_without_writing_standards(project_vault):
    path = standards_proposals.save(
        standards_proposals.Proposal(
            domain="text",
            title="Rejected rule",
            rule_body="x",
            evidence_refs=(),
        ),
        project_code=PROJECT_CODE,
    )
    pid = path.stem
    result = _runner().invoke(
        cli_standards.app, ["reject", "--code", PROJECT_CODE, pid]
    )
    assert result.exit_code == 0
    assert "rejected" in result.stdout
    assert not path.exists()
    assert not (project_vault / "standards" / "text.md").exists()


def test_reject_missing_id_exits_nonzero(project_vault):
    result = _runner().invoke(
        cli_standards.app, ["reject", "--code", PROJECT_CODE, "ghost"]
    )
    assert result.exit_code != 0
