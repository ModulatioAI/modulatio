# SPDX-License-Identifier: Apache-2.0
"""Pre-ship (0.9.0) regression tests for src/modulatio/inboxes.py.

Three findings from the final pre-public-ship sweep:

  - MEDIUM/race (inboxes.py:791): ``_emit_inbox_event`` wrote the audit
    JSONL row with its own unsynchronized open/write, bypassing
    ``_APPEND_LOCK`` despite the module's documented invariant that the
    lock serializes EVERY shared-file JSONL append. Under parallel wave
    workers two interleaved audit writes could tear a row.

  - LOW/resource-leak (inboxes.py:83): ``_WARNED_SOFT_CAP`` accumulated
    one entry per recipient PER RUN and was never cleared, so a
    long-lived daemon serving many runs grew it without bound. Now
    hard-capped.

  - LOW/race (inboxes.py:1366): candidate abandonment in
    ``list_pending_candidates`` and ``sweep_abandoned_candidates`` did an
    unguarded read-check-write on the terminal set, so the two paths
    running for the same candidate/turn could double-emit the terminal
    state + audit rows. Now atomic under ``_APPEND_LOCK`` with a re-read.

Each test fails against the pre-fix code and passes after.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from unittest import mock

import pytest

from modulatio import inboxes


@pytest.fixture
def run_dir(tmp_path: Path) -> Path:
    return tmp_path / "run"


@pytest.fixture
def audit_path(run_dir: Path) -> Path:
    return run_dir / "audit.jsonl"


@pytest.fixture(autouse=True)
def _clear_warned_soft_cap():
    inboxes._WARNED_SOFT_CAP.clear()
    yield
    inboxes._WARNED_SOFT_CAP.clear()


def _propose(
    *, run_dir: Path, audit_path: Path, content: str = "c", turn: int = 1
) -> "inboxes.InboxCandidate":
    cand = inboxes.propose(
        source_agent_id="drafter-1", source_role="drafter",
        target_scope="agent", target_agent_id="leader",
        target_runner_role=None, priority="P1",
        reason="constraint_discovered", content=content,
        project_code="tst", run_id="run-1", turn=turn,
        run_dir=run_dir, audit_path=audit_path,
    )
    assert cand is not None
    return cand


# ── MEDIUM: audit-write goes under _APPEND_LOCK ──────────────────────────


def test_emit_inbox_event_holds_append_lock(
    run_dir: Path, audit_path: Path,
) -> None:
    """The audit write must acquire _APPEND_LOCK. We assert it by
    swapping in a sentinel lock and confirming it is entered around the
    write (pre-fix the write bypassed the lock entirely)."""
    entered = {"n": 0}

    real_lock = inboxes._APPEND_LOCK

    class _SpyLock:
        def __enter__(self):
            entered["n"] += 1
            return real_lock.__enter__()

        def __exit__(self, *a):
            return real_lock.__exit__(*a)

    with mock.patch.object(inboxes, "_APPEND_LOCK", _SpyLock()):
        inboxes._emit_inbox_event(
            audit_path=audit_path, event="propose_emit", current_turn=1,
        )

    assert entered["n"] >= 1
    # Row still landed and parses.
    rows = [
        json.loads(ln)
        for ln in audit_path.read_text().splitlines()
        if ln.strip()
    ]
    assert any(r.get("event") == "propose_emit" for r in rows)


def test_concurrent_audit_emits_do_not_tear_jsonl(
    run_dir: Path, audit_path: Path,
) -> None:
    """Parallel wave-worker audit emissions must not interleave and tear
    a JSONL line — every row parses cleanly."""
    inboxes._ensure_parent_0700(audit_path)
    barrier = threading.Barrier(16)

    def worker(i: int) -> None:
        barrier.wait()
        for _ in range(20):
            inboxes._emit_inbox_event(
                audit_path=audit_path,
                event="propose_emit",
                current_turn=i,
            )

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    raw = [ln for ln in audit_path.read_text().splitlines() if ln.strip()]
    assert len(raw) == 16 * 20
    for ln in raw:
        json.loads(ln)  # would raise on a torn line


# ── LOW: _WARNED_SOFT_CAP is bounded ─────────────────────────────────────


def test_warned_soft_cap_is_bounded(
    run_dir: Path, audit_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """Simulate many runs' worth of dedup entries: the set must not grow
    past the cap (pre-fix it grew without bound)."""
    cap = inboxes._WARNED_SOFT_CAP_MAX
    # Pre-load the set right up to the cap with synthetic prior-run keys.
    for i in range(cap):
        inboxes._WARNED_SOFT_CAP.add((f"run-{i}", "runner_role", None, "leader"))
    assert len(inboxes._WARNED_SOFT_CAP) == cap

    # One more distinct recipient in the soft-cap band must not push the
    # set over the ceiling.
    inboxes._soft_cap_warn_once(
        run_id="run-new", target_scope="runner_role",
        target_agent_id=None, target_runner_role="leader",
        live_count=8, soft_cap=8, hard_cap=12,
    )
    assert len(inboxes._WARNED_SOFT_CAP) <= cap


# ── LOW: candidate abandonment double-emit ───────────────────────────────


def _abandon_terminal_rows(run_dir: Path, cid: str) -> int:
    rows = inboxes._load_jsonl(inboxes.candidate_terminals_path(run_dir))
    return sum(
        1 for r in rows
        if r.get("candidate_id") == cid and r.get("terminal") == "abandoned"
    )


def _abandon_audit_rows(audit_path: Path, cid: str) -> int:
    if not audit_path.exists():
        return 0
    rows = [
        json.loads(ln)
        for ln in audit_path.read_text().splitlines()
        if ln.strip()
    ]
    return sum(
        1 for r in rows
        if r.get("candidate_id") == cid
        and r.get("event") == "propose_abandoned"
    )


def test_list_then_sweep_does_not_double_emit_abandon(
    run_dir: Path, audit_path: Path,
) -> None:
    """list_pending_candidates and sweep_abandoned_candidates running for
    the same candidate/turn must record the terminal + audit rows exactly
    once total — not once each."""
    cand = _propose(run_dir=run_dir, audit_path=audit_path, turn=1)
    # current_turn=4 is >= ABANDON_AFTER_TURNS (3) past created turn 1.
    inboxes.list_pending_candidates(
        run_dir=run_dir, audit_path=audit_path, current_turn=4,
    )
    inboxes.sweep_abandoned_candidates(
        run_dir=run_dir, audit_path=audit_path, current_turn=4,
    )

    assert _abandon_terminal_rows(run_dir, cand.candidate_id) == 1
    assert _abandon_audit_rows(audit_path, cand.candidate_id) == 1


def test_sweep_count_excludes_already_abandoned(
    run_dir: Path, audit_path: Path,
) -> None:
    """sweep returns the number IT transitioned: after list_pending has
    already abandoned the candidate, a following sweep must count 0."""
    _propose(run_dir=run_dir, audit_path=audit_path, turn=1)
    inboxes.list_pending_candidates(
        run_dir=run_dir, audit_path=audit_path, current_turn=4,
    )
    n = inboxes.sweep_abandoned_candidates(
        run_dir=run_dir, audit_path=audit_path, current_turn=4,
    )
    assert n == 0


def test_concurrent_abandon_emits_once(
    run_dir: Path, audit_path: Path,
) -> None:
    """Race list_pending_candidates against sweep_abandoned_candidates on
    the same candidate/turn from parallel threads: exactly one terminal
    row and one audit row total."""
    cand = _propose(run_dir=run_dir, audit_path=audit_path, turn=1)
    barrier = threading.Barrier(8)
    errors: list[BaseException] = []

    def via_list() -> None:
        try:
            barrier.wait()
            inboxes.list_pending_candidates(
                run_dir=run_dir, audit_path=audit_path, current_turn=4,
            )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def via_sweep() -> None:
        try:
            barrier.wait()
            inboxes.sweep_abandoned_candidates(
                run_dir=run_dir, audit_path=audit_path, current_turn=4,
            )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [
        threading.Thread(target=via_list if i % 2 == 0 else via_sweep)
        for i in range(8)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"abandon raced: {errors!r}"
    assert _abandon_terminal_rows(run_dir, cand.candidate_id) == 1
    assert _abandon_audit_rows(audit_path, cand.candidate_id) == 1
