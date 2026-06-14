"""Tests for the research cache (slice #3 item #4).

Research follows the same Research-First pattern as standards — load from
cache first, fall back to fresh on miss — but diverges on writes: the
Researcher specialist WRITES to the cache, whereas standards are
human-curated. Research also does not stack across shared/project files;
project-local wins when present, shared is a simple fallback.
"""

from __future__ import annotations

from pathlib import Path

from modulatio import research, vault


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def test_load_returns_empty_entry_for_missing_topic(tmp_path, monkeypatch):
    """No shared cache and no project cache → empty entry, never crashes."""
    monkeypatch.setattr(research, "_RESEARCH_ROOT", tmp_path / "shared")
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path / "projects")
    entry = research.load_with_metadata("anything", project_code="TST")
    assert entry.body == ""
    assert entry.source_path is None
    assert entry.sources == ()


def test_load_with_metadata_parses_frontmatter(tmp_path, monkeypatch):
    """Cached research carries query + freshness + last_verified_at in its
    YAML frontmatter; loader exposes them on the entry."""
    monkeypatch.setattr(research, "_RESEARCH_ROOT", tmp_path / "shared")
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path / "projects")
    _write(
        tmp_path / "projects" / "tst" / "research" / "caffeine-effects.md",
        "---\n"
        "query: caffeine effects\n"
        "freshness_class: semi-stable\n"
        "last_verified_at: 2026-04-19\n"
        "---\n"
        "\n"
        "Caffeine is a stimulant. It blocks adenosine receptors.\n",
    )
    entry = research.load_with_metadata("caffeine effects", project_code="TST")
    assert "Caffeine is a stimulant" in entry.body
    assert entry.query == "caffeine effects"
    assert entry.freshness_class == "semi-stable"
    assert entry.last_verified_at == "2026-04-19"
    assert entry.source_path is not None
    assert "caffeine-effects.md" in entry.source_path


def test_load_prefers_project_local_over_shared(tmp_path, monkeypatch):
    """When both shared and project-local research exist for the same
    topic, project-local wins — the team's research supersedes baseline."""
    shared_root = tmp_path / "shared"
    projects_root = tmp_path / "projects"
    monkeypatch.setattr(research, "_RESEARCH_ROOT", shared_root)
    monkeypatch.setattr(vault, "VAULT_ROOT", projects_root)
    _write(shared_root / "topic.md", "Shared baseline notes.\n")
    _write(projects_root / "tst" / "research" / "topic.md", "Team-specific deep dive.\n")
    entry = research.load_with_metadata("topic", project_code="TST")
    assert "Team-specific deep dive" in entry.body
    assert "Shared baseline" not in entry.body


def test_save_round_trip(tmp_path, monkeypatch):
    """Writing a fresh research entry and reading it back yields the same
    content and metadata — the save/load pair is symmetric."""
    monkeypatch.setattr(research, "_RESEARCH_ROOT", tmp_path / "shared")
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path / "projects")
    research.save(
        topic="red spinning tops",
        body="Spinning tops can be red. Axes of rotation matter.\n",
        project_code="TST",
        query="red spinning tops",
        freshness_class="semi-stable",
        last_verified_at="2026-04-19",
        sources=("https://en.wikipedia.org/wiki/Spinning_top",),
    )
    entry = research.load_with_metadata("red spinning tops", project_code="TST")
    assert "Spinning tops can be red" in entry.body
    assert entry.query == "red spinning tops"
    assert entry.freshness_class == "semi-stable"
    assert entry.last_verified_at == "2026-04-19"
    assert "en.wikipedia.org" in entry.sources[0]


def test_save_round_trip_source_with_comma(tmp_path, monkeypatch):
    """A source string that itself contains a comma must survive the
    save/load round-trip as a SINGLE source — not be split on the comma.

    Regression: sources were inline-joined with ', ' on save and naively
    comma-split on load, so a citation like 'Smith, J. (2026)' or a URL
    with a comma fractured into multiple bogus sources.
    """
    monkeypatch.setattr(research, "_RESEARCH_ROOT", tmp_path / "shared")
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path / "projects")
    comma_source = "Smith, J. (2026), Journal of Tops, vol. 3"
    url_with_comma = "https://example.com/a,b?x=1,2"
    research.save(
        topic="comma sources",
        body="Body.\n",
        project_code="TST",
        sources=(comma_source, url_with_comma, "https://plain.example/ok"),
    )
    entry = research.load_with_metadata("comma sources", project_code="TST")
    assert entry.sources == (
        comma_source,
        url_with_comma,
        "https://plain.example/ok",
    )


def test_load_legacy_inline_comma_sources_still_parsed(tmp_path, monkeypatch):
    """Back-compat: entries written in the old inline 'sources: a, b' shape
    (comma-free sources) still load as a list of sources."""
    monkeypatch.setattr(research, "_RESEARCH_ROOT", tmp_path / "shared")
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path / "projects")
    _write(
        tmp_path / "projects" / "tst" / "research" / "legacy.md",
        "---\n"
        "sources: https://a.example, https://b.example\n"
        "---\n"
        "\nBody.\n",
    )
    entry = research.load_with_metadata("legacy", project_code="TST")
    assert entry.sources == ("https://a.example", "https://b.example")


def test_topic_slugs_are_filesystem_safe(tmp_path, monkeypatch):
    """Arbitrary topic strings — with spaces, punctuation, mixed case —
    must map deterministically to safe filenames. Two calls with equivalent
    topics resolve to the same cache file."""
    monkeypatch.setattr(research, "_RESEARCH_ROOT", tmp_path / "shared")
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path / "projects")
    research.save(
        topic="The Red Spinning Top (2026)!",
        body="Research body.\n",
        project_code="TST",
    )
    # Sloppy but equivalent topic string hits the same slug.
    entry = research.load_with_metadata("the red spinning top 2026", project_code="TST")
    assert "Research body" in entry.body
