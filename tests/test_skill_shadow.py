"""Tests for the codified-skill SHADOW fix (task #84).

The bug: a machine-codified shared skill unconditionally shadowed the
package seed, so a seed improvement silently never loaded (it buried the
consolidation assembly-manifest fix). The fix makes a *codified* shared
skill supersede-able by a newer seed (via a stamped base_seed_hash),
while a *user* override stays sacred.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from modulatio import skills


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    seed = tmp_path / "seed"
    shared = tmp_path / "shared"
    seed.mkdir()
    shared.mkdir()
    monkeypatch.setattr(skills, "_SEED_SKILLS_ROOT", seed)
    monkeypatch.setattr(skills, "_SKILLS_ROOT", shared)
    skills._WARNED_SUPERSEDED.clear()
    return seed, shared


def _write(d: Path, name: str, body: str, **fm) -> None:
    lines = [f"name: {name}"] + [f"{k}: {v}" for k, v in fm.items()]
    d.joinpath(f"{name}.md").write_text("---\n" + "\n".join(lines) + "\n---\n\n" + body + "\n")


# ── supersession matrix ───────────────────────────────────────────────────


def test_user_override_always_wins(dirs):
    """A shared skill with no version / no base_seed_hash is a user override —
    honored even when the seed differs. Sacred."""
    seed, shared = dirs
    _write(seed, "s", "SEED BODY")
    _write(shared, "s", "USER BODY")  # no version, no base_seed_hash
    assert skills.load_with_metadata("s").prompt_template.strip() == "USER BODY"


def test_current_codification_wins(dirs):
    """Codified shared skill whose base_seed_hash matches the current seed →
    it's current, so it wins (its learning applies)."""
    seed, shared = dirs
    _write(seed, "s", "SEED BODY")
    h = skills.seed_content_hash("s")
    _write(shared, "s", "CODIFIED BODY", version="3", base_seed_hash=h)
    assert skills.load_with_metadata("s").prompt_template.strip() == "CODIFIED BODY"


def test_stale_codification_superseded_by_seed(dirs):
    """Codified shared skill whose base_seed_hash no longer matches the seed
    (package shipped a newer seed) → the SEED supersedes it. This is the bug."""
    seed, shared = dirs
    _write(seed, "s", "NEW IMPROVED SEED BODY")
    _write(shared, "s", "OLD CODIFIED BODY", version="3", base_seed_hash="deadbeefdeadbeef")
    assert skills.load_with_metadata("s").prompt_template.strip() == "NEW IMPROVED SEED BODY"


def test_legacy_codification_without_stamp_is_preserved(dirs):
    """A pre-#84 codification (version but no base_seed_hash) can't be proven
    stale → preserved as-is (don't nuke existing learning). It gets stamped on
    the next re-codify."""
    seed, shared = dirs
    _write(seed, "s", "SEED BODY")
    _write(shared, "s", "LEGACY CODIFIED BODY", version="3")  # no base_seed_hash
    assert skills.load_with_metadata("s").prompt_template.strip() == "LEGACY CODIFIED BODY"


def test_no_shared_falls_to_seed(dirs):
    seed, shared = dirs
    _write(seed, "s", "SEED BODY")
    assert skills.load_with_metadata("s").prompt_template.strip() == "SEED BODY"


def test_supersede_warns_once(dirs, caplog):
    seed, shared = dirs
    _write(seed, "s", "SEED")
    _write(shared, "s", "OLD", version="2", base_seed_hash="0000000000000000")
    import logging
    with caplog.at_level(logging.WARNING, logger="modulatio.skills"):
        skills.load_with_metadata("s")
        skills.load_with_metadata("s")
    assert sum("is stale" in r.message for r in caplog.records) == 1


# ── save() auto-stamps base_seed_hash for codifications ───────────────────


def test_save_stamps_base_seed_hash_for_codified_skill(dirs):
    seed, shared = dirs
    _write(seed, "s", "SEED BODY")
    sk = skills.Skill(name="s", description="d", prompt_template="CODIFIED", version="1")
    skills.save(sk)
    written = (shared / "s.md").read_text()
    assert f"base_seed_hash: {skills.seed_content_hash('s')}" in written


def test_save_does_not_stamp_user_skill(dirs):
    """No version → user override → no base_seed_hash stamp (stays sacred)."""
    seed, shared = dirs
    _write(seed, "s", "SEED BODY")
    sk = skills.Skill(name="s", description="d", prompt_template="USER")
    skills.save(sk)
    assert "base_seed_hash:" not in (shared / "s.md").read_text()


def test_save_no_seed_no_stamp(dirs):
    """Codified skill with no corresponding seed → nothing to stamp."""
    seed, shared = dirs
    sk = skills.Skill(name="novel", description="d", prompt_template="X", version="1")
    skills.save(sk)
    assert "base_seed_hash:" not in (shared / "novel.md").read_text()


def test_roundtrip_codification_stays_current(dirs):
    """Codify → save (auto-stamp) → load: the freshly codified skill is current
    (base matches seed) so it wins. End-to-end of the happy path."""
    seed, shared = dirs
    _write(seed, "s", "SEED BODY")
    skills.save(skills.Skill(name="s", description="d", prompt_template="FRESH CODIFIED", version="1"))
    assert skills.load_with_metadata("s").prompt_template.strip() == "FRESH CODIFIED"
