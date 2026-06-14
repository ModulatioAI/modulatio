"""Re-sweep regression: BudgetTracker.record() must be atomic across
concurrently sharing wave workers.

One BudgetTracker instance is deliberately shared across the default-on
concurrent wave path (orchestration.py copies the ContextVar BINDING,
not the object, into each ThreadPoolExecutor future). The accumulation
in record() was a non-atomic read-modify-write (``+=``), so concurrent
calls lost increments and under-counted spend, weakening the token/cost
caps. These tests assert the totals are exact under contention and the
per-line JSONL telemetry stays self-consistent and un-interleaved.
"""

import json
import threading
from pathlib import Path

import pytest

from modulatio import budget
from tests._thread_check import run_threads_checked


def test_record_is_atomic_under_concurrent_shared_workers():
    """Many threads sharing ONE tracker must not lose any increment.

    Without the lock the non-atomic ``+=`` drops updates and the totals
    come in BELOW the exact expected sum (flaky-but-real). With the lock
    they are exact every run.
    """
    tracker = budget.BudgetTracker()

    n_threads = 16
    calls_per_thread = 500
    in_t, out_t, cost = 7, 3, 0.0001

    start = threading.Barrier(n_threads)

    def worker():
        start.wait()  # maximize collision window
        for _ in range(calls_per_thread):
            tracker.record(input_tokens=in_t, output_tokens=out_t, cost_usd=cost)

    run_threads_checked([worker] * n_threads)

    total_calls = n_threads * calls_per_thread
    assert tracker.tokens_used == total_calls * (in_t + out_t)
    assert tracker.cost_usd_used == pytest.approx(total_calls * cost)


def test_concurrent_log_lines_are_intact_and_consistent(tmp_path: Path):
    """Concurrent record() with a log_path must produce exactly one
    valid JSONL line per call (no interleaving) and every line's running
    totals must be drawn from a consistent snapshot."""
    log_path = tmp_path / "budget.jsonl"
    tracker = budget.BudgetTracker(log_path=log_path)

    n_threads = 12
    calls_per_thread = 200
    in_t, out_t, cost = 5, 5, 0.002

    start = threading.Barrier(n_threads)

    def worker(name: str):
        start.wait()
        for _ in range(calls_per_thread):
            tracker.record(
                input_tokens=in_t, output_tokens=out_t,
                cost_usd=cost, model=name,
            )

    run_threads_checked([(lambda i=i: worker(f"m{i}")) for i in range(n_threads)])

    total_calls = n_threads * calls_per_thread
    lines = log_path.read_text().splitlines()
    assert len(lines) == total_calls  # no dropped / merged lines

    # Every line is valid JSON (no interleaved/garbled writes) and the
    # cumulative token totals form the exact arithmetic progression, so
    # no increment was lost or double-counted under contention.
    totals = []
    for ln in lines:
        rec = json.loads(ln)  # raises if a line was garbled
        assert rec["input_tokens"] == in_t
        assert rec["output_tokens"] == out_t
        totals.append(rec["tokens_total"])

    per_call = in_t + out_t
    assert sorted(totals) == [
        per_call * k for k in range(1, total_calls + 1)
    ]
    assert tracker.tokens_used == total_calls * per_call


def test_lock_excluded_from_equality_and_construction():
    """The synchronization field must stay invisible to the dataclass
    contract: two equal-valued trackers compare equal, and the lock is
    not a constructor argument."""
    a = budget.BudgetTracker(max_tokens=100, tokens_used=10)
    b = budget.BudgetTracker(max_tokens=100, tokens_used=10)
    assert a == b  # lock has compare=False
    # Lock auto-created, not a positional/kw constructor arg.
    assert isinstance(a._lock, type(threading.Lock()))
