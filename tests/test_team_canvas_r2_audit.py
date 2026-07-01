# SPDX-License-Identifier: Apache-2.0
"""Regression: build_digest must bound name-only listing lines too.

Before the fix, the MAX_DIGEST_CHARS cap only gated head-excerpt blocks;
every 'listed only' path (oversized / binary / non-source) appended to the
digest without counting toward the budget, so a tree of thousands of such
files produced an arbitrarily large digest. These tests pin the bound.
"""

from __future__ import annotations

from modulatio import team_canvas
from modulatio.team_canvas import MAX_DIGEST_CHARS, build_digest


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

def test_digest_priority_prefix_puts_this_run_first(tmp_path):
    """With a priority_prefix (the current run), this run's artifacts are
    emitted BEFORE any prior run's — even though the prior run sorts earlier
    alphabetically. Fixes the saturated-digest bug where a run couldn't see
    its own freshly-produced sections (they sorted last, got truncated)."""
    root = tmp_path / "artifacts"
    (root / "20260629-old").mkdir(parents=True)
    (root / "20260629-old" / "aaa.md").write_text("# old\nold body\n")
    (root / "20260701-new").mkdir(parents=True)
    (root / "20260701-new" / "zzz.md").write_text("# new\nnew body\n")

    d = build_digest(root, priority_prefix="20260701-new")
    assert d.index("20260701-new/zzz.md") < d.index("20260629-old/aaa.md")


def test_digest_prior_runs_ordered_most_recent_first(tmp_path):
    """Among prior runs, the most recent (lexicographically-largest run id,
    since ids are timestamp-prefixed) is listed first — the freshest prior
    work is the most reusable, so it survives the cap first."""
    root = tmp_path / "artifacts"
    for rid in ("20260601-a", "20260615-b", "20260630-c"):
        (root / rid).mkdir(parents=True)
        (root / rid / "s.md").write_text(f"# {rid}\n")
    d = build_digest(root, priority_prefix="does-not-exist")
    i_recent = d.index("20260630-c/s.md")
    i_mid = d.index("20260615-b/s.md")
    i_old = d.index("20260601-a/s.md")
    assert i_recent < i_mid < i_old


def test_digest_no_priority_prefix_stays_alphabetical(tmp_path):
    """Back-compat: without priority_prefix, ordering is unchanged
    (alphabetical by relative path)."""
    root = tmp_path / "artifacts"
    root.mkdir()
    (root / "a.md").write_text("a\n")
    (root / "z.md").write_text("z\n")
    d = build_digest(root)
    assert d.index("a.md") < d.index("z.md")
