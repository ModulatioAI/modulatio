"""Tests for qc_notes (slice #8.2).

Human-curated training notes for QC, domain-scoped. The file lives at
``<project_vault>/qc-notes/<domain>.md`` and is loaded at QC time and
injected into the QC prompt as the standing-notes slot.

Orthogonal concerns (tested elsewhere):
- One-shot session notes from the ``--qc-notes`` CLI flag flow through
  the Orchestrator kwarg, not this module.
- The QC prompt's two-slot layout (standing vs one-shot) is tested in
  ``test_orchestration_qc_history.py`` integration tests.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from modulatio import qc_notes, vault


PROJECT_CODE = "TST"


@pytest.fixture
def project(tmp_path: Path, monkeypatch) -> str:
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path / "vault")
    vault.init_project(PROJECT_CODE, "Test", "test")
    return PROJECT_CODE


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def test_load_standing_notes_returns_empty_when_no_file(project):
    """Missing file → empty string. QC prompt renders a neutral marker;
    never an exception on a fresh project."""
    assert qc_notes.load_standing_notes("essay", project) == ""


def test_load_standing_notes_returns_body_when_file_present(project):
    """File body is returned as-is so QC reads the human's verbatim
    guidance. No truncation, no summarization."""
    _write(
        vault.VAULT_ROOT / "tst" / "qc-notes" / "essay.md",
        "Reject any draft that opens with 'In conclusion'.\n",
    )
    out = qc_notes.load_standing_notes("essay", project)
    assert "Reject any draft that opens" in out


def test_load_standing_notes_strips_frontmatter_when_present(project):
    """Human may annotate the file with YAML frontmatter for their own
    organization; the QC prompt only wants the body."""
    _write(
        vault.VAULT_ROOT / "tst" / "qc-notes" / "essay.md",
        "---\n"
        "owner: reviewer\n"
        "last_reviewed: 2026-04-21\n"
        "---\n\n"
        "Prefer concrete claims over hedging language.\n",
    )
    out = qc_notes.load_standing_notes("essay", project)
    assert out.startswith("Prefer concrete claims")
    assert "owner: reviewer" not in out


def test_load_standing_notes_is_domain_scoped(project):
    """Essay QC must not pick up code-domain guidance. One file per
    domain — no stacking, no shared fallback."""
    _write(
        vault.VAULT_ROOT / "tst" / "qc-notes" / "essay.md",
        "Essay guidance here.\n",
    )
    _write(
        vault.VAULT_ROOT / "tst" / "qc-notes" / "code.md",
        "Code guidance here.\n",
    )
    assert "Code guidance" not in qc_notes.load_standing_notes("essay", project)
    assert "Essay guidance" not in qc_notes.load_standing_notes("code", project)
