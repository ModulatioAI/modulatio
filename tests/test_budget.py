"""Unit tests for the budget tracker module.

Behavior is independent of any LLM provider — the runner-level
integration (litellm_runner → record_usage) is exercised in
test_project_execution.py via the cap-halt tests.
"""

from pathlib import Path

import pytest

from modulatio import budget


def test_tracker_starts_at_zero_when_no_caps_set():
    t = budget.BudgetTracker()
    assert t.tokens_used == 0
    assert t.cost_usd_used == 0.0
    assert t.max_tokens is None
    assert t.max_cost_usd is None
    assert t.over_cap() is None  # unbounded never trips


def test_record_accumulates_input_and_output_tokens():
    t = budget.BudgetTracker()
    t.record(input_tokens=100, output_tokens=50, cost_usd=0.001)
    assert t.tokens_used == 150
    assert t.cost_usd_used == pytest.approx(0.001)
    t.record(input_tokens=200, output_tokens=80, cost_usd=0.002)
    assert t.tokens_used == 430
    assert t.cost_usd_used == pytest.approx(0.003)


def test_over_cap_returns_none_when_under_token_cap():
    t = budget.BudgetTracker(max_tokens=500)
    t.record(input_tokens=100, output_tokens=50, cost_usd=0.0)
    assert t.over_cap() is None


def test_over_cap_returns_reason_when_token_cap_exceeded():
    t = budget.BudgetTracker(max_tokens=100)
    t.record(input_tokens=80, output_tokens=30, cost_usd=0.0)
    reason = t.over_cap()
    assert reason is not None
    assert "token cap" in reason.lower()
    assert "110" in reason
    assert "100" in reason


def test_over_cap_returns_reason_when_cost_cap_exceeded():
    t = budget.BudgetTracker(max_cost_usd=0.01)
    t.record(input_tokens=10, output_tokens=10, cost_usd=0.015)
    reason = t.over_cap()
    assert reason is not None
    assert "cost cap" in reason.lower()


def test_token_cap_takes_precedence_when_both_exceeded():
    """Deterministic ordering — token cap is checked first. The user
    sees ONE halt reason at a time, not a compound message."""
    t = budget.BudgetTracker(max_tokens=10, max_cost_usd=0.001)
    t.record(input_tokens=20, output_tokens=20, cost_usd=0.10)
    reason = t.over_cap()
    assert reason is not None
    assert "token cap" in reason.lower()


def test_record_usage_no_op_when_no_tracker_bound():
    """Module-level helper must be safe to call without an active
    binding — runners call it unconditionally so the no-binding case
    cannot raise."""
    # Confirm no tracker is bound at module-import baseline.
    assert budget.current_tracker() is None
    # Should not raise.
    budget.record_usage(input_tokens=100, output_tokens=50, cost_usd=0.001)


def test_with_tracker_binds_for_block_only():
    """Binding scope: inside the with-block, ``current_tracker``
    returns the tracker; outside, it returns to whatever was bound
    before (None at the module-import baseline)."""
    t = budget.BudgetTracker()
    assert budget.current_tracker() is None
    with budget.with_tracker(t):
        assert budget.current_tracker() is t
        budget.record_usage(
            input_tokens=10, output_tokens=5, cost_usd=0.001,
        )
    assert budget.current_tracker() is None
    assert t.tokens_used == 15
    assert t.cost_usd_used == pytest.approx(0.001)


def test_bind_unbind_roundtrip():
    """Non-context-manager API for callers that can't easily wrap a
    long block in ``with``. ``bind`` returns a token; ``unbind``
    accepts it. Same effect as with_tracker; same isolation."""
    t = budget.BudgetTracker()
    assert budget.current_tracker() is None
    token = budget.bind(t)
    try:
        assert budget.current_tracker() is t
        budget.record_usage(
            input_tokens=42, output_tokens=8, cost_usd=0.0,
        )
    finally:
        budget.unbind(token)
    assert budget.current_tracker() is None
    assert t.tokens_used == 50


def test_nested_with_tracker_restores_outer():
    """Nested binding: inner with-block sees the inner tracker;
    outer block sees the outer one again on exit. Correctness for
    composed callers (e.g., a future ``with_run_budget`` wrapping
    ``with_plan_budget``)."""
    outer = budget.BudgetTracker()
    inner = budget.BudgetTracker()
    with budget.with_tracker(outer):
        assert budget.current_tracker() is outer
        with budget.with_tracker(inner):
            assert budget.current_tracker() is inner
            budget.record_usage(
                input_tokens=1, output_tokens=1, cost_usd=0.0,
            )
        assert budget.current_tracker() is outer
        budget.record_usage(
            input_tokens=10, output_tokens=10, cost_usd=0.0,
        )
    assert outer.tokens_used == 20
    assert inner.tokens_used == 2


# ── Per-call telemetry log ──────────────────────────────────────────────


def test_log_path_none_skips_logging(tmp_path):
    """Default tracker (no log_path) doesn't create any files even
    after multiple records. Backward compat: existing callers see
    no behavior change."""
    t = budget.BudgetTracker()
    t.record(input_tokens=10, output_tokens=5, cost_usd=0.001)
    t.record(input_tokens=20, output_tokens=10, cost_usd=0.002)
    # No file should exist anywhere; tmp_path stays empty.
    assert list(tmp_path.iterdir()) == []


def test_log_path_appends_one_jsonl_line_per_record(tmp_path):
    """When log_path is set, every record() appends a JSONL line
    capturing this call's tokens + cost AND the running totals after
    the call (self-describing per line — tail/grep-friendly)."""
    import json
    log = tmp_path / "plan.usage.jsonl"
    t = budget.BudgetTracker(log_path=log)

    t.record(
        input_tokens=100, output_tokens=50, cost_usd=0.001,
        model="openrouter/test-1",
    )
    t.record(
        input_tokens=200, output_tokens=80, cost_usd=0.002,
        model="ollama/test-2",
    )

    lines = log.read_text().strip().split("\n")
    assert len(lines) == 2

    e1 = json.loads(lines[0])
    assert e1["model"] == "openrouter/test-1"
    assert e1["input_tokens"] == 100
    assert e1["output_tokens"] == 50
    assert e1["cost_usd"] == 0.001
    assert e1["tokens_total"] == 150
    assert e1["cost_total"] == 0.001
    assert "ts" in e1  # ISO8601 timestamp present

    e2 = json.loads(lines[1])
    assert e2["model"] == "ollama/test-2"
    assert e2["tokens_total"] == 430  # cumulative across both calls
    assert e2["cost_total"] == 0.003


def test_log_path_creates_parent_directory(tmp_path):
    """If the configured log_path's parent doesn't exist yet (fresh
    project), the first record() creates it. ``start_execution``
    relies on this — the plans/ dir exists but a .usage.jsonl
    sibling will be the first file in some sub-tree variations."""
    log = tmp_path / "fresh" / "subdir" / "plan.usage.jsonl"
    assert not log.parent.exists()
    t = budget.BudgetTracker(log_path=log)
    t.record(input_tokens=1, output_tokens=1, cost_usd=0.0)
    assert log.exists()


def test_log_path_failure_swallowed_does_not_raise(tmp_path, monkeypatch):
    """Telemetry must never break a real LLM call. If the filesystem
    write raises, record() must still complete (the totals still
    update even though the log line is lost)."""
    log = tmp_path / "plan.usage.jsonl"
    t = budget.BudgetTracker(log_path=log)

    # Force every open() on the log to raise.
    real_open = Path.open

    def boom(self, *args, **kwargs):
        if self == log:
            raise OSError("simulated disk full")
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", boom)

    # Must not raise.
    t.record(input_tokens=10, output_tokens=5, cost_usd=0.001)
    assert t.tokens_used == 15  # accumulator still ran


def test_record_usage_passes_model_through_to_tracker(tmp_path):
    """The module-level record_usage helper threads the optional
    ``model`` arg to the tracker. Important: runners that have the
    model id MUST be able to label log entries; tests that don't can
    leave it None."""
    import json
    log = tmp_path / "plan.usage.jsonl"
    t = budget.BudgetTracker(log_path=log)
    with budget.with_tracker(t):
        budget.record_usage(
            input_tokens=10, output_tokens=5, cost_usd=0.001,
            model="anthropic/claude-haiku-4.5",
        )
    line = log.read_text().strip()
    entry = json.loads(line)
    assert entry["model"] == "anthropic/claude-haiku-4.5"


def test_record_usage_without_model_logs_null(tmp_path):
    """Backward compat: callers that don't pass model get a None
    in the log entry rather than missing the field entirely. Keeps
    the log schema fixed."""
    import json
    log = tmp_path / "plan.usage.jsonl"
    t = budget.BudgetTracker(log_path=log)
    with budget.with_tracker(t):
        budget.record_usage(
            input_tokens=10, output_tokens=5, cost_usd=0.001,
        )
    line = log.read_text().strip()
    entry = json.loads(line)
    assert entry["model"] is None
