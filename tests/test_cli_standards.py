"""Tests for the modulatio-standards CLI (slice #10)."""

from __future__ import annotations

import re
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


# ── approve: unsafe QC-emitted domains (resweep-r4 fold) ───────────────────


@pytest.mark.parametrize("bad_domain", ["slide deck", "data/viz", "design.doc"])
def test_approve_unsafe_domain_exits_clean_not_crash(project_vault, bad_domain):
    """r4 regression: a QC proposal carries domain = task.artifact_kind
    (planner free-text — 'slide deck', 'data/viz'); approve routes it through
    validate_registry_name, whose ValueError propagated UNCAUGHT and crashed
    the CLI. Must exit non-zero with a reported error, never a crash."""
    path = standards_proposals.save(
        standards_proposals.Proposal(
            domain=bad_domain,
            title="Some rule",
            rule_body="Body text.",
            evidence_refs=(),
        ),
        project_code=PROJECT_CODE,
    )
    pid = path.stem
    result = _runner().invoke(
        cli_standards.app, ["approve", "--code", PROJECT_CODE, pid]
    )
    # Clean failure, not a crash: non-zero exit AND no uncaught exception.
    assert result.exit_code != 0
    assert not isinstance(result.exception, ValueError)
    assert "unsafe domain" in result.output


def test_approve_safe_domain_still_succeeds(project_vault):
    """Guard against over-broad catching: a conforming domain still approves."""
    path = standards_proposals.save(
        standards_proposals.Proposal(
            domain="text",
            title="Clean rule",
            rule_body="Final output must be coherent.",
            evidence_refs=(),
        ),
        project_code=PROJECT_CODE,
    )
    pid = path.stem
    result = _runner().invoke(
        cli_standards.app, ["approve", "--code", PROJECT_CODE, pid]
    )
    assert result.exit_code == 0
    assert "approved" in result.output
    assert (project_vault / "standards" / "text.md").exists()
    assert not path.exists()


# ── list: id/proposal pairing under concurrent staging (preship fold) ──────


def _stage(domain: str, title: str) -> str:
    path = standards_proposals.save(
        standards_proposals.Proposal(
            domain=domain,
            title=title,
            rule_body=f"Body for {title}.",
            evidence_refs=(),
        ),
        project_code=PROJECT_CODE,
    )
    return path.stem


def _list_lines(stdout: str) -> list[tuple[str, str]]:
    """Parse the `  [id] (domain) title` lines into (id, title) pairs."""
    pairs: list[tuple[str, str]] = []
    for line in stdout.splitlines():
        m = re.match(r"\s*\[(?P<id>[^\]]+)\]\s*\([^)]*\)\s*(?P<title>.+)", line)
        if m:
            pairs.append((m.group("id"), m.group("title").strip()))
    return pairs


def test_list_keeps_id_paired_with_its_own_proposal(project_vault):
    """0.9.0-preship regression: `list` double-scanned the proposals dir (two
    independent globs that can drift under concurrent staging), so zip(ids,
    proposals) printed a wrong id next to a body. Single-scan keeps each id
    paired with the proposal parsed from that same file."""
    id_a = _stage("text", "Alpha rule")
    id_b = _stage("code", "Beta rule")

    result = _runner().invoke(
        cli_standards.app, ["list", "--code", PROJECT_CODE]
    )
    assert result.exit_code == 0
    pairs = _list_lines(result.stdout)
    assert len(pairs) == 2

    title_to_id = {title: pid for pid, title in pairs}
    assert title_to_id["Alpha rule"] == id_a
    assert title_to_id["Beta rule"] == id_b


def test_list_immune_to_drift_between_id_and_proposal_scans(
    project_vault, monkeypatch
):
    """Poison list_ids to simulate a concurrent stage between the OLD code's
    two scans — the pre-fix zip mislabeled every body; the single-scan fix
    ignores list_ids entirely, so labels stay correct."""
    id_a = _stage("text", "Alpha rule")
    id_b = _stage("code", "Beta rule")

    def _poisoned_list_ids(code: str) -> list[str]:
        return ["zzz-phantom-staged-after-the-proposals-snapshot", id_a, id_b]

    monkeypatch.setattr(standards_proposals, "list_ids", _poisoned_list_ids)

    result = _runner().invoke(
        cli_standards.app, ["list", "--code", PROJECT_CODE]
    )
    assert result.exit_code == 0
    pairs = _list_lines(result.stdout)
    title_to_id = {title: pid for pid, title in pairs}
    # Correct pairing survives the poisoned second scan.
    assert title_to_id["Alpha rule"] == id_a
    assert title_to_id["Beta rule"] == id_b
    # The phantom id never leaks into output.
    assert "zzz-phantom" not in result.stdout
