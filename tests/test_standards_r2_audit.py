"""Regression tests for the r2 debug audit findings on standards.py.

1. ``_parse_file`` must honor ``load_with_metadata``'s documented contract
   ("return empty rather than raise") for a present-but-broken standards file
   (non-utf-8 bytes, unreadable perms) — a single bad
   ``<project>/standards/<domain>.md`` must NOT crash the producer/QC hot path.
2. The shared-standards path must be resolved at CALL time, not frozen at
   import — relocating ``config.get_shared_resources_path()`` (e.g. after a
   config reload) must take effect without re-importing the module.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from modulatio import config, standards, vault


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


@pytest.fixture(autouse=True)
def isolate_seed(tmp_path_factory, monkeypatch):
    """Point the bundled-seed tier at an empty dir so these tests see only the
    files they create."""
    empty = tmp_path_factory.mktemp("seed_standards_r2")
    monkeypatch.setattr(standards, "_SEED_STANDARDS_ROOT", empty)
    return empty


def test_non_utf8_shared_file_returns_empty_not_raise(tmp_path, monkeypatch):
    """A non-utf-8 shared standards file is treated as absent, not a crash."""
    shared_root = tmp_path / "shared"
    monkeypatch.setattr(standards, "_STANDARDS_ROOT", shared_root)
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path / "projects")
    bad = shared_root / "text.md"
    bad.parent.mkdir(parents=True, exist_ok=True)
    # 0x80 0x81 are invalid as standalone UTF-8 — read_text(utf-8) would raise.
    bad.write_bytes(b"---\nfreshness_class: fresh\n---\n\x80\x81 broken body")

    entry = standards.load_with_metadata("text")
    assert entry.body == ""
    assert entry.freshness_class is None
    assert standards.load("text") == ""


def test_non_utf8_project_file_returns_empty_not_raise(tmp_path, monkeypatch):
    """Same contract for a present-but-broken PROJECT-local override file."""
    shared_root = tmp_path / "shared"
    monkeypatch.setattr(standards, "_STANDARDS_ROOT", shared_root)
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path / "projects")
    proj_file = vault.project_dir("TST") / "standards" / "text.md"
    proj_file.parent.mkdir(parents=True, exist_ok=True)
    proj_file.write_bytes(b"\xff\xfe not utf-8")

    # Must not raise; broken project file is treated as absent → empty entry.
    entry = standards.load_with_metadata("text", project_code="TST")
    assert entry.body == ""


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses file perms")
def test_unreadable_shared_file_returns_empty_not_raise(tmp_path, monkeypatch):
    """An unreadable (OSError on read) shared file is treated as absent."""
    shared_root = tmp_path / "shared"
    monkeypatch.setattr(standards, "_STANDARDS_ROOT", shared_root)
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path / "projects")
    f = shared_root / "code.md"
    _write(f, "---\n---\n# rules\n")
    os.chmod(f, 0o000)
    try:
        entry = standards.load_with_metadata("code")
        assert entry.body == ""
    finally:
        os.chmod(f, 0o644)


def test_parse_file_directly_returns_empty_on_bad_bytes(tmp_path):
    """Unit-level: _parse_file swallows decode errors and returns ({}, '')."""
    bad = tmp_path / "x.md"
    bad.write_bytes(b"\x80\x81\x82")
    assert standards._parse_file(bad) == ({}, "")


def test_shared_root_resolved_at_call_time(tmp_path, monkeypatch):
    """With no pin, _standards_root() re-resolves from config every call — a
    relocated shared-resources path takes effect without re-import."""
    monkeypatch.setattr(standards, "_STANDARDS_ROOT", None)
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path / "projects")

    first = tmp_path / "loc_a"
    second = tmp_path / "loc_b"
    _write(first / "standards" / "text.md", "---\n---\n# from A\n")
    _write(second / "standards" / "text.md", "---\n---\n# from B\n")

    monkeypatch.setattr(config, "get_shared_resources_path", lambda: first)
    assert standards._standards_root() == first / "standards"
    assert "from A" in standards.load("text")

    # Relocate (simulating a config reload) — no module re-import.
    monkeypatch.setattr(config, "get_shared_resources_path", lambda: second)
    assert standards._standards_root() == second / "standards"
    assert "from B" in standards.load("text")


def test_pinned_root_overrides_config(tmp_path, monkeypatch):
    """When _STANDARDS_ROOT is pinned, it wins over config (test-pin behavior
    that the existing suite relies on)."""
    pinned = tmp_path / "pinned"
    monkeypatch.setattr(standards, "_STANDARDS_ROOT", pinned / "standards")
    monkeypatch.setattr(config, "get_shared_resources_path", lambda: tmp_path / "other")
    assert standards._standards_root() == pinned / "standards"
