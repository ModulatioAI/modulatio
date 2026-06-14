# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""0.9.0 pre-ship re-sweep regressions for ``standards.py``.

Finding 1 [LOW/security]: ``load_with_metadata(domain, ...)`` built its three
tier paths (``seed``/``shared``/``project``) straight from ``domain`` (the
task's free-form, planner-sourced ``artifact_kind``) with no slug validation,
unlike ``qc_notes`` which guards with ``_DOMAIN_RE``. A traversal value could
escape the standards roots and read an arbitrary ``.md`` file. The fix
engine-binds the same bare-slug guard so every consumer inherits it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from modulatio import standards, vault


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


@pytest.fixture(autouse=True)
def isolate_roots(tmp_path, monkeypatch):
    """Point all three tiers at controlled temp dirs so the test reasons
    about path resolution, not Modulatio's shipped baseline standards."""
    seed_root = tmp_path / "seed"
    seed_root.mkdir()
    monkeypatch.setattr(standards, "_SEED_STANDARDS_ROOT", seed_root)
    monkeypatch.setattr(standards, "_STANDARDS_ROOT", tmp_path / "shared")
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path / "projects")
    return tmp_path


# --- Finding 1: path-traversal domain is rejected fail-closed ---------------

def test_traversal_domain_cannot_read_file_outside_seed_root(isolate_roots):
    """A ``../`` domain that would resolve to a real ``.md`` OUTSIDE the seed
    root must not be read — the guard returns the empty entry instead."""
    # Plant a "secret" .md one level above the seed root. A traversal domain
    # of "../secret" would resolve seed_path to exactly this file.
    secret = isolate_roots / "seed" / ".." / "secret.md"
    _write(secret.resolve(), "TOP SECRET STANDARDS\n")
    # Sanity: without the guard this path WOULD exist and be read.
    assert (standards._SEED_STANDARDS_ROOT / "../secret.md").exists()

    entry = standards.load_with_metadata("../secret")
    assert entry is standards._EMPTY_ENTRY
    assert entry.body == ""
    assert entry.sources == ()


def test_traversal_domain_via_load_wrapper_returns_empty(isolate_roots):
    """The string-returning ``load`` wrapper inherits the guard too."""
    secret = isolate_roots / "seed" / ".." / "secret.md"
    _write(secret.resolve(), "TOP SECRET\n")
    assert standards.load("../secret") == ""


def test_deep_traversal_into_etc_passwd_style_path_is_empty(isolate_roots):
    """A multi-segment traversal domain is non-conforming → empty entry,
    no crash, no read attempt outside the roots."""
    entry = standards.load_with_metadata("../../../../etc/passwd")
    assert entry is standards._EMPTY_ENTRY


def test_absolute_and_slash_domains_are_rejected(isolate_roots):
    """Any domain carrying a path separator fails the bare-slug pattern."""
    for bad in ("/etc/hosts", "sub/dir", "a/b", "."):
        entry = standards.load_with_metadata(bad)
        assert entry is standards._EMPTY_ENTRY, bad


def test_uppercase_and_special_chars_rejected(isolate_roots):
    """The slug pattern matches qc_notes: lowercase alnum + ``-``/``_`` only."""
    for bad in ("Text", "my domain", "kind!", "a" * 33):
        entry = standards.load_with_metadata(bad)
        assert entry is standards._EMPTY_ENTRY, bad


# --- Guard must NOT break legitimate bare-slug domains ----------------------

def test_legit_slug_domain_still_loads(isolate_roots):
    """A normal artifact_kind slug resolves and loads exactly as before."""
    _write(
        standards._SEED_STANDARDS_ROOT / "code.md",
        "# Code rules\n- Tests required.\n",
    )
    entry = standards.load_with_metadata("code")
    assert "Code rules" in entry.body
    assert len(entry.sources) == 1


def test_legit_slug_with_dash_and_underscore_loads(isolate_roots):
    """Hyphen/underscore slugs (e.g. ``data-set``, ``web_copy``) are valid."""
    for ok in ("data-set", "web_copy", "kind123"):
        _write(
            standards._SEED_STANDARDS_ROOT / f"{ok}.md",
            f"# {ok} rules\n",
        )
        entry = standards.load_with_metadata(ok)
        assert ok in entry.body, ok
