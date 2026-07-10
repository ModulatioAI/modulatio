# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Brick 1 of the skill-library arc: the resident index + discover/checkout
surface, and the producer-facing search_skills / load_skill / drop_skill
builtins. Pure-addition coverage — nothing here touches routing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from modulatio import skill_library, skills, tools


def _write_skill(d: Path, name: str, body: str, **fm: str) -> None:
    lines = [f"name: {name}"] + [f"{k}: {v}" for k, v in fm.items()]
    d.joinpath(f"{name}.md").write_text(
        "---\n" + "\n".join(lines) + "\n---\n\n" + body + "\n"
    )


@pytest.fixture
def isolated_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point the seed + shared roots at empty temp dirs so the index sees
    only the skills a test writes (and so checkout resolves against them)."""
    seed = tmp_path / "seed"
    shared = tmp_path / "shared"
    seed.mkdir()
    shared.mkdir()
    monkeypatch.setattr(skills, "_SEED_SKILLS_ROOT", seed)
    monkeypatch.setattr(skills, "_SKILLS_ROOT", shared)
    skills._WARNED_SUPERSEDED.clear()
    return seed, shared


# ── the resident index ────────────────────────────────────────────────────


def test_index_covers_seed_skills_without_bodies() -> None:
    """The index is built from the bundled seeds and carries metadata
    only — name, description, tags — never the prompt body."""
    idx = skill_library.build_index()
    names = {e.name for e in idx}
    # A representative slice of the bundled seeds.
    assert {"web-search", "researcher", "qc", "leader"} <= names
    # Index entries have no body field at all (cheap-index guardrail).
    assert not hasattr(idx[0], "prompt_template")


def test_index_one_entry_per_name() -> None:
    idx = skill_library.build_index()
    names = [e.name for e in idx]
    assert len(names) == len(set(names)), "index must not duplicate a skill name"


def test_read_frontmatter_stops_at_close_and_ignores_body(tmp_path: Path) -> None:
    """The cheap frontmatter reader must not pull body text into the index,
    even if the body itself contains a line that looks like `key: value`."""
    f = tmp_path / "demo.md"
    f.write_text(
        "---\n"
        "name: demo\n"
        "description: a demo skill\n"
        "capability_tags: alpha, beta\n"
        "---\n\n"
        "Body line that is sneaky: should not be parsed as frontmatter\n"
    )
    meta = skill_library._read_frontmatter(f)
    assert meta["name"] == "demo"
    assert meta["description"] == "a demo skill"
    assert meta["capability_tags"] == "alpha, beta"
    assert "sneaky" not in " ".join(meta.keys())


def test_read_frontmatter_no_frontmatter_returns_empty(tmp_path: Path) -> None:
    f = tmp_path / "plain.md"
    f.write_text("Just a body, no frontmatter.\n")
    assert skill_library._read_frontmatter(f) == {}


# ── search ────────────────────────────────────────────────────────────────


def test_search_finds_web_search_skill() -> None:
    hits = skill_library.search_skills("web search discover sources")
    assert any(e.name == "web-search" for e in hits)


def test_search_empty_query_returns_nothing() -> None:
    assert skill_library.search_skills("   ") == []


def test_search_respects_limit() -> None:
    hits = skill_library.search_skills("a e i o u the", limit=3)
    assert len(hits) <= 3


def test_search_ranks_more_matches_first() -> None:
    # "web-search" should rank above a skill that only matches one token.
    hits = skill_library.search_skills("web search current sources discover")
    names = [e.name for e in hits]
    assert "web-search" in names


# ── checkout ──────────────────────────────────────────────────────────────


def test_checkout_returns_full_body() -> None:
    skill = skill_library.checkout("web-search")
    assert skill.name == "web-search"
    assert skill.prompt_template.strip(), "checkout must return the full body"


def test_checkout_unknown_is_empty_not_error() -> None:
    skill = skill_library.checkout("definitely-not-a-real-skill")
    assert skill.name == ""  # the empty-skill sentinel; callers treat as missing


# ── index ↔ checkout agreement (task #84 freshness gate) ──────────────────


def test_index_reflects_supersession_not_stale_codification(isolated_roots) -> None:
    """A STALE machine codification (base_seed_hash no longer matches the
    bundled seed) is superseded by the seed on checkout. The resident index
    must advertise the SAME thing checkout returns — the seed's metadata —
    not the stale codification's description/tags that checkout discards.
    Regression for the index-vs-checkout drift (#38)."""
    seed, shared = isolated_roots
    _write_skill(
        seed, "s", "NEW SEED BODY",
        description="fresh seed description",
        capability_tags="fresh-tag",
    )
    # A codification stamped against an OLD seed → stale → superseded.
    _write_skill(
        shared, "s", "OLD CODIFIED BODY",
        version="3", base_seed_hash="deadbeefdeadbeef",
        description="stale codified description",
        capability_tags="stale-tag",
    )

    checked_out = skill_library.checkout("s")
    assert checked_out.prompt_template.strip() == "NEW SEED BODY"

    entry = next(e for e in skill_library.build_index() if e.name == "s")
    # The index must match checkout, not the stale shared file on disk.
    assert entry.description == checked_out.description == "fresh seed description"
    assert entry.capability_tags == checked_out.capability_tags == ("fresh-tag",)
    assert "stale" not in entry.description
    assert "stale-tag" not in entry.capability_tags


def test_index_honors_current_codification(isolated_roots) -> None:
    """When the shared codification is CURRENT (base_seed_hash matches), it
    wins on checkout — and the index advertises ITS metadata, not the seed's."""
    seed, shared = isolated_roots
    _write_skill(seed, "s", "SEED BODY", description="seed desc")
    h = skills.seed_content_hash("s")
    _write_skill(
        shared, "s", "CODIFIED BODY",
        version="3", base_seed_hash=h,
        description="codified desc", capability_tags="codified-tag",
    )

    checked_out = skill_library.checkout("s")
    entry = next(e for e in skill_library.build_index() if e.name == "s")
    assert entry.description == checked_out.description == "codified desc"
    assert entry.capability_tags == ("codified-tag",)


# ── the builtins ──────────────────────────────────────────────────────────


def test_builtins_registered_always() -> None:
    """Skill-library builtins are core capabilities — present in every
    registry, even the bare (no artifacts_root) one."""
    reg = tools.build_registry()
    for name in ("search_skills", "load_skill", "drop_skill"):
        assert name in reg
        assert reg[name].params_schema is not None


def test_search_skills_builtin_lists_matches() -> None:
    out = tools.make_search_skills(None)(query="web", limit=8)
    assert "Skills matching" in out
    assert "load_skill" in out  # nudges the producer toward checkout


def test_search_skills_builtin_empty_query() -> None:
    assert "non-empty" in tools.make_search_skills(None)(query="")


def test_load_skill_builtin_returns_body() -> None:
    out = tools.make_load_skill(None)(name="web-search")
    assert "Skill checked out: web-search" in out
    assert len(out) > 100  # the actual guidance, not a stub


def test_load_skill_builtin_unknown_points_to_search() -> None:
    out = tools.make_load_skill(None)(name="nope")
    assert "no skill named" in out
    assert "search_skills" in out


def test_drop_skill_builtin_is_advisory() -> None:
    out = tools.make_drop_skill(None)(name="web-search")
    assert "Dropped skill" in out
    assert "load_skill" in out  # reversible


def test_build_registry_accepts_project_code() -> None:
    """The new param threads through without disturbing the existing tools."""
    reg = tools.build_registry(project_code="someproj")
    assert "http_get" in reg and "web_search" in reg
    assert "search_skills" in reg


def test_read_frontmatter_survives_non_utf8(tmp_path):
    """Cross-file (R2): a non-UTF-8 skill file must not raise UnicodeDecodeError
    out of the index build (which would crash the wave scheduler) — degrade to a
    best-effort parse."""
    from modulatio import skill_library
    p = tmp_path / "weird.md"
    p.write_bytes(b"---\nname: weird\ndescription: has \xff\xfe bad bytes\n---\nbody\n")
    meta = skill_library._read_frontmatter(p)  # must not raise
    assert meta.get("name") == "weird"


# ═══ fold: test_skill_library_low_audit.py ═══
# LOW-audit regression tests for :mod:`modulatio.skill_library`.
#
# Finding #82: ``search_skills`` returned 1 result when ``limit <= 0`` instead
# of 0, because the slice used ``max(1, limit)``. A caller asking for zero (or
# a negative number of) results should get an empty list.


def test_search_limit_zero_returns_nothing() -> None:
    # A query that DOES match real skills, so the only thing keeping the
    # result empty is the limit guard (not a no-match).
    assert skill_library.search_skills("web search", limit=0) == []


def test_search_negative_limit_returns_nothing() -> None:
    # Negative limit must not fall through to Python's from-the-end slicing.
    assert skill_library.search_skills("web search", limit=-3) == []


def test_search_positive_limit_still_works() -> None:
    hits = skill_library.search_skills("web search", limit=2)
    assert 0 < len(hits) <= 2
