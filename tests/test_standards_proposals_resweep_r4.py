# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Re-sweep R4 regressions for standards_proposals.

Finding 1 [MEDIUM/error-path]: `_parse_file` read proposal `.md` files
with strict UTF-8 (no `errors=` fallback), unlike the 0.9.0-hardened JSON
readers in team_memory which use `errors="replace"`. A single proposal
containing a non-UTF-8 byte (operator hand-edit in a non-UTF-8 editor)
would raise UnicodeDecodeError and crash the entire `modulatio-standards`
review surface — list, show, approve, reject — taking down every OTHER
(valid) proposal with it. The fix pins `errors="replace"` so a corrupt
byte degrades the one file rather than the whole surface.
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
    """Stage a proposal `.md` file whose frontmatter contains a raw
    non-UTF-8 byte (0xFF), as an operator hand-edit in a non-UTF-8
    editor would produce."""
    root = standards_proposals._proposals_dir(project_code)
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{stem}.md"
    raw = (
        b"---\n"
        b"domain: text\n"
        b"title: caf\xe9 rule\n"  # 0xe9 is a lone non-UTF-8 byte here
        b"evidence_refs: q1\n"
        b"rationale: keep accents\n"
        b"---\n\n"
        b"Body of the rule.\n"
    )
    path.write_bytes(raw)
    return path


# ── _parse_file resilience ──────────────────────────────────────────────────

def test_parse_file_tolerates_non_utf8_byte(project_vault):
    """A corrupt byte in a proposal file must not raise — it should
    decode via replacement and still yield a usable Proposal."""
    _write_corrupt_proposal(PROJECT_CODE, "20260101T000000Z__cafe-rule")
    path = standards_proposals._proposals_dir(PROJECT_CODE) / "20260101T000000Z__cafe-rule.md"
    proposal = standards_proposals._parse_file(path)  # must NOT raise UnicodeDecodeError
    assert proposal.domain == "text"
    assert proposal.rule_body == "Body of the rule."


def test_list_proposals_skips_no_one_on_corrupt_byte(project_vault):
    """One corrupt proposal must not crash list_proposals — every
    proposal (the corrupt one plus a clean one) is still returned."""
    standards_proposals.save(
        standards_proposals.Proposal(
            domain="code", title="Clean rule", rule_body="Use type hints."
        ),
        PROJECT_CODE,
    )
    _write_corrupt_proposal(PROJECT_CODE, "20260101T000000Z__corrupt")
    proposals = standards_proposals.list_proposals(PROJECT_CODE)  # must NOT raise
    titles = {p.title for p in proposals}
    assert "Clean rule" in titles
    assert len(proposals) == 2


def test_load_corrupt_proposal_does_not_crash(project_vault):
    """`load` (used by show/approve) must survive a corrupt-byte file."""
    _write_corrupt_proposal(PROJECT_CODE, "20260101T000000Z__corrupt")
    proposal = standards_proposals.load(
        "20260101T000000Z__corrupt", PROJECT_CODE
    )  # must NOT raise
    assert proposal.domain == "text"


def test_approve_corrupt_proposal_does_not_crash_on_decode(project_vault):
    """approve() loads via _parse_file; a corrupt byte must not crash the
    decode. The clean domain ('text') still routes to a safe standards file."""
    _write_corrupt_proposal(PROJECT_CODE, "20260101T000000Z__corrupt")
    dest = standards_proposals.approve(
        "20260101T000000Z__corrupt", PROJECT_CODE
    )  # must NOT raise UnicodeDecodeError
    assert dest.name == "text.md"
    assert dest.exists()
