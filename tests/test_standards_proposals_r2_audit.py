"""R2 debug-audit regression tests for standards_proposals.

Isolated from test_standards_proposals.py to avoid colliding with
concurrent edits. Covers the same-second/same-title id-collision bug:
two proposals saved in the same UTC second with the same title must
NOT silently overwrite each other.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from modulatio import standards_proposals, vault


PROJECT_CODE = "R2A"


@pytest.fixture
def project_vault(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    vault.init_project(PROJECT_CODE, "Test", "Test objective")
    return tmp_path / PROJECT_CODE.lower()


def _proposal(rule_body: str) -> standards_proposals.Proposal:
    return standards_proposals.Proposal(
        domain="text",
        title="Same title every time",
        rule_body=rule_body,
        evidence_refs=("hist-001",),
        rationale="recurring",
    )


def test_same_second_same_title_proposals_do_not_overwrite(
    project_vault, monkeypatch
):
    """Two proposals with an identical title saved within the same UTC
    second must each persist to a distinct file with a distinct id —
    not collide and silently clobber the first."""
    frozen = datetime(2026, 6, 13, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(
        standards_proposals,
        "datetime",
        type(
            "_FrozenDatetime",
            (),
            {"now": staticmethod(lambda tz=None: frozen)},
        ),
    )

    path_a = standards_proposals.save(_proposal("Rule A body"), PROJECT_CODE)
    path_b = standards_proposals.save(_proposal("Rule B body"), PROJECT_CODE)

    # Distinct files, distinct ids.
    assert path_a != path_b
    assert path_a.stem != path_b.stem
    assert path_a.exists()
    assert path_b.exists()

    # Both bodies survived — neither was overwritten.
    assert "Rule A body" in path_a.read_text()
    assert "Rule B body" in path_b.read_text()

    # Both are enumerable and loadable by their ids.
    ids = standards_proposals.list_ids(PROJECT_CODE)
    assert path_a.stem in ids
    assert path_b.stem in ids
    assert len(standards_proposals.list_proposals(PROJECT_CODE)) == 2
