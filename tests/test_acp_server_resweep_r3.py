# SPDX-License-Identifier: Apache-2.0
"""Round-3 re-sweep regressions for ``modulatio.acp.server``.

Finding 2 (LOW/resource-leak): a FIFO / device inside an allowed attachment
root passed validation, then read_text() blocked the worker thread forever.
_validate_attachment_path now requires a regular file.

(Finding 1 — converse() per-instance lock across ACP sessions — was triaged a
skip: a server-level lock around the whole converse() serializes the
INTERACTIVE permission wait, which deadlocks the test-asserted H1 contract that
two sessions hold in-flight permission requests at once. The real fix for the
durable-log interleave is a per-project-code/file lock around the load+append
window INSIDE orchestration.converse() — a cross-file change, out of this
module's scope. See the structured report.)
"""
from __future__ import annotations

import os

import pytest

from modulatio.acp.server import _validate_attachment_path


# ── Finding 2: non-regular files (FIFO/device) rejected at validation ───────

def test_fifo_inside_root_rejected(tmp_path, monkeypatch):
    """A named pipe placed inside an allowed attachment root must be rejected
    by _validate_attachment_path — reading it would block the worker forever.
    Before the fix the FIFO passed (confinement + dotfile checks only)."""
    monkeypatch.setenv("MODULATIO_ACP_ATTACHMENT_ROOTS", str(tmp_path))
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)

    with pytest.raises(ValueError, match="not a regular file"):
        _validate_attachment_path(str(fifo))


def test_directory_inside_root_rejected(tmp_path, monkeypatch):
    """A directory inside an allowed root is also not a regular file → rejected
    before any read attempt."""
    monkeypatch.setenv("MODULATIO_ACP_ATTACHMENT_ROOTS", str(tmp_path))
    sub = tmp_path / "subdir"
    sub.mkdir()

    with pytest.raises(ValueError, match="not a regular file"):
        _validate_attachment_path(str(sub))


def test_regular_file_inside_root_still_accepted(tmp_path, monkeypatch):
    """The fix must not regress the happy path: an ordinary file inside an
    allowed root still validates and returns its resolved path."""
    monkeypatch.setenv("MODULATIO_ACP_ATTACHMENT_ROOTS", str(tmp_path))
    f = tmp_path / "note.txt"
    f.write_text("hello", encoding="utf-8")

    resolved = _validate_attachment_path(str(f))
    assert resolved == f.resolve()
