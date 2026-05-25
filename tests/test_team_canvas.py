"""Pre-V2 Slice C tests — team_canvas digest builder.

The integration into producer prompts is exercised via the
orchestration tests (drafter prompt assembly carries the new slot).
These tests cover the digest module directly: empty / populated /
binary / oversize / missing-dir cases.
"""

from __future__ import annotations


from modulatio import team_canvas


def test_build_digest_returns_empty_marker_when_dir_missing(tmp_path):
    """Missing artifacts/ → stable empty marker, never crashes. The
    first producer in a run hits this path before any other producer
    has shipped."""
    out = team_canvas.build_digest(tmp_path / "does-not-exist")
    assert "Team canvas" in out
    assert "first producer" in out


def test_build_digest_returns_empty_marker_when_dir_empty(tmp_path):
    """Empty artifacts/ → same empty marker as missing dir. Producers
    see consistent slot shape regardless of whether the run has any
    artifacts yet."""
    (tmp_path / "artifacts").mkdir()
    out = team_canvas.build_digest(tmp_path / "artifacts")
    assert "first producer" in out


def test_build_digest_lists_files_with_head_excerpt(tmp_path):
    """Populated artifacts/ — each text file gets a code-fenced head
    excerpt + line count. Engineer 2 sees engineer 1's actual class +
    method names, doesn't have to invent or guess."""
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "engine.py").write_text(
        "class Engine:\n"
        "    def __init__(self):\n"
        "        self.running = False\n"
        "    def tick(self):\n"
        "        pass\n"
    )
    out = team_canvas.build_digest(artifacts)
    assert "engine.py" in out
    assert "5 lines" in out  # 5 newlines + content
    assert "class Engine" in out
    assert "def tick" in out
    assert "```" in out  # head wrapped in fences


def test_build_digest_orders_files_alphabetically(tmp_path):
    """Stable ordering by relative path so two runs with the same
    artifacts produce identical digests (cache-friendly, diff-friendly)."""
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "z_combat.py").write_text("# combat\n")
    (artifacts / "a_engine.py").write_text("# engine\n")
    out = team_canvas.build_digest(artifacts)
    a_pos = out.find("a_engine.py")
    z_pos = out.find("z_combat.py")
    assert 0 < a_pos < z_pos


def test_build_digest_lists_binary_files_by_name_only(tmp_path):
    """Binary files (extension not in TEXT_EXTENSIONS) get listed by
    name + line count but no head excerpt — head would be noise. PNG,
    .so, .pyc, etc. all fall into this bucket."""
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "sprite.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
    out = team_canvas.build_digest(artifacts)
    assert "sprite.png" in out
    assert "listed only" in out


def test_build_digest_handles_unreadable_text(tmp_path):
    """A .py file with non-UTF8 bytes should NOT crash the digest
    builder — fall back to listed-only line. Defensive against partial
    writes, raw-byte saves, etc."""
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "bad.py").write_bytes(b"\xff\xfe\x00 invalid utf8")
    out = team_canvas.build_digest(artifacts)
    assert "bad.py" in out
    # Doesn't crash; falls back to listed-only.


def test_build_digest_truncates_at_max_chars(tmp_path, monkeypatch):
    """When the cumulative excerpts exceed MAX_DIGEST_CHARS, remaining
    files get listed-only and a truncation note appended. Prevents
    runaway digest in a sub-objective with hundreds of files."""
    monkeypatch.setattr(team_canvas, "MAX_DIGEST_CHARS", 200)
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    big_content = "x = 1\n" * 50
    (artifacts / "a.py").write_text(big_content)
    (artifacts / "b.py").write_text(big_content)
    (artifacts / "c.py").write_text(big_content)
    out = team_canvas.build_digest(artifacts)
    assert "Digest truncated" in out


def test_build_digest_recurses_into_subdirs(tmp_path):
    """Nested files (assets/, sprites/, modules/) all show up. Producer
    sees the whole tree, not just root-level."""
    artifacts = tmp_path / "artifacts"
    (artifacts / "modules").mkdir(parents=True)
    (artifacts / "modules" / "combat.py").write_text("# combat\n")
    out = team_canvas.build_digest(artifacts)
    assert "modules/combat.py" in out


def test_build_digest_skips_files_above_size_cap(tmp_path, monkeypatch):
    """Third-party review fix 2026-05-02: a multi-MiB log file
    accidentally written to artifacts shouldn't be read into memory
    just to slice off HEAD_LINES. Stat first; skip oversize files
    with a size-only listing.
    """
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()

    # Lower the cap to keep the test fast — same code path.
    monkeypatch.setattr(team_canvas, "MAX_FILE_BYTES_FOR_DIGEST", 1024)

    (artifacts / "small.py").write_text("# tiny\n")
    (artifacts / "big.log").write_text("x" * 10_000)  # over the 1 KiB cap

    out = team_canvas.build_digest(artifacts)
    # Small file's head is included.
    assert "small.py" in out
    assert "tiny" in out
    # Big file is acknowledged but its content NOT read into the digest.
    assert "big.log" in out
    assert "exceeds digest size cap" in out
    assert "x" * 100 not in out  # head body is absent


