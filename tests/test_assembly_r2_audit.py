"""Regression tests for the Cowboy/Opus round-2 full-debug findings scoped to
``src/modulatio/assembly.py``:

  * MEDIUM/race — csv.field_size_limit save/restore races across concurrent wave
    workers (process-global) and could leak the raised ceiling.
  * LOW/product-agnostic — TOC cap math omitted framing bytes, so the TOC could
    list a unit the body drops at the byte cap.
"""
from __future__ import annotations

import csv
import threading

import modulatio.assembly as assembly
from modulatio.assembly import _document_head, _merge_csv, _unit_headings


# ── MEDIUM: csv.field_size_limit must not leak across concurrent merges ────────

def test_merge_csv_restores_field_size_limit():
    """A single merge restores the prior process-global field_size_limit."""
    orig = csv.field_size_limit()
    try:
        out, errs = _merge_csv([("a.csv", "h\n1\n2\n")], dedupe=False)
        assert out.startswith("h")
        assert csv.field_size_limit() == orig
    finally:
        csv.field_size_limit(orig)


def test_merge_csv_concurrent_does_not_leak_raised_ceiling():
    """Concurrently running many merges must always leave the process-global
    ``csv.field_size_limit`` at its true original value.

    Pre-fix the save/restore idiom raced: one worker could capture another's
    RAISED ceiling as its "prior" value and restore THAT in its finally, leaking
    the 4 MiB ceiling onto unrelated CSV parsing. The module lock makes
    set→parse→restore atomic, so the original is always what gets restored.
    """
    orig = csv.field_size_limit()
    # Force a distinct, small baseline so a leaked 4 MiB ceiling is unmistakable.
    sentinel = 131072
    csv.field_size_limit(sentinel)
    try:
        start = threading.Barrier(8)
        observed: list[int] = []
        lock = threading.Lock()

        def worker() -> None:
            start.wait()
            for _ in range(40):
                _merge_csv([("u.csv", "col\nx\ny\nz\n")], dedupe=True)
            with lock:
                observed.append(csv.field_size_limit())

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Every worker, after its merges, must see the sentinel restored — never
        # the raised _MAX_UNIT_BYTES ceiling leaked by a racing worker.
        assert observed, "no observations recorded"
        assert all(v == sentinel for v in observed), observed
        assert csv.field_size_limit() == sentinel
    finally:
        csv.field_size_limit(orig)


def test_csv_field_limit_lock_serializes_window():
    """The lock is held across the whole set→parse→restore body so two merges can
    never interleave their global mutations."""
    assert isinstance(assembly._CSV_FIELD_LIMIT_LOCK, type(threading.Lock()))


# ── LOW: TOC cap math must include framing bytes so it agrees with the body ────

def test_unit_headings_base_total_drops_unit_at_byte_cap(tmp_path, monkeypatch):
    """With ``base_total`` seeded near the total-byte cap, the LAST unit that the
    body would drop must also be dropped from the heading list — the TOC and body
    stop at the same unit."""
    # Shrink the caps so we can exercise the boundary cheaply.
    monkeypatch.setattr(assembly, "_MAX_TOTAL_BYTES", 200)
    monkeypatch.setattr(assembly, "_MAX_UNIT_BYTES", 200)

    sep = "\n\n"
    # Two units, ~80 bytes each.
    u1 = tmp_path / "u1.md"
    u2 = tmp_path / "u2.md"
    u1.write_text("# One\n" + "a" * 80)
    u2.write_text("# Two\n" + "b" * 80)

    # base_total=0: both units fit (80 + 80 + sep < 200) → both headings.
    both = _unit_headings(["u1.md", "u2.md"], tmp_path, separator=sep, base_total=0)
    assert both == ["One", "Two"]

    # base_total=80 (framing): now 80 + 80 + sep(2) = 162 fits u1, but u2 pushes to
    # 162 + 80 + 2 = 244 > 200 → body drops u2, so the TOC must drop it too.
    seeded = _unit_headings(
        ["u1.md", "u2.md"], tmp_path, separator=sep, base_total=80
    )
    assert seeded == ["One"]


def test_document_head_toc_excludes_unit_body_drops(tmp_path, monkeypatch):
    """End-to-end through ``_document_head``: a large title_page frame plus units
    that just exceed the cap must produce a TOC that omits the trailing unit the
    body would drop — no phantom TOC entry."""
    monkeypatch.setattr(assembly, "_MAX_TOTAL_BYTES", 300)
    monkeypatch.setattr(assembly, "_MAX_UNIT_BYTES", 300)

    u1 = tmp_path / "u1.md"
    u2 = tmp_path / "u2.md"
    u1.write_text("# Alpha\n" + "x" * 100)
    u2.write_text("# Beta\n" + "y" * 100)

    # A big title makes framing_bytes large enough that, combined with u1, u2 no
    # longer fits under the 300-byte cap.
    big_title = "T" * 90
    manifest = {"units": ["u1.md", "u2.md"]}
    out = _document_head(
        manifest, tmp_path, title=big_title, required_structure=("title", "toc"),
    )
    title_page = out.get("title_page", "")
    assert "## Contents" in title_page
    # Alpha (u1) survives; Beta (u2) is dropped by the body at the cap, so it must
    # NOT appear in the TOC.
    assert "Alpha" in title_page
    assert "Beta" not in title_page
