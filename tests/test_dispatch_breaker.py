"""QC-as-fixer Slice 2 — per-dispatch circuit breaker (post-hoc detector)."""

from __future__ import annotations

from modulatio import dispatch_breaker
from modulatio.dispatch_breaker import DispatchAbort, analyze_output, resolve_output_budget


# ── enable flag ───────────────────────────────────────────────────────


def test_breaker_disabled_by_default(monkeypatch):
    monkeypatch.delenv("MODULATIO_DISPATCH_BREAKER", raising=False)
    assert dispatch_breaker.breaker_enabled() is False


def test_breaker_enabled_only_for_exact_one(monkeypatch):
    monkeypatch.setenv("MODULATIO_DISPATCH_BREAKER", "1")
    assert dispatch_breaker.breaker_enabled() is True
    monkeypatch.setenv("MODULATIO_DISPATCH_BREAKER", "true")
    assert dispatch_breaker.breaker_enabled() is False


# ── healthy dispatches do NOT trip ─────────────────────────────────────


def test_healthy_committed_artifact_passes():
    raw = "\n".join(f"line {i} of a perfectly normal monotonic draft" for i in range(200))
    committed = raw
    assert analyze_output(raw, committed, role="producer") is None


def test_large_monotonic_artifact_above_soft_cap_passes():
    # 35K unique tokens — ABOVE the 30K soft cap but below the hard cap:
    # no repetition, fully committed. Soft cap alone must NOT trip (the
    # whole "trip on no-progress, not volume" contract).
    raw = " ".join(f"token{i}" for i in range(35_000))
    assert analyze_output(raw, raw, role="producer") is None


# ── no-progress signals DO trip ────────────────────────────────────────


def test_no_commit_storm_trips():
    raw = " ".join(f"word{i}" for i in range(5_000))  # lots of unique output...
    committed = ""  # ...nothing written
    abort = analyze_output(raw, committed, role="producer")
    assert isinstance(abort, DispatchAbort)
    assert abort.reason == "no_commit"
    assert abort.output_tokens >= 500


def test_no_commit_does_not_trip_on_small_empty_output():
    # A legitimately short empty result (below the min-output floor) is not
    # a storm — don't trip.
    abort = analyze_output("just a few words here", "", role="producer")
    assert abort is None


def test_degenerate_repetition_trips():
    phrase = "the beacon sink will not converge and so " * 8  # 12-gram repeats
    abort = analyze_output(phrase, phrase, role="producer")
    assert isinstance(abort, DispatchAbort)
    assert abort.reason == "repetition"


def test_hard_backstop_trips_regardless():
    budget = resolve_output_budget("producer")
    raw = " ".join(f"u{i}" for i in range(budget.hard_cap + 10))  # unique, no repeat
    abort = analyze_output(raw, raw, role="producer")  # fully committed
    assert isinstance(abort, DispatchAbort)
    assert abort.reason == "hard_cap"
    assert abort.output_tokens >= budget.hard_cap


# ── structured payload ─────────────────────────────────────────────────


def test_abort_carries_partial_text_for_salvage():
    raw = " ".join(f"word{i}" for i in range(5_000))
    committed = ""
    abort = analyze_output(raw, committed, role="producer")
    assert abort is not None
    assert abort.partial_text == committed
    assert abort.role == "producer"
    assert "no_commit" in abort.summary


def test_resolve_output_budget_hard_cap_exceeds_soft():
    b = resolve_output_budget("producer")
    assert b.hard_cap > b.soft_cap
    assert b.soft_cap == 30_000


# ── #151: project/agent output-budget overrides ────────────────────────


def test_resolve_output_budget_override_wins():
    """A project/agent per-role override beats the built-in default, and
    lifts the hard backstop with it (hard = max(default_hard, soft*2))."""
    b = resolve_output_budget("producer", overrides={"producer": 50_000})
    assert b.soft_cap == 50_000
    assert b.hard_cap == 100_000  # soft*2 > default 60K


def test_resolve_output_budget_override_only_for_named_role():
    # override names "qc"; producer still gets the default.
    b = resolve_output_budget("producer", overrides={"qc": 5_000})
    assert b.soft_cap == 30_000


def test_analyze_output_honors_override_for_hard_cap_trip():
    # Lower the producer soft cap so a modest unique output trips hard_cap.
    raw = " ".join(f"u{i}" for i in range(9_000))  # 9K tokens, unique
    # default: 9K < 30K soft, no trip. Override soft to 4K → hard=8K → trips.
    none = analyze_output(raw, raw, role="producer")
    assert none is None
    abort = analyze_output(raw, raw, role="producer", overrides={"producer": 4_000})
    assert isinstance(abort, DispatchAbort)
    assert abort.reason == "hard_cap"
