"""Pre-ship regression for cli_standards list (0.9.0 sweep).

Finding [LOW/race] cli_standards.py:36 — the `list` command double-scanned
the proposals dir (list_proposals + list_ids), two independent globs whose
results can drift under concurrent staging, so zip(ids, proposals) printed a
wrong id next to a body. The fix collapses the command to a single scan that
keeps each id paired with the proposal parsed from that same file.
"""

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
    """Each printed id must be the stem of the file the printed title came
    from — never a stem borrowed from a separate, possibly-drifted scan."""
    id_a = _stage("text", "Alpha rule")
    id_b = _stage("code", "Beta rule")

    result = CliRunner().invoke(
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
    """Simulate a concurrent stage between the two scans the OLD code did:
    poison list_ids so it returns a drifted (extra-leading) id list. The
    pre-fix zip(ids, proposals) would mislabel every body; the single-scan
    fix ignores list_ids entirely, so labels stay correct."""
    id_a = _stage("text", "Alpha rule")
    id_b = _stage("code", "Beta rule")

    # A proposal staged concurrently would make list_ids longer/reordered than
    # the list_proposals snapshot. If the command still consults list_ids, the
    # zip shifts and the wrong id prints next to each title.
    def _poisoned_list_ids(code: str) -> list[str]:
        return ["zzz-phantom-staged-after-the-proposals-snapshot", id_a, id_b]

    monkeypatch.setattr(standards_proposals, "list_ids", _poisoned_list_ids)

    result = CliRunner().invoke(
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
