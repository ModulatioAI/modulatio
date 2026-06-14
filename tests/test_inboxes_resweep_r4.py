# SPDX-License-Identifier: Apache-2.0
"""Round-4 re-sweep regressions for src/modulatio/inboxes.py.

Finding 1 [LOW/error-path]: ``_load_jsonl`` opened JSONL with
``encoding="utf-8"`` and the default ``errors="strict"``. A non-UTF-8 byte
(a Latin-1 hand-edit or a torn write — the exact resilience scenarios the
module's docstrings cite) raised ``UnicodeDecodeError`` during ``for line in
fh`` iteration, OUTSIDE the per-line ``except json.JSONDecodeError``, so it
propagated out of every inbox reader. The fix opens with
``errors="replace"`` (the package-wide convention), so the bad byte degrades
to U+FFFD on that one line and the remaining valid rows survive.
"""

from __future__ import annotations

import json

from modulatio.inboxes import _load_jsonl, _load_notes


def _write_bytes(path, data: bytes) -> None:
    path.write_bytes(data)


def test_load_jsonl_survives_non_utf8_byte(tmp_path):
    # Two valid rows straddling one line that carries a raw Latin-1 byte
    # (0xe9 = "é" in Latin-1, an invalid standalone UTF-8 sequence).
    path = tmp_path / "inbox_candidates.jsonl"
    good_first = json.dumps({"id": "a", "body": "alpha"}).encode("utf-8")
    bad_middle = b'{"id": "b", "body": "caf\xe9"}'
    good_last = json.dumps({"id": "c", "body": "gamma"}).encode("utf-8")
    _write_bytes(path, good_first + b"\n" + bad_middle + b"\n" + good_last + b"\n")

    # Must NOT raise UnicodeDecodeError; the two clean rows survive.
    rows = _load_jsonl(path)
    ids = {r.get("id") for r in rows}
    assert "a" in ids
    assert "c" in ids
    # The corrupt-byte line decodes to U+FFFD and is still valid JSON here,
    # so it is preserved too — the contract is "don't brick the whole read".
    assert len(rows) >= 2


def test_load_jsonl_bad_byte_in_otherwise_unparseable_line(tmp_path):
    # A line whose bad byte lands inside otherwise-unparseable JSON: the
    # U+FFFD replacement keeps it a string the JSON parser rejects per-line,
    # so it is skipped — but the surrounding valid rows still load.
    path = tmp_path / "inbox.jsonl"
    good = json.dumps({"id": "keep"}).encode("utf-8")
    torn = b"not json at all \xff\xfe trailing"
    _write_bytes(path, good + b"\n" + torn + b"\n")

    rows = _load_jsonl(path)
    assert {"id": "keep"} in rows
    assert len(rows) == 1


def test_load_notes_survives_non_utf8_byte(tmp_path):
    # End-to-end through the recipient note reader: a hand-edited Latin-1
    # byte in a per-recipient file must not brick the whole inbox read.
    path = tmp_path / "recipient.jsonl"
    bad = b'{"note_id": "x", "garbled": "caf\xe9"}'
    _write_bytes(path, bad + b"\n")

    # Should not raise; best-effort row reconstruction (a row that does not
    # match InboxNote is skipped, but no UnicodeDecodeError escapes).
    notes = _load_notes(path)
    assert isinstance(notes, list)
