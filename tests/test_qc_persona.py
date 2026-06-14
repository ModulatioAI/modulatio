# SPDX-License-Identifier: Apache-2.0
"""Tests for QC's persona loader (precedence + frontmatter) — the constructive
register injected into the QC review prompt."""
from __future__ import annotations

import pytest

from modulatio import config, qc_persona


@pytest.fixture
def iso(tmp_path, monkeypatch):
    seed = tmp_path / "seed_persona.md"
    seed.write_text("---\nname: qc_persona\nfreshness_class: stable\n---\n"
                    "SEED PERSONA\n", encoding="utf-8")
    monkeypatch.setattr(qc_persona, "_SEED_QC_PERSONA_FILE", seed)
    shared_root = tmp_path / "shared"
    shared_root.mkdir()
    monkeypatch.setattr(config, "get_shared_resources_path", lambda: shared_root)
    proj_root = tmp_path / "proj"
    monkeypatch.setattr(qc_persona, "project_dir", lambda code: proj_root / code)
    return tmp_path


def test_seed_is_the_default(iso):
    assert qc_persona.load_qc_persona() == "SEED PERSONA"


def test_frontmatter_is_stripped(iso):
    out = qc_persona.load_qc_persona()
    assert "name:" not in out and "---" not in out


def test_shared_overrides_seed(iso):
    (iso / "shared" / "qc_persona.md").write_text("SHARED PERSONA", encoding="utf-8")
    assert qc_persona.load_qc_persona() == "SHARED PERSONA"


def test_project_overrides_shared(iso):
    (iso / "shared" / "qc_persona.md").write_text("SHARED", encoding="utf-8")
    proj = iso / "proj" / "TST"
    proj.mkdir(parents=True)
    (proj / "qc_persona.md").write_text("PROJECT PERSONA", encoding="utf-8")
    assert qc_persona.load_qc_persona("TST") == "PROJECT PERSONA"
    # a different project with no override still falls back to shared
    assert qc_persona.load_qc_persona("OTHER") == "SHARED"


def test_non_utf8_project_persona_degrades_to_shared(iso):
    """A user pasting a non-UTF-8 byte into qc_persona.md must NOT crash the QC
    review prompt-build. read_text(encoding='utf-8') raises UnicodeDecodeError
    (a ValueError subclass, not OSError); the loader must catch it and fall
    through to the next candidate. Regression for the 0.9.0 pre-ship sweep."""
    (iso / "shared" / "qc_persona.md").write_text("SHARED PERSONA", encoding="utf-8")
    proj = iso / "proj" / "TST"
    proj.mkdir(parents=True)
    (proj / "qc_persona.md").write_bytes(b"\xff\xfe not valid utf-8 \x80\x81")
    # Before the fix this propagated UnicodeDecodeError; now it degrades.
    assert qc_persona.load_qc_persona("TST") == "SHARED PERSONA"


def test_non_utf8_shared_persona_degrades_to_seed(iso):
    """Same defensive contract on the shared candidate: a corrupt shared file
    falls through to the package seed instead of crashing."""
    (iso / "shared" / "qc_persona.md").write_bytes(b"\xff\xfe garbage \x80")
    assert qc_persona.load_qc_persona() == "SEED PERSONA"


def test_seed_default_carries_the_constructive_register():
    """The shipped seed actually encodes Clif's register: constructive,
    complimentary of what's right, actionable, peer-voiced, still honest."""
    body = qc_persona.load_qc_persona().lower()
    assert "lead with what's right" in body or "what the producer got" in body
    assert "actionable" in body
    assert "honest" in body and "soft" in body          # constructive != soft
