"""Pre-V2 Slice C tests — team_canvas digest builder.

The integration into producer prompts is exercised via the
orchestration tests (drafter prompt assembly carries the new slot).
These tests cover the digest module directly: empty / populated /
binary / oversize / missing-dir cases.
"""

from __future__ import annotations


from modulatio import team_canvas
from modulatio.team_canvas import MAX_DIGEST_CHARS, build_digest


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




# ═══ fold: test_team_canvas_r2_audit.py ═══
# Regression: build_digest must bound name-only listing lines too.
#
# Before the fix, the MAX_DIGEST_CHARS cap only gated head-excerpt blocks;
# every 'listed only' path (oversized / binary / non-source) appended to the
# digest without counting toward the budget, so a tree of thousands of such
# files produced an arbitrarily large digest. These tests pin the bound.


def test_digest_bounded_with_many_name_only_files(tmp_path):
    # A non-source extension => always 'listed only'. Make enough of them
    # that the *unbounded* digest would blow far past the cap.
    root = tmp_path / "artifacts"
    root.mkdir()
    per_line = 60  # rough rendered length of one name-only line
    n = (MAX_DIGEST_CHARS // per_line) * 4  # ~4x the cap if unbounded
    for i in range(n):
        (root / f"blob_{i:05d}.bin").write_text("x")

    digest = build_digest(root)

    # The whole digest stays within a small fixed overhead of the cap
    # (header + truncation footer are tiny and fixed).
    assert len(digest) <= MAX_DIGEST_CHARS + 500
    assert "omitted" in digest


def test_digest_bounded_with_oversized_files(tmp_path):
    root = tmp_path / "artifacts"
    root.mkdir()
    # Each file exceeds MAX_FILE_BYTES_FOR_DIGEST -> the oversized 'listed
    # only' path. Pre-fix this path bypassed the cap entirely.
    big = "y" * (team_canvas.MAX_FILE_BYTES_FOR_DIGEST + 16)
    n = 4000
    for i in range(n):
        (root / f"huge_{i:05d}.py").write_text(big)

    digest = build_digest(root)
    assert len(digest) <= MAX_DIGEST_CHARS + 500
    assert "omitted" in digest


def test_digest_under_cap_lists_everything(tmp_path):
    # A handful of small files must still all appear (no spurious truncation).
    root = tmp_path / "artifacts"
    root.mkdir()
    for i in range(5):
        (root / f"mod_{i}.py").write_text(f"def f{i}():\n    return {i}\n")

    digest = build_digest(root)
    for i in range(5):
        assert f"mod_{i}.py" in digest
    assert "omitted" not in digest


def test_omitted_count_present_and_plausible(tmp_path):
    root = tmp_path / "artifacts"
    root.mkdir()
    n = 5000
    for i in range(n):
        (root / f"blob_{i:05d}.bin").write_text("x")

    digest = build_digest(root)
    # The footer reports a positive omitted count and the rendered file
    # lines plus omitted count never imply more files than we created.
    assert "remaining file(s) omitted" in digest


# ── this-run-first + recency ordering (reuse: producers must see their own
#    run's work + the most recent prior work, not have it truncated away) ────

def test_digest_hoist_run_id_puts_this_run_first(tmp_path):
    """With a hoist_run_id (the current run), this run's artifacts are
    emitted BEFORE any prior run's — even though the prior run sorts earlier
    alphabetically. Fixes the saturated-digest bug where a run couldn't see
    its own freshly-produced sections (they sorted last, got truncated)."""
    root = tmp_path / "artifacts"
    (root / "20260629-old").mkdir(parents=True)
    (root / "20260629-old" / "aaa.md").write_text("# old\nold body\n")
    (root / "20260701-new").mkdir(parents=True)
    (root / "20260701-new" / "zzz.md").write_text("# new\nnew body\n")

    d = build_digest(root, hoist_run_id="20260701-new")
    assert d.index("20260701-new/zzz.md") < d.index("20260629-old/aaa.md")


def test_digest_prior_runs_ordered_most_recent_first(tmp_path):
    """Among prior runs, the most recent (lexicographically-largest run id,
    since ids are timestamp-prefixed) is listed first — the freshest prior
    work is the most reusable, so it survives the cap first."""
    root = tmp_path / "artifacts"
    for rid in ("20260601-a", "20260615-b", "20260630-c"):
        (root / rid).mkdir(parents=True)
        (root / rid / "s.md").write_text(f"# {rid}\n")
    d = build_digest(root, hoist_run_id="does-not-exist")
    i_recent = d.index("20260630-c/s.md")
    i_mid = d.index("20260615-b/s.md")
    i_old = d.index("20260601-a/s.md")
    assert i_recent < i_mid < i_old


def test_digest_no_hoist_run_id_stays_alphabetical(tmp_path):
    """Back-compat: without hoist_run_id, ordering is unchanged
    (alphabetical by relative path)."""
    root = tmp_path / "artifacts"
    root.mkdir()
    (root / "a.md").write_text("a\n")
    (root / "z.md").write_text("z\n")
    d = build_digest(root)
    assert d.index("a.md") < d.index("z.md")


# ═══ fold: test_team_canvas_resweep_r3.py ═══
# Round-3 re-sweep regressions for team_canvas — Finding 1 (security).
#
# A producer holding a shell tool can plant a symlink inside the run's
# artifacts/ tree pointing at a file OUTSIDE the tree (e.g. /etc/passwd or
# another project's vault). Because rglob('*') filtered by is_file() follows
# symlinks, the out-of-tree file's head-excerpt would otherwise be read and
# injected into the NEXT producer's prompt context. These tests assert the
# digest builder refuses to surface symlinked / escaped paths.


def test_symlink_to_out_of_tree_file_is_not_read(tmp_path):
    """A symlink in artifacts/ pointing outside the tree must NOT leak the
    target's contents into the digest."""
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP-SECRET-VAULT-CONTENTS\nrow2\n", encoding="utf-8")

    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    # A legitimate in-tree file so the digest is non-empty.
    (artifacts / "real.py").write_text("class Real:\n    pass\n", encoding="utf-8")
    # The planted escape symlink.
    link = artifacts / "leak.txt"
    link.symlink_to(secret)

    out = team_canvas.build_digest(artifacts)

    assert "TOP-SECRET-VAULT-CONTENTS" not in out
    assert "leak.txt" not in out
    # The honest in-tree file is still surfaced.
    assert "real.py" in out


def test_symlink_to_in_tree_file_is_skipped_not_duplicated(tmp_path):
    """Even a symlink whose target is INSIDE the tree is skipped (we keep the
    real file once; the symlink alias is dropped rather than read again)."""
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "engine.py").write_text("class Engine:\n    pass\n", encoding="utf-8")
    alias = artifacts / "alias.py"
    alias.symlink_to(artifacts / "engine.py")

    out = team_canvas.build_digest(artifacts)

    assert "engine.py" in out
    assert "alias.py" not in out


def test_symlinked_subdir_escape_is_blocked(tmp_path):
    """A symlinked DIRECTORY inside artifacts/ pointing out of tree must not
    expose files reached through it."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "creds.env").write_text("API_KEY=sk-leaked\n", encoding="utf-8")

    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "ok.md").write_text("# fine\n", encoding="utf-8")
    (artifacts / "escape").symlink_to(outside, target_is_directory=True)

    out = team_canvas.build_digest(artifacts)

    assert "API_KEY=sk-leaked" not in out
    assert "creds.env" not in out
    assert "ok.md" in out
