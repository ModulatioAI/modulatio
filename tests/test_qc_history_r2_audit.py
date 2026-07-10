"""Round-2 audit regression tests for qc_history error-path resilience.

Covers the MEDIUM finding: `_parse_verdict_file` did a bare `read_text()`
with no encoding/errors arg and no guard, so a single non-UTF-8 / mangled
history file raised UnicodeDecodeError (a ValueError) out of
`load_verdicts`, bricking the whole domain's precedent — and through it the
QC read path (`similar_verdicts` -> `_qc_review`) and the lessons
codification loop that consume it.

The docstring explicitly invites humans to hand-prune / drop in history
files, so a mangled drop-in is plausible; one bad file must degrade to a
best-effort parse, never crash the listing.

Uniquely-named file to avoid colliding with a sibling agent editing
tests/test_qc_history.py concurrently. Mirrors that file's fixtures.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from modulatio import config, qc_history, vault


PROJECT_CODE = "TST"


@pytest.fixture
def project(tmp_path: Path, monkeypatch) -> str:
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path / "vault")
    monkeypatch.setattr(config, "get_cache_root", lambda: tmp_path / "cache")
    vault.init_project(PROJECT_CODE, "Test", "test")
    return PROJECT_CODE


def test_load_verdicts_does_not_crash_on_non_utf8_file(project):
    """A non-UTF-8 / binary .md in the domain dir must not raise
    UnicodeDecodeError out of load_verdicts. Before the fix the bare
    read_text() raised and aborted the whole listing."""
    qc_history.append_verdict("essay", project, _record("good-1"))

    domain_dir = qc_history._domain_dir("essay", project)
    # A file whose bytes are not valid UTF-8 (truncated multibyte / binary
    # blob saved with a .md name). Use a sortable-later name so the good
    # record still loads regardless of iteration order.
    (domain_dir / "zzz__binary.md").write_bytes(b"\xff\xfe binary garbage \x80\x81")

    # Must not raise.
    loaded = qc_history.load_verdicts("essay", project)

    # The valid record is still returned; the bad file degraded to a
    # best-effort parse (errors="replace") rather than bricking the listing.
    assert "good-1" in [r.entry_id for r in loaded]


def test_load_verdicts_skips_file_that_vanishes_mid_read(project, monkeypatch):
    """A transient OSError (file vanished between glob and read, permission
    hiccup) on one file must be skipped, not propagated — the rest of the
    domain's precedent must still load."""
    qc_history.append_verdict("essay", project, _record("keep-1"))
    qc_history.append_verdict("essay", project, _record("keep-2"))

    real_parse = qc_history._parse_verdict_file
    calls = {"n": 0}

    def flaky_parse(path: Path):
        calls["n"] += 1
        # Fail the first file with an OSError; parse the rest normally.
        if calls["n"] == 1:
            raise FileNotFoundError(path)
        return real_parse(path)

    monkeypatch.setattr(qc_history, "_parse_verdict_file", flaky_parse)

    loaded = qc_history.load_verdicts("essay", project)
    # One file was dropped; the other still loaded — no crash.
    assert len(loaded) == 1
    assert loaded[0].entry_id in {"keep-1", "keep-2"}


def _record_with(entry_id: str, timestamp: str) -> "qc_history.VerdictRecord":
    rec = _record(entry_id)
    return qc_history.VerdictRecord(
        entry_id=rec.entry_id,
        timestamp=timestamp,
        task_id=rec.task_id,
        producer_agent=rec.producer_agent,
        qc_agent=rec.qc_agent,
        verdict=rec.verdict,
        defect_type=rec.defect_type,
        rationale=rec.rationale,
        artifact_body=rec.artifact_body,
    )


def test_same_second_verdicts_load_in_append_order(project):
    """LOW finding: timestamps are stamped at second resolution, so verdicts
    appended within the same second share a filename prefix. The entry_id tail
    is a random uuid, which sorts arbitrarily — not in append order. The
    monotonic per-append sequence wedged into the filename must make
    load_verdicts (sorted glob) reflect true append order within a second.

    The entry_ids here are deliberately chosen so that a plain
    ``{timestamp}__{entry_id}`` sort would invert append order: the first
    appended record has the lexicographically-largest tail.
    """
    ts = "2026-04-21T12:00:00Z"  # identical second for all three
    append_order = ["zzz-first", "mmm-second", "aaa-third"]
    for eid in append_order:
        qc_history.append_verdict("essay", project, _record_with(eid, ts))

    loaded = qc_history.load_verdicts("essay", project)
    assert [r.entry_id for r in loaded] == append_order


def test_verdict_filename_has_sortable_seq_tiebreaker(project):
    """The monotonic sequence segment must be present and strictly increasing
    across appends so filenames sort by append order, independent of the
    entry_id tail."""
    ts = "2026-04-21T12:00:00Z"
    qc_history.append_verdict("essay", project, _record_with("zzz", ts))
    qc_history.append_verdict("essay", project, _record_with("aaa", ts))

    names = sorted(p.name for p in qc_history._domain_dir("essay", project).glob("*.md"))
    # Filenames sort with the zzz-tailed (first appended) file first because
    # its seq segment is smaller, despite "zzz" > "aaa" lexically.
    assert names[0].endswith("zzz.md")
    assert names[1].endswith("aaa.md")
