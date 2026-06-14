"""Pre-ship regression: standards_proposals must read/write files as explicit
UTF-8, matching standards.py's explicit-utf-8 read side.

Without explicit ``encoding="utf-8"`` the write side defaults to the locale
preferred encoding. On a non-UTF-8 locale (e.g. latin-1) non-ASCII rule text
would be encoded with a different codec, then standards.py / _parse_file read
it back as utf-8 and either raise UnicodeDecodeError or silently corrupt the
bytes. This asymmetry is the bug; these tests pin the round-trip to utf-8
regardless of the ambient locale.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from modulatio import standards_proposals, vault


PROJECT_CODE = "TST"

# A rule body with non-ASCII content that round-trips cleanly only under utf-8.
NON_ASCII_BODY = "Prefer the em—dash “style” — café résumé naïve → ✓"


@pytest.fixture
def project_vault(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    vault.init_project(PROJECT_CODE, "Test", "Test objective")
    return tmp_path / PROJECT_CODE.lower()


def test_save_writes_utf8_bytes_regardless_of_locale(project_vault):
    """The persisted proposal file is UTF-8 on disk even when the platform
    default encoding is not utf-8 — the bytes must decode as utf-8."""
    proposal = standards_proposals.Proposal(
        domain="text",
        title="Non-ASCII rule",
        rule_body=NON_ASCII_BODY,
    )
    path = standards_proposals.save(proposal, PROJECT_CODE)

    # Decode the raw bytes explicitly as utf-8: this fails if save() wrote
    # using a non-utf-8 codec (the locale-dependent corruption bug).
    decoded = path.read_bytes().decode("utf-8")
    assert NON_ASCII_BODY in decoded


def test_save_then_parse_round_trips_non_ascii(project_vault):
    """list_proposals (which reads via _parse_file) must recover the exact
    non-ASCII rule body that save() wrote."""
    proposal = standards_proposals.Proposal(
        domain="text",
        title="Non-ASCII round trip",
        rule_body=NON_ASCII_BODY,
        rationale="ensure é survives",
    )
    standards_proposals.save(proposal, PROJECT_CODE)

    loaded = standards_proposals.list_proposals(PROJECT_CODE)
    assert len(loaded) == 1
    assert loaded[0].rule_body == NON_ASCII_BODY


def test_approve_appends_utf8_to_standards_file(project_vault):
    """approve() writes the rule into standards/<domain>.md as utf-8, so the
    explicit-utf-8 standards reader recovers it without corruption."""
    proposal = standards_proposals.Proposal(
        domain="text",
        title="UTF-8 approve",
        rule_body=NON_ASCII_BODY,
    )
    path = standards_proposals.save(proposal, PROJECT_CODE)
    domain_file = standards_proposals.approve(path.stem, PROJECT_CODE)

    decoded = domain_file.read_bytes().decode("utf-8")
    assert NON_ASCII_BODY in decoded
    # Mirror the standards.py read path (explicit utf-8) — must not raise.
    assert NON_ASCII_BODY in domain_file.read_text(encoding="utf-8")
