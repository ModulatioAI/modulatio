# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Re-sweep regressions for standards_proposals encoding handling.

A standards proposal is DURABLE, human/team-authored POLICY text that
``approve()`` appends verbatim into the project standards — it is NOT a
rebuildable cache. The round-4 fix wrongly used ``errors="replace"`` to keep
the review surface from crashing on a corrupt byte, but that left a corrupt
proposal listable + approvable as mojibake (U+FFFD grafted into standards).

The corrected contract (Nemo, 0.9.0 cadre review):
- ``_parse_file`` decodes STRICTLY → raises UnicodeDecodeError on a bad byte.
- ``list_proposals`` SKIPS a corrupt file (the surface still doesn't crash, and
  the bad file is excluded rather than surfaced as approvable mojibake).
- ``load`` / ``approve`` of a corrupt proposal fails closed (raises) — corrupt
  policy text can never be grafted into standards.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from modulatio import standards_proposals, vault


PROJECT_CODE = "TST"


@pytest.fixture
def project_vault(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    vault.init_project(PROJECT_CODE, "Test", "Test objective")
    return tmp_path / PROJECT_CODE.lower()


def _write_corrupt_proposal(project_code: str, stem: str) -> Path:
    """Stage a proposal `.md` whose frontmatter contains a raw non-UTF-8 byte."""
    root = standards_proposals._proposals_dir(project_code)
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{stem}.md"
    path.write_bytes(
        b"---\n"
        b"domain: text\n"
        b"title: caf\xe9 rule\n"  # 0xe9 is a lone non-UTF-8 byte here
        b"evidence_refs: q1\n"
        b"rationale: keep accents\n"
        b"---\n\n"
        b"Body of the rule.\n"
    )
    return path


def test_parse_file_decodes_strictly_and_raises_on_corrupt_byte(project_vault):
    """STRICT decode: a corrupt byte must RAISE, not decode-with-replacement —
    so corrupt policy text can never become approvable mojibake."""
    _write_corrupt_proposal(PROJECT_CODE, "20260101T000000Z__cafe-rule")
    path = standards_proposals._proposals_dir(PROJECT_CODE) / "20260101T000000Z__cafe-rule.md"
    with pytest.raises(UnicodeDecodeError):
        standards_proposals._parse_file(path)


def test_list_proposals_skips_corrupt_file_without_crashing(project_vault):
    """One corrupt proposal must NOT crash list_proposals AND must NOT be
    surfaced — only the clean proposal(s) come back."""
    standards_proposals.save(
        standards_proposals.Proposal(
            domain="code", title="Clean rule", rule_body="Use type hints."
        ),
        PROJECT_CODE,
    )
    _write_corrupt_proposal(PROJECT_CODE, "20260101T000000Z__corrupt")
    proposals = standards_proposals.list_proposals(PROJECT_CODE)  # must NOT raise
    titles = {p.title for p in proposals}
    assert titles == {"Clean rule"}, "corrupt proposal must be skipped, not surfaced"
    assert len(proposals) == 1


def test_load_corrupt_proposal_fails_closed(project_vault):
    """`load` (used by show/approve) of a corrupt proposal fails closed — it is
    not loadable as mojibake."""
    _write_corrupt_proposal(PROJECT_CODE, "20260101T000000Z__corrupt")
    with pytest.raises(UnicodeDecodeError):
        standards_proposals.load("20260101T000000Z__corrupt", PROJECT_CODE)


def test_approve_corrupt_proposal_cannot_graft_mojibake(project_vault):
    """approve() loads via _parse_file; a corrupt proposal must fail closed so
    no U+FFFD-mutated policy text is appended into the project standards."""
    _write_corrupt_proposal(PROJECT_CODE, "20260101T000000Z__corrupt")
    with pytest.raises(UnicodeDecodeError):
        standards_proposals.approve("20260101T000000Z__corrupt", PROJECT_CODE)
