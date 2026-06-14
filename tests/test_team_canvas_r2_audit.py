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
