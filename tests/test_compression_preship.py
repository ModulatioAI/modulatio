"""Pre-ship 0.9.0 regression: compression audit append serializes under
an orchestrator-bound ``write_lock`` (parity with ``context_budget``).

Finding [LOW/race] compression.py:882 — the audit.jsonl append used no
write-lock, unlike ``context_budget`` which serializes via ``write_lock``.
Under concurrent wave workers this can tear rows on the shared audit file.
The fix threads an optional ``write_lock`` through ``_append_audit_row_0600``
and the ``emit_compaction*`` family and guards the append with it
(``nullcontext`` when unbound → prior behavior unchanged).
"""
from __future__ import annotations

import threading
from pathlib import Path

import pytest

from modulatio import compression


def test_append_audit_row_acquires_write_lock_when_bound(tmp_path: Path) -> None:
    """When a ``write_lock`` is passed, the append acquires it for the
    duration of the write — mirroring ``context_budget``'s idiom."""
    acquisitions = {"count": 0, "held_during_write": False}
    audit_path = tmp_path / "audit.jsonl"

    class _RecordingLock:
        def __init__(self) -> None:
            self._lock = threading.Lock()

        def __enter__(self):
            self._lock.acquire()
            acquisitions["count"] += 1
            # The audit file must NOT exist yet when the lock is taken —
            # proves the whole touch+chmod+open+write critical section runs
            # under the lock, not just a fragment.
            acquisitions["held_during_write"] = not audit_path.exists()
            return self

        def __exit__(self, *exc) -> None:
            self._lock.release()

    lock = _RecordingLock()
    compression._append_audit_row_0600(
        audit_path, {"event": "compaction_emit"}, lock,
    )

    assert acquisitions["count"] == 1
    assert acquisitions["held_during_write"] is True
    # The row landed exactly once.
    assert audit_path.read_text(encoding="utf-8").count("\n") == 1


def test_append_audit_row_no_lock_is_prior_behavior(tmp_path: Path) -> None:
    """Unbound (``write_lock=None``) path appends with no locking —
    the sequential / back-compat behavior is preserved."""
    audit_path = tmp_path / "audit.jsonl"
    compression._append_audit_row_0600(audit_path, {"event": "x"})
    compression._append_audit_row_0600(audit_path, {"event": "y"})
    assert audit_path.read_text(encoding="utf-8").count("\n") == 2


def test_append_audit_row_concurrent_appends_under_shared_lock_dont_tear(
    tmp_path: Path,
) -> None:
    """A shared lock serializes concurrent appends so every JSONL line is
    intact (no interleaved/torn rows) under parallel wave workers."""
    audit_path = tmp_path / "audit.jsonl"
    shared_lock = threading.Lock()
    n = 60

    def _worker(i: int) -> None:
        compression._append_audit_row_0600(
            audit_path, {"event": "compaction_emit", "i": i}, shared_lock,
        )

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    lines = [
        ln for ln in audit_path.read_text(encoding="utf-8").splitlines() if ln
    ]
    assert len(lines) == n
    import json

    seen = set()
    for ln in lines:
        row = json.loads(ln)  # each line is valid JSON (not torn)
        seen.add(row["i"])
    assert seen == set(range(n))


def test_append_audit_row_releases_lock_on_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Best-effort contract holds WITH a lock: a write failure is
    swallowed and the lock is released (via the context manager), so a
    subsequent append can still acquire it."""
    audit_path = tmp_path / "audit.jsonl"
    lock = threading.Lock()
    real_open = Path.open

    def explosive_open(self, *args, **kwargs):
        if self.name == "audit.jsonl":
            raise OSError("simulated disk-full")
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", explosive_open)
    # Must not raise and must not leave the lock held.
    compression._append_audit_row_0600(audit_path, {"event": "x"}, lock)
    assert lock.acquire(blocking=False) is True
    lock.release()
