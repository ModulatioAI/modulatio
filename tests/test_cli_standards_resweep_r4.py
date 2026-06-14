"""Re-sweep r4 regressions for the modulatio-standards CLI.

Finding 1 [MEDIUM/error-path] cli_standards.approve_cmd:
A QC-emitted proposal carries ``domain = task.artifact_kind`` (planner
free-text), which save() persists raw. ``artifact_kind`` can contain
spaces / slashes / dots ('slide deck', 'data/viz', 'design doc'). On
approve, ``standards_proposals.approve`` routes the domain through
``validate_registry_name`` (it becomes a file name) which raises
``ValueError`` on such input. ``approve_cmd`` only caught
FileNotFoundError, so the ValueError propagated UNCAUGHT and crashed the
CLI. The fix catches ValueError too and reports a clean, non-zero error.
"""

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


@pytest.mark.parametrize("bad_domain", ["slide deck", "data/viz", "design.doc"])
def test_approve_unsafe_domain_exits_clean_not_crash(project_vault, bad_domain):
    """A multi-word / separator-bearing domain must NOT crash the CLI with an
    uncaught ValueError; it must exit non-zero with a reported error."""
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
