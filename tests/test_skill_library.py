# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Brick 1 of the skill-library arc: the resident index + discover/checkout
surface, and the producer-facing search_skills / load_skill / drop_skill
builtins. Pure-addition coverage — nothing here touches routing.
"""

from __future__ import annotations

from pathlib import Path


from modulatio import skill_library, tools


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
