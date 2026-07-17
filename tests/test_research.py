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
import threading
import pytest


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


# ─── Reuse freshness: research goes stale after RESEARCH_TTL_DAYS ────────────


def test_is_stale_uses_last_verified_at():
    """Age is taken from last_verified_at when present: verified 40 days ago is
    stale at the 30-day TTL; 5 days ago is fresh."""
    from datetime import datetime, timedelta, timezone

    now = datetime(2026, 6, 1, tzinfo=timezone.utc)
    old = (now - timedelta(days=40)).date().isoformat()
    stale_entry = research.ResearchEntry(
        body="x", query=None, freshness_class=None,
        last_verified_at=old, sources=(), source_path=None,
    )
    assert research.is_stale(stale_entry, now=now) is True

    recent = (now - timedelta(days=5)).date().isoformat()
    fresh_entry = research.ResearchEntry(
        body="x", query=None, freshness_class=None,
        last_verified_at=recent, sources=(), source_path=None,
    )
    assert research.is_stale(fresh_entry, now=now) is False


def test_is_stale_falls_back_to_file_mtime(tmp_path, monkeypatch):
    """With no last_verified_at, age comes from the cache file's mtime: a
    freshly-saved note is fresh; back-dating its mtime past the TTL makes it
    stale."""
    import os
    from datetime import datetime, timedelta, timezone

    monkeypatch.setattr(research, "_RESEARCH_ROOT", tmp_path / "shared")
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path / "projects")
    path = research.save("topic x", "body", "TST")

    fresh = research.load_with_metadata("topic x", project_code="TST")
    assert fresh.last_verified_at is None          # save didn't stamp one
    assert research.is_stale(fresh) is False

    old = (datetime.now(timezone.utc) - timedelta(days=45)).timestamp()
    os.utime(path, (old, old))
    aged = research.load_with_metadata("topic x", project_code="TST")
    assert research.is_stale(aged) is True


def test_is_stale_undeterminable_age_is_not_stale():
    """Reuse-first: an entry whose age can't be determined (no timestamp, no
    resolvable file) is NOT treated as stale — don't discard research we can't
    prove is old."""
    entry = research.ResearchEntry(
        body="x", query=None, freshness_class=None,
        last_verified_at=None, sources=(), source_path=None,
    )
    assert research.is_stale(entry) is False
    # An empty entry is never stale either (it's a cache miss, handled upstream).
    assert research.is_stale(research._EMPTY_ENTRY) is False


def test_is_stale_file_skips_symlink(tmp_path):
    """A planted symlink must not be read through (out-of-tree leak) —
    is_stale_file returns False for a symlink instead of parsing its
    target."""
    secret = tmp_path / "secret.md"
    secret.write_text("---\nlast_verified_at: 2020-01-01\n---\nsensitive\n")
    link = tmp_path / "link.md"
    link.symlink_to(secret)
    assert research.is_stale_file(link) is False


def test_is_stale_rejects_future_last_verified_at():
    """A far-future last_verified_at (a producer-controlled file trying to look
    eternally fresh) must NOT defeat the reuse guard — a beyond-skew-future basis
    is treated as STALE so it gets re-fetched."""
    from datetime import datetime, timezone
    now = datetime(2026, 6, 1, tzinfo=timezone.utc)
    entry = research.ResearchEntry(
        body="x", query=None, freshness_class=None,
        last_verified_at="9999-01-01", sources=(), source_path=None,
    )
    assert research.is_stale(entry, now=now) is True
    # A within-skew "just verified" stamp is still fresh (not falsely stale).
    entry_now = research.ResearchEntry(
        body="x", query=None, freshness_class=None,
        last_verified_at="2026-06-01", sources=(), source_path=None,
    )
    assert research.is_stale(entry_now, now=now) is False


# ═══ fold: test_research_resweep_r4.py ═══
# Round-4 re-sweep regression: research.save must be atomic.
#
# Finding (LOW/race): research.save() persisted via a plain truncate-then-write
# ``path.write_text()``. The research cache READ path
# (load_with_metadata → _parse_file → read_text) is intentionally unlocked while
# the WRITE is serialized under the orchestrator's store lock, so a concurrent
# same-slug reader could observe a half-written (truncated) note and inject
# empty/partial research context into a prompt. The fix routes save() through a
# unique-temp-file + os.replace, so a reader always sees either the complete old
# file or the complete new one — never a truncation.
#
# These tests live in their own _r4 file to avoid colliding with the existing
# test_research.py / prior-round suites.


@pytest.fixture(autouse=True)
def _redirect_vault(tmp_path, monkeypatch):
    monkeypatch.setattr(research, "_RESEARCH_ROOT", tmp_path / "shared")
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path / "projects")
    return tmp_path


def _research_path(code: str, slug: str) -> Path:
    return vault.project_dir(code) / "research" / f"{slug}.md"


def test_save_roundtrips_and_leaves_no_temp_files(tmp_path):
    """Atomic save still writes correct content and cleans up after itself —
    no stray ``.tmp`` files left in the research dir."""
    path = research.save("Caffeine Effects", "caffeine keeps you awake", "TST")
    entry = research.load_with_metadata("Caffeine Effects", project_code="TST")
    assert entry.body.strip() == "caffeine keeps you awake"
    assert path.exists()
    siblings = list(path.parent.iterdir())
    # Exactly the one note, no leftover temp files from mkstemp.
    assert siblings == [path], siblings


def test_save_never_exposes_truncated_file_to_concurrent_reader(monkeypatch):
    """Core regression. While save() is mid-write, a same-slug reader must
    never see a truncated/empty body.

    We wedge a reader to fire at the exact moment the new bytes are committed:
    we wrap os.replace so that JUST BEFORE the rename we read the destination
    path. With the old plain write_text(), the destination would already have
    been truncated-then-(partially)-written in place, so a read here could
    observe an empty/partial body. With the atomic temp+rename, the destination
    is still the COMPLETE previous version (or absent) at this instant — the new
    content only becomes visible at the rename itself.
    """
    code = "TST"
    slug = "topic-x"
    path = _research_path(code, slug)

    # Seed a complete prior version of the note.
    research.save("topic x", "FIRST COMPLETE VERSION", code)
    assert path.exists()

    observed: list[str] = []

    real_replace = research.os.replace

    def spy_replace(src, dst, *a, **kw):
        # Inspect the destination the instant before it is atomically swapped.
        # Under an atomic implementation the temp file (src) holds the new
        # bytes and dst is untouched, so dst still parses to the OLD complete
        # body. A non-atomic write_text would have already clobbered dst.
        if Path(dst) == path and path.exists():
            entry = research.load_with_metadata("topic x", project_code=code)
            observed.append(entry.body.strip())
        return real_replace(src, dst, *a, **kw)

    monkeypatch.setattr(research.os, "replace", spy_replace)

    research.save("topic x", "SECOND COMPLETE VERSION", code)

    # The reader fired exactly once (one save), and never saw a truncated body:
    # it saw the full prior version, not "" or a partial string.
    assert observed == ["FIRST COMPLETE VERSION"], observed

    # Final state is the complete new version.
    final = research.load_with_metadata("topic x", project_code=code)
    assert final.body.strip() == "SECOND COMPLETE VERSION"


def test_concurrent_writers_and_readers_never_yield_partial_body(monkeypatch):
    """Stress: many writers churn the same slug while readers poll. Every read
    must return a body that is one of the COMPLETE values we wrote — never a
    truncated prefix. Non-atomic write_text() would intermittently expose an
    empty or partial body to the reader."""
    code = "TST"
    # Distinct, easily-validated complete bodies of varying length.
    bodies = [f"COMPLETE-BODY-{i:03d}-" + ("z" * (i * 7)) for i in range(60)]
    valid = set(bodies)

    # Pre-seed so the very first reads have a complete file to find.
    research.save("hot topic", bodies[0], code)

    stop = threading.Event()
    bad: list[str] = []
    errors: list[BaseException] = []

    def writer():
        try:
            for b in bodies:
                research.save("hot topic", b, code)
        except BaseException as exc:  # noqa: BLE001 - surface in assertion
            errors.append(exc)
        finally:
            stop.set()

    def reader():
        try:
            while not stop.is_set():
                entry = research.load_with_metadata("hot topic", project_code=code)
                body = entry.body.strip()
                if body not in valid:
                    bad.append(repr(body))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=writer)]
    threads += [threading.Thread(target=reader) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, errors
    assert not bad, bad[:5]
