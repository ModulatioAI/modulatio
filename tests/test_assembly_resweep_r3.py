"""0.9.0 pre-ship round-3 re-sweep regressions for assembly.py.

Finding 1 (LOW, assembly.py:491): the document head's TOC cap-math used to seed
``_unit_headings``'s ``base_total`` with the TITLE line only — but the body that
``_assemble_document`` later concatenates counts the FULL ``title_page`` (title +
the entire rendered ``## Contents`` block) as its framing, AND it prepends that
head as the first block, so it separates the first unit too. The TOC's running
total therefore under-counted framing (the TOC block bytes + one leading
separator) vs. the body, and at the ``_MAX_TOTAL_BYTES`` cap the TOC could list a
final unit the body actually drops (TOC/body diverge by one unit).

The fix (a) charges the leading separator in ``_unit_headings`` when a framing
block precedes the units, and (b) iterates the head to a byte-size fixpoint
(seeding with the FULL candidate head), picking the safe side in the narrow
bistable band right at the cap — so the TOC is always a SUBSET of the units that
survive into the body, never a phantom entry.

This file is additive and must not collide with tests/test_assembly_resweep.py
(round-2, a different finding).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from modulatio import assembly


def _toc_titles(title_page: str) -> list[str]:
    """The heading text the rendered ``## Contents`` block lists, in order."""
    out: list[str] = []
    in_toc = False
    for line in title_page.splitlines():
        if line.strip() == "## Contents":
            in_toc = True
            continue
        if in_toc:
            m = re.match(r"^\d+\.\s+(.*)$", line.strip())
            if m:
                out.append(m.group(1))
    return out


def _body_kept_headings(title_page: str, bodies: dict[str, str], sep: str,
                        cap: int) -> list[str]:
    """Reproduce _assemble_document's unit-survival under ``cap`` for a given final
    ``title_page``, returning the headings of the units the body actually keeps."""
    framing = len(title_page.encode())
    if framing > cap:
        framing = 0
    total = framing
    sep_b = len(sep.encode())
    emitted = bool(title_page.strip())
    kept: list[str] = []
    for name, text in bodies.items():
        size = len(text.encode())
        added = size + (sep_b if emitted else 0)
        if total + added > cap:
            break
        total += added
        emitted = True
        kept.append(assembly._first_heading(text))
    return kept


def test_toc_is_always_a_subset_of_body_units_across_the_cap_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # Tiny fixtures + a shrunk cap so we can sweep the byte boundary where the
    # TOC/body coupling used to diverge, instead of needing 32MB of data.
    bodies = {
        "u1.txt": "# Alpha\n" + ("a" * 60),
        "u2.txt": "# Bravo\n" + ("b" * 60),
        "u3.txt": "# Charlie\n" + ("c" * 60),
    }
    for name, text in bodies.items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    sep = "\n\n---\n\n"
    title = "My Report"

    # Sweep a band of caps that straddles where the third unit and the TOC block
    # fight for the last few bytes — this is exactly where the title-only seed used
    # to let the TOC list a unit the body dropped.
    saw_partial = False
    for cap in range(240, 320):
        monkeypatch.setattr(assembly, "_MAX_TOTAL_BYTES", cap)
        framed = assembly.apply_framing(
            {"units": list(bodies), "separator": sep}, tmp_path, "document",
            title=title, required_structure=("toc",),
        )
        toc = _toc_titles(framed["title_page"])
        kept = _body_kept_headings(framed["title_page"], bodies, sep, cap)
        if len(kept) < len(bodies):
            saw_partial = True
        # The core invariant the finding is about: the TOC never lists a unit the
        # body drops at the cap.
        assert set(toc) <= set(kept), (
            f"cap={cap}: TOC {toc} lists a unit the body dropped (kept={kept})"
        )

    # Make sure the sweep actually exercised the cap-truncation boundary (otherwise
    # the subset assertion is vacuous).
    assert saw_partial


def test_toc_charges_the_leading_separator(tmp_path: Path,
                                           monkeypatch: pytest.MonkeyPatch):
    # Regression for sub-bug (a): with a non-empty head the body separates the FIRST
    # unit too. Pick a cap where one unit fits in the body ONLY if you (wrongly) skip
    # the leading separator. The TOC must drop the boundary unit, matching the body.
    bodies = {
        "u1.txt": "# Alpha\n" + ("a" * 50),
        "u2.txt": "# Bravo\n" + ("b" * 50),
    }
    for name, text in bodies.items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    sep = "\n\n---\n\n"
    title = "Doc"

    for cap in range(120, 200):
        monkeypatch.setattr(assembly, "_MAX_TOTAL_BYTES", cap)
        framed = assembly.apply_framing(
            {"units": list(bodies), "separator": sep}, tmp_path, "document",
            title=title, required_structure=("toc",),
        )
        toc = _toc_titles(framed["title_page"])
        kept = _body_kept_headings(framed["title_page"], bodies, sep, cap)
        assert set(toc) <= set(kept), f"cap={cap}: TOC {toc} vs body {kept}"


def test_toc_lists_all_units_when_everything_fits(tmp_path: Path):
    # No artificial cap: the fix must not over-prune when there is plenty of headroom.
    for name, text in {
        "a.txt": "# One\nbody",
        "b.txt": "# Two\nbody",
        "c.txt": "# Three\nbody",
    }.items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    framed = assembly.apply_framing(
        {"units": ["a.txt", "b.txt", "c.txt"]}, tmp_path, "document",
        title="Doc", required_structure=("toc",),
    )
    assert _toc_titles(framed["title_page"]) == ["One", "Two", "Three"]


def test_unit_headings_leading_block_charges_first_separator():
    # Direct unit test of the new _unit_headings parameter: leading_block=True must
    # charge a separator before the first unit (the body does, when a head precedes).
    import tempfile

    d = Path(tempfile.mkdtemp())
    (d / "x.txt").write_text("# X\n" + "x" * 20, encoding="utf-8")
    sep = "----"
    size = len((d / "x.txt").read_bytes())
    # Cap exactly at body size, so the extra leading separator pushes it over.
    cap = size
    import modulatio.assembly as a
    orig = a._MAX_TOTAL_BYTES
    try:
        a._MAX_TOTAL_BYTES = cap
        # Without a leading block: the single unit fits (no separator charged).
        assert a._unit_headings(["x.txt"], d, separator=sep, leading_block=False) == ["X"]
        # With a leading block: the leading separator pushes it over the cap → dropped.
        assert a._unit_headings(["x.txt"], d, separator=sep, leading_block=True) == []
    finally:
        a._MAX_TOTAL_BYTES = orig
