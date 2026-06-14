"""Round-2 full-debug regression tests for src/modulatio/inboxes.py.

Covers two findings from the post-0.9.0 re-debug:

  - MEDIUM/race: the read-side decay pass (render_for_prompt ->
    read_for_dispatch -> _tombstone_expired) is reached from parallel
    wave workers and was unsynchronized: N concurrent reads of the same
    expired note each appended a duplicate ``decayed`` tombstone, and
    interleaved appends from separate handles could tear a JSONL line.
    Now serialized + dedup-against-disk under _APPEND_LOCK.

  - LOW/error-path: _load_notes deserialized via InboxNote(**row) with
    no per-row guard, so one malformed row bricked a recipient's whole
    inbox read (unlike the candidate path, which skips bad rows).

These tests fail against the pre-fix code and pass after.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from modulatio import inboxes


# ── fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture
def run_dir(tmp_path: Path) -> Path:
    return tmp_path / "run"


@pytest.fixture
def audit_path(run_dir: Path) -> Path:
    return run_dir / "audit.jsonl"


def _enqueue(
    *,
    run_dir: Path,
    audit_path: Path,
    target_runner_role: str,
    priority: str = "P2",
    content: str = "note",
    turn: int = 1,
) -> "inboxes.InboxNote":
    note = inboxes.enqueue(
        source_agent_id="leader",
        source_role="leader",
        target_scope="runner_role",  # type: ignore[arg-type]
        target_agent_id=None,
        target_runner_role=target_runner_role,
        priority=priority,  # type: ignore[arg-type]
        reason="constraint_discovered",
        content=content,
        project_code="tst",
        run_id="run-1",
        turn=turn,
        run_dir=run_dir,
        audit_path=audit_path,
    )
    assert note is not None
    return note


# ── LOW: _load_notes per-row guard ───────────────────────────────────────


def test_load_notes_skips_malformed_row(run_dir: Path, audit_path: Path) -> None:
    """A single malformed note row (schema drift / hand-edit / a field
    the dataclass doesn't accept) must be skipped, not crash the whole
    recipient load — mirroring the candidate path. Before the fix this
    raised TypeError out of _load_notes."""
    good = _enqueue(
        run_dir=run_dir, audit_path=audit_path,
        target_runner_role="researcher", content="keep-me", turn=1,
    )
    path = inboxes.role_inbox_path(run_dir, "researcher")
    # Append a row with an unknown key — InboxNote(**row) would raise.
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"note_id": "bad", "totally_unknown_field": 1}) + "\n")
    # And a row missing required fields entirely.
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"note_id": "bad2"}) + "\n")

    notes = inboxes._load_notes(path)

    ids = {n.note_id for n in notes}
    assert good.note_id in ids
    assert "bad" not in ids and "bad2" not in ids
    assert len(notes) == 1


def test_read_for_dispatch_survives_malformed_note_row(
    run_dir: Path, audit_path: Path,
) -> None:
    """End-to-end: a bad row in a recipient file must not brick the
    dispatch read for the valid notes."""
    _enqueue(
        run_dir=run_dir, audit_path=audit_path,
        target_runner_role="researcher", content="live", turn=1, priority="P0",
    )
    path = inboxes.role_inbox_path(run_dir, "researcher")
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"note_id": "x", "garbage": True}) + "\n")

    notes = inboxes.read_for_dispatch(
        target_runner_role="researcher",
        project_code="tst",
        run_id="run-1",
        current_turn=1,
        run_dir=run_dir,
        audit_path=audit_path,
    )
    assert [n.content for n in notes] == ["live"]


# ── MEDIUM: read-side decay race ─────────────────────────────────────────


def _count_decayed_tombstones(run_dir: Path, note_id: str) -> int:
    rows = inboxes._load_jsonl(inboxes.tombstones_path(run_dir))
    return sum(
        1 for r in rows
        if r.get("note_id") == note_id and r.get("reason") == "decayed"
    )


def test_concurrent_reads_write_one_decayed_tombstone(
    run_dir: Path, audit_path: Path,
) -> None:
    """N parallel wave-worker reads of the same expired note must append
    exactly ONE ``decayed`` tombstone, not one per thread. Before the
    fix each thread loaded the tombstoned set fresh and independently
    appended a duplicate row."""
    # P2 created at turn 1 is visible at T1/T2, expired at T>=3.
    note = _enqueue(
        run_dir=run_dir, audit_path=audit_path,
        target_runner_role="researcher", priority="P2", turn=1,
    )

    barrier = threading.Barrier(8)
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            barrier.wait()
            inboxes.read_for_dispatch(
                target_runner_role="researcher",
                project_code="tst",
                run_id="run-1",
                current_turn=5,  # past P2 decay window
                run_dir=run_dir,
                audit_path=audit_path,
            )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"reads raised: {errors!r}"
    assert _count_decayed_tombstones(run_dir, note.note_id) == 1


def test_concurrent_appends_do_not_tear_jsonl(
    run_dir: Path, audit_path: Path,
) -> None:
    """Interleaved appends from parallel workers must not tear a JSONL
    line. Every row in the tombstones file must parse cleanly."""
    inboxes._ensure_parent_0700(inboxes.tombstones_path(run_dir))
    path = inboxes.tombstones_path(run_dir)

    barrier = threading.Barrier(16)
    payload = {"note_id": "n", "reason": "decayed", "filler": "x" * 500}

    def worker(i: int) -> None:
        barrier.wait()
        for _ in range(20):
            inboxes._append_jsonl_row(path, {**payload, "i": i})

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    raw_lines = [ln for ln in path.read_text().splitlines() if ln.strip()]
    assert len(raw_lines) == 16 * 20
    # Every line parses — no torn/interleaved JSON.
    for ln in raw_lines:
        json.loads(ln)
