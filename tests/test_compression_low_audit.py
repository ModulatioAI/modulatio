# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""LOW-audit regression tests for ``modulatio.compression``.

Covers finding #54 (LOW): ``emit_compaction`` had no write-lock guarding
the read-version → write-manifest critical section, so two concurrent
compaction attempts on the same ``run_dir`` could both read the same
``latest_version``, allocate the same next version, and clobber each
other's state/diff/parsed/manifest quadruple (corrupting the monotonic
version sequence). The fix wraps steps 4–9 in a per-run exclusive
``flock`` (``compression._compaction_lock``).

This file is uniquely named to avoid colliding with other concurrent
LOW-audit agents editing ``tests/test_compression.py``.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from modulatio import compression
from modulatio.compression import (
    emit_compaction,
    read_manifest,
    state_version_path,
)


def _well_formed_state(**overrides) -> dict:
    base = {
        "compressed_active_goal": "Ship goal-state compression",
        "active_sub_objectives": ["SO-1 write compression module"],
        "key_decisions": ["audit-best-effort vs state-authoritative"],
        "current_focus": "land the lock",
        "open_blockers": [],
        "recent_activity": [
            {"at": "11:50", "agent": "leader", "claim": "wrote seed"},
        ],
        "deferred_items": [],
        "non_goals": [],
        "reason_code": "sub_objective_completed",
        "reason_note": "routine boundary compaction",
    }
    base.update(overrides)
    return base


def _emit(run_dir: Path, audit_path: Path, focus: str) -> None:
    emit_compaction(
        project_code="STA",
        run_id="run-race",
        plan_id="plan-race",
        after_sub_objective=1,
        run_dir=run_dir,
        audit_path=audit_path,
        call_scope_id="scope",
        call_id=None,
        model=None,
        # Distinct body per call so neither short-circuits on
        # no_material_change — both must allocate a real version.
        parsed_state=_well_formed_state(current_focus=focus),
        original_user_goal_source="Ship goal-state compression",
    )


def test_concurrent_emit_compaction_does_not_clobber_versions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two threads racing emit_compaction on the SAME run_dir must
    allocate DISTINCT versions (1 and 2), not collide on one.

    We pin the race window deterministically: a barrier in
    ``_write_state_version`` makes the first writer pause AFTER it has
    allocated its version but BEFORE the manifest commits, while the
    second thread runs ahead to the same point. Without the per-run
    lock both threads read latest_version=None at step 4, both allocate
    v1, and one clobbers the other (manifest ends at v1, no v2, both
    audit rows claim version 1). With the lock the second thread blocks
    at step 4 until the first commits, sees latest_version=1, and
    allocates v2.
    """
    run_dir = tmp_path / "run"
    audit_path = run_dir / "audit.jsonl"

    real_write_state = compression._write_state_version
    # Short timeout: in the FIXED case the lock serializes the two
    # attempts so the second never reaches the barrier — the first
    # writer simply waits out this timeout once, then both commit
    # cleanly. In the BUGGY case both reach the barrier and release
    # immediately. Either way the test is fast.
    barrier = threading.Barrier(2, timeout=1.5)
    _tripped = {"done": False}

    def _slow_write_state(rd: Path, version: int, body: str):  # noqa: ANN202
        # First writer into the critical section pauses here (after
        # version allocation, before the manifest write) until the
        # other thread reaches the same point — the exact clobber
        # window the lock must close. Only the first pair waits.
        if not _tripped["done"]:
            _tripped["done"] = True
            try:
                barrier.wait()
            except threading.BrokenBarrierError:
                pass
        return real_write_state(rd, version, body)

    monkeypatch.setattr(compression, "_write_state_version", _slow_write_state)

    errors: list[BaseException] = []

    def _worker(focus: str) -> None:
        try:
            _emit(run_dir, audit_path, focus)
        except BaseException as exc:  # noqa: BLE001 - surface to assert
            errors.append(exc)

    t1 = threading.Thread(target=_worker, args=("focus-A",))
    t2 = threading.Thread(target=_worker, args=("focus-B",))
    t1.start()
    t2.start()
    t1.join(timeout=15)
    t2.join(timeout=15)

    assert not errors, f"worker raised: {errors!r}"

    # Both versions landed; the sequence is monotonic and uncorrupted.
    manifest = read_manifest(run_dir)
    assert manifest is not None
    assert manifest.latest_version == 2
    assert state_version_path(run_dir, 1).exists()
    assert state_version_path(run_dir, 2).exists()

    # Two compaction_emit rows — one per attempt, neither lost.
    rows = [
        json.loads(line)
        for line in audit_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    emits = [r for r in rows if r["event"] == "compaction_emit"]
    assert len(emits) == 2
    versions = sorted(r["compaction_version"] for r in emits)
    assert versions == [1, 2]


def test_compaction_lock_serializes_holders(tmp_path: Path) -> None:
    """The per-run lock is a real mutual-exclusion guard: while one
    holder is inside the context, a second acquisition blocks."""
    if compression.fcntl is None:  # pragma: no cover - non-POSIX
        pytest.skip("flock unavailable on this platform")

    run_dir = tmp_path / "run"
    entered = threading.Event()
    second_acquired = threading.Event()
    release = threading.Event()

    def _holder() -> None:
        with compression._compaction_lock(run_dir):
            entered.set()
            # Hold until the test confirms the second waiter is blocked.
            release.wait(timeout=5)

    def _waiter() -> None:
        with compression._compaction_lock(run_dir):
            second_acquired.set()

    h = threading.Thread(target=_holder)
    h.start()
    assert entered.wait(timeout=5)

    w = threading.Thread(target=_waiter)
    w.start()

    # The waiter must NOT acquire while the holder is inside.
    assert not second_acquired.wait(timeout=0.5)

    # Release the holder; now the waiter proceeds.
    release.set()
    assert second_acquired.wait(timeout=5)
    h.join(timeout=5)
    w.join(timeout=5)
