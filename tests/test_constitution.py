# SPDX-License-Identifier: Apache-2.0
"""Tests for the Leader's constitution loader (precedence + frontmatter)."""
from __future__ import annotations

import pytest

from modulatio import config, constitution


@pytest.fixture
def iso(tmp_path, monkeypatch):
    seed = tmp_path / "seed_constitution.md"
    seed.write_text("---\nname: constitution\nfreshness_class: stable\n---\n"
                    "SEED VALUES\n", encoding="utf-8")
    monkeypatch.setattr(constitution, "_SEED_CONSTITUTION_FILE", seed)
    shared_root = tmp_path / "shared"
    shared_root.mkdir()
    monkeypatch.setattr(config, "get_shared_resources_path", lambda: shared_root)
    proj_root = tmp_path / "proj"
    monkeypatch.setattr(constitution, "project_dir", lambda code: proj_root / code)
    return tmp_path


def test_seed_is_the_default(iso):
    assert constitution.load_constitution() == "SEED VALUES"


def test_frontmatter_is_stripped(iso):
    out = constitution.load_constitution()
    assert "name:" not in out
    assert "---" not in out


def test_shared_overrides_seed(iso):
    (iso / "shared" / "constitution.md").write_text(
        "SHARED VALUES", encoding="utf-8")
    assert constitution.load_constitution() == "SHARED VALUES"


def test_project_overrides_shared(iso):
    (iso / "shared" / "constitution.md").write_text("SHARED", encoding="utf-8")
    proj = iso / "proj" / "TST"
    proj.mkdir(parents=True)
    (proj / "constitution.md").write_text("PROJECT VALUES", encoding="utf-8")
    assert constitution.load_constitution("TST") == "PROJECT VALUES"
    # a different project with no override still falls back to shared
    assert constitution.load_constitution("OTHER") == "SHARED"


def test_non_utf8_shared_file_degrades_to_seed(iso):
    """A non-UTF-8 byte in a user-editable constitution must NOT crash the
    Leader prompt-build (UnicodeDecodeError is a ValueError, not OSError) —
    it degrades to the next candidate."""
    (iso / "shared" / "constitution.md").write_bytes(b"\xff\xfe bad bytes")
    # Before the fix this raised UnicodeDecodeError; now it falls back.
    assert constitution.load_constitution() == "SEED VALUES"


def test_non_utf8_project_file_degrades_to_shared(iso):
    (iso / "shared" / "constitution.md").write_text("SHARED", encoding="utf-8")
    proj = iso / "proj" / "TST"
    proj.mkdir(parents=True)
    (proj / "constitution.md").write_bytes(b"\xff\xfe\x80broken")
    assert constitution.load_constitution("TST") == "SHARED"


def test_seed_default_has_real_values():
    """The shipped seed actually carries the four value sections."""
    body = constitution.load_constitution()
    for heading in ("Honesty", "Diligence", "Harm", "Respect"):
        assert heading in body
