"""0.9.0 pre-ship re-sweep (round 3) regressions for ``modulatio.inboxes``.

Dedicated file (does not touch ``tests/test_inboxes.py`` or the round-1/2
re-sweep files) covering ONE finding:

  1. ``_load_tombstoned_ids`` indexed ``row["note_id"]`` with no guard. A
     well-formed JSON object lacking ``note_id`` (schema drift, hand-edit,
     a future tombstone format, a field rename) passed ``_load_jsonl``'s
     JSON-decode-only tolerance and KeyError'd, silently disabling EVERY
     inbox read / enqueue for the run. Every sibling reader in the module
     is per-row best-effort; this one was not.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from modulatio import inboxes


@pytest.fixture
def run_dir(tmp_path: Path) -> Path:
    return tmp_path / "run"


@pytest.fixture
def audit_path(run_dir: Path) -> Path:
    return run_dir / "audit.jsonl"


def _append_raw(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")


# ── finding 1: schema-drifted tombstone row must not brick reads ──────────


def test_load_tombstoned_ids_skips_schema_drifted_row(run_dir: Path) -> None:
    """A well-formed JSONL row that LACKS ``note_id`` must be skipped, not
    raise. Before the fix this KeyError'd and took every inbox read down
    with it."""
    tomb = inboxes.tombstones_path(run_dir)
    _append_raw(tomb, {"note_id": "note-aaaaaaaaaaaa", "reason": "decayed"})
    # Schema drift: well-formed JSON object, no ``note_id``.
    _append_raw(tomb, {"reason": "decayed", "tombstoned_at_turn": 4})
    # A row where note_id is present-but-falsy must also be skipped.
    _append_raw(tomb, {"note_id": "", "reason": "decayed"})

    ids = inboxes._load_tombstoned_ids(tomb)
    assert ids == {"note-aaaaaaaaaaaa"}


def test_read_for_dispatch_survives_drifted_tombstone(
    run_dir: Path, audit_path: Path
) -> None:
    """End-to-end: a live note is enqueued, then a schema-drifted
    tombstone row is hand-injected. The read path loads tombstoned ids;
    before the fix it KeyError'd, hiding the live note entirely."""
    note = inboxes.enqueue(
        source_agent_id="qc-1",
        source_role="qc",
        target_scope="runner_role",
        target_runner_role="writers",
        priority="P0",
        reason="constraint_discovered",
        content="hold the line",
        project_code="proj",
        run_id="run-1",
        turn=0,
        run_dir=run_dir,
        audit_path=audit_path,
    )
    assert note is not None

    # Inject a schema-drifted tombstone (no note_id) — e.g. a future
    # tombstone format or a hand-edit.
    _append_raw(inboxes.tombstones_path(run_dir), {"reason": "decayed", "foo": 1})

    notes = inboxes.read_for_dispatch(
        target_runner_role="writers",
        project_code="proj",
        run_id="run-1",
        current_turn=0,
        run_dir=run_dir,
        audit_path=audit_path,
    )
    assert [n.note_id for n in notes] == [note.note_id]


def test_enqueue_survives_drifted_tombstone(
    run_dir: Path, audit_path: Path
) -> None:
    """The enqueue path also loads tombstoned ids (for decay + supersede).
    A schema-drifted tombstone row must not block a subsequent enqueue."""
    inboxes.enqueue(
        source_agent_id="leader-1",
        source_role="leader",
        target_scope="runner_role",
        target_runner_role="writers",
        priority="P1",
        reason="scope_clarification",
        content="first",
        project_code="proj",
        run_id="run-1",
        turn=0,
        run_dir=run_dir,
        audit_path=audit_path,
    )
    _append_raw(inboxes.tombstones_path(run_dir), {"reason": "decayed"})

    second = inboxes.enqueue(
        source_agent_id="leader-1",
        source_role="leader",
        target_scope="runner_role",
        target_runner_role="writers",
        priority="P1",
        reason="scope_clarification",
        content="second",
        project_code="proj",
        run_id="run-1",
        turn=1,
        run_dir=run_dir,
        audit_path=audit_path,
    )
    assert second is not None
