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


# ── Slice 2: the factory boundaries wear the deadline; Clay never does ──────


def _mock_llm(monkeypatch, sleep_s: float = 0.0):
    """Monkeypatch litellm.completion (optionally sleeping) + resolution."""
    import litellm

    def _completion(**k):
        if sleep_s:
            time.sleep(sleep_s)
        return _fake_response()

    monkeypatch.setattr(litellm, "completion", _completion)
    monkeypatch.setattr(litellm, "completion_cost", lambda **k: 0.0)
    monkeypatch.setattr(
        "modulatio.runners._resolve_model_call_args", lambda m: (m, {}))
    monkeypatch.setattr("modulatio.model_presets.load_presets", lambda: {})


def _fake_response():
    class _U:
        prompt_tokens = 10
        completion_tokens = 5

    class _M:
        content = "ok"
        tool_calls = None

    class _C:
        message = _M()

    class _R:
        choices = [_C()]
        usage = _U()

    return _R()


def test_single_shot_runner_wears_the_deadline(monkeypatch, zero_grace, crash_dir):
    from modulatio.runners import _default_call_timeout, litellm_runner

    _mock_llm(monkeypatch)
    runner = litellm_runner("openrouter/test")
    assert runner._hard_deadline_s == _default_call_timeout()
    assert runner("hi") == "ok"                       # transparent fast path

    monkeypatch.setenv("MODULATIO_CALL_TIMEOUT", "0.1")
    _mock_llm(monkeypatch, sleep_s=5)
    slow = litellm_runner("openrouter/test")
    with pytest.raises(SeatCallHardTimeout):
        slow("hi")


def test_chat_runner_wears_the_deadline(monkeypatch, zero_grace, crash_dir):
    from modulatio.runners import _default_call_timeout, litellm_chat_runner

    _mock_llm(monkeypatch)
    runner = litellm_chat_runner("openrouter/test")
    assert runner._hard_deadline_s == _default_call_timeout()
    resp = runner(messages=[{"role": "user", "content": "hi"}], tools=[])
    assert resp.content == "ok"

    monkeypatch.setenv("MODULATIO_CALL_TIMEOUT", "0.1")
    _mock_llm(monkeypatch, sleep_s=5)
    slow = litellm_chat_runner("openrouter/test")
    with pytest.raises(SeatCallHardTimeout):
        slow(messages=[{"role": "user", "content": "hi"}], tools=[])


def test_chat_runner_usage_rides_through_the_deadline_thread(
    monkeypatch, zero_grace,
):
    """Thread-in-thread proof at the real seam: the caller's tracker sees
    the usage recorded inside the guarded completion."""
    from modulatio.runners import litellm_chat_runner

    _mock_llm(monkeypatch)
    runner = litellm_chat_runner("openrouter/test")
    tracker = budget.BudgetTracker()
    with budget.with_tracker(tracker):
        runner(messages=[{"role": "user", "content": "hi"}], tools=[])
    assert tracker.tokens_used == 15


def test_clay_runners_never_wear_the_deadline(monkeypatch, zero_grace):
    """Clay's subprocess lane already hard-kills — the thread boundary must
    not double-bind it, on either path."""
    from modulatio.runners import litellm_chat_runner, litellm_runner

    monkeypatch.setattr(
        "modulatio.model_presets.load_presets",
        lambda: {"clayseat": {"model": "claude-sonnet-4-6", "endpoint": "claude_cli",
                              "auth_type": "oauth_anthropic"}},
    )
    single = litellm_runner("clayseat")
    assert not hasattr(single, "_hard_deadline_s")
    chat = litellm_chat_runner("clayseat")
    assert not hasattr(chat, "_hard_deadline_s")
