"""Round-4 re-sweep regression: research.save must be atomic.

Finding (LOW/race): research.save() persisted via a plain truncate-then-write
``path.write_text()``. The research cache READ path
(load_with_metadata → _parse_file → read_text) is intentionally unlocked while
the WRITE is serialized under the orchestrator's store lock, so a concurrent
same-slug reader could observe a half-written (truncated) note and inject
empty/partial research context into a prompt. The fix routes save() through a
unique-temp-file + os.replace, so a reader always sees either the complete old
file or the complete new one — never a truncation.

These tests live in their own _r4 file to avoid colliding with the existing
test_research.py / prior-round suites.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from modulatio import research, vault


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
