"""The hard kill-boundary: no seat call outlives its wall-clock deadline.

``_hard_deadline`` runs the guarded call in a disposable daemon thread
(ContextVars copied), joins with timeout+grace, and on expiry captures an
all-threads stack dump to the crash log, accounts the abandoned zombie, and
raises ``SeatCallHardTimeout`` — an availability-class failure the proven
recovery train (fallback chain, backoff, seat cooldown, QC backstop) already
knows how to swallow.

Born from the cb6c0d wedge: a tool-loop completion spun silently for 16m54s
with nothing bounding it; the Stage-0 diagnoser never covered that seam.
"""
from __future__ import annotations

import time

import pytest

from modulatio import budget, logstore
from modulatio.runners import (
    SeatCallHardTimeout,
    _hard_deadline,
    is_availability_error,
)


@pytest.fixture
def crash_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("MODULATIO_CRASH_DIR", str(tmp_path / "crashes"))
    return tmp_path / "crashes"


@pytest.fixture
def zero_grace(monkeypatch):
    monkeypatch.setattr("modulatio.runners._HARD_DEADLINE_GRACE_S", 0.0)


def test_transparent_result_when_fn_finishes_in_time(zero_grace):
    guarded = _hard_deadline(
        lambda a, b=1: a + b, timeout_s=5.0, describe="adder")
    assert guarded(2, b=3) == 5


def test_transparent_exception_preserves_type_and_args(zero_grace):
    def _boom():
        raise ValueError("original", 42)

    guarded = _hard_deadline(_boom, timeout_s=5.0, describe="boom")
    with pytest.raises(ValueError) as exc:
        guarded()
    assert exc.value.args == ("original", 42)


def test_expiry_raises_hard_timeout_fast(zero_grace, crash_dir):
    guarded = _hard_deadline(
        lambda: time.sleep(5), timeout_s=0.1, describe="wedge sim")
    start = time.monotonic()
    with pytest.raises(SeatCallHardTimeout) as exc:
        guarded()
    assert time.monotonic() - start < 2.0     # released at the deadline
    assert "wedge sim" in str(exc.value)
    assert "0.1" in str(exc.value)


def test_hard_timeout_is_availability_class(zero_grace):
    assert is_availability_error(SeatCallHardTimeout("seat x")) is True


def test_contextvars_ride_into_the_helper_thread(zero_grace):
    """The budget tracker is a ContextVar binding — the guarded call must see
    it (copy_context) and its usage must land on the caller's tracker."""
    def _spend():
        budget.record_usage(input_tokens=60, output_tokens=40, cost_usd=0.0)
        return "done"

    guarded = _hard_deadline(_spend, timeout_s=5.0, describe="spender")
    tracker = budget.BudgetTracker()
    with budget.with_tracker(tracker):
        assert guarded() == "done"
    assert tracker.tokens_used == 100


def test_expiry_dumps_all_thread_stacks_to_the_crash_log(zero_grace, crash_dir):
    guarded = _hard_deadline(
        lambda: time.sleep(5), timeout_s=0.1, describe="dumping wedge")
    with pytest.raises(SeatCallHardTimeout):
        guarded()
    logs = [e for e in logstore.list_logs() if "hard-timeout" in e.summary]
    assert len(logs) == 1
    body = logs[0].path.read_text()
    assert "dumping wedge" in body
    assert "Thread" in body                   # the all-threads dump
    assert "time.sleep" in body or "sleep" in body  # the wedged frame


def test_expiry_warning_carries_zombie_count(zero_grace, crash_dir, caplog):
    import logging

    guarded = _hard_deadline(
        lambda: time.sleep(5), timeout_s=0.1, describe="zombie one")
    with caplog.at_level(logging.WARNING, logger="modulatio.runners"):
        with pytest.raises(SeatCallHardTimeout):
            guarded()
    warned = [r.message for r in caplog.records if "hard-timeout" in r.message]
    assert warned and "zombie" in warned[0].lower()
