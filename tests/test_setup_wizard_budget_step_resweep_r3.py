# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Round-3 re-sweep regressions for ``setup_wizard.budget_step``.

Covers the re-invocation clear-an-axis defect: on re-invocation the step
pre-fills ``budget_caps`` from defaults, and the old code passed each
prior value as ``prompt_nav``'s ``default`` — so a blank Enter returned
the prior value (steps.py:133) and silently KEPT the cap, contradicting
the "blank = unbounded" prompt copy. A single axis could not be cleared.

The fix shows the prior value as a hint (not a ``default``); blank now
truly clears the axis to unbounded, while typing ``k`` keeps the prior
value without retyping.
"""

from __future__ import annotations

from modulatio.setup_wizard import budget_step


def _feed(monkeypatch, answers):
    it = iter(answers)
    monkeypatch.setattr("builtins.input", lambda *_a, **_k: next(it))


def test_reinvocation_blank_clears_single_axis(monkeypatch):
    """Re-invocation with all three caps pre-set; user blanks the
    wall-clock axis and keeps the other two with 'k'. Blank must clear
    the axis to None (unbounded), not retain the prior value."""
    state = {
        "budget_caps": {
            "max_wall_clock_min": 30.0,
            "max_tokens": 50000,
            "max_cost_usd": 5.0,
        }
    }
    # gate, wall-clock (blank=clear), tokens (k=keep), cost (k=keep)
    _feed(monkeypatch, ["y", "", "k", "k"])
    result = budget_step.run(state)
    assert result == "captured"
    caps = state["budget_caps"]
    assert caps["max_wall_clock_min"] is None  # the bug: was 30.0
    assert caps["max_tokens"] == 50000
    assert caps["max_cost_usd"] == 5.0


def test_reinvocation_blank_clears_all_axes(monkeypatch):
    """Blank on every axis during re-invocation clears every cap, even
    though all three were previously set."""
    state = {
        "budget_caps": {
            "max_wall_clock_min": 30.0,
            "max_tokens": 50000,
            "max_cost_usd": 5.0,
        }
    }
    _feed(monkeypatch, ["y", "", "", ""])
    result = budget_step.run(state)
    assert result == "captured"
    assert state["budget_caps"] == {
        "max_wall_clock_min": None,
        "max_tokens": None,
        "max_cost_usd": None,
    }


def test_reinvocation_keep_sentinel_retains_prior(monkeypatch):
    """'k' on an axis with a prior value keeps it; the convenience path
    that replaces the old implicit blank-keeps behavior."""
    state = {
        "budget_caps": {
            "max_wall_clock_min": 30.0,
            "max_tokens": 50000,
            "max_cost_usd": 5.0,
        }
    }
    _feed(monkeypatch, ["y", "k", "k", "k"])
    result = budget_step.run(state)
    assert result == "captured"
    assert state["budget_caps"] == {
        "max_wall_clock_min": 30.0,
        "max_tokens": 50000,
        "max_cost_usd": 5.0,
    }


def test_reinvocation_new_value_overrides_prior(monkeypatch):
    """Typing a fresh value replaces the prior cap on that axis."""
    state = {
        "budget_caps": {
            "max_wall_clock_min": 30.0,
            "max_tokens": 50000,
            "max_cost_usd": 5.0,
        }
    }
    _feed(monkeypatch, ["y", "45", "k", "k"])
    result = budget_step.run(state)
    assert result == "captured"
    assert state["budget_caps"]["max_wall_clock_min"] == 45.0


def test_keep_sentinel_inert_when_no_prior(monkeypatch):
    """On a first run (no prior caps) there is nothing to keep, so 'k'
    is just invalid text: the validator rejects it and re-prompts. This
    guards against 'k' being silently treated as a value."""
    # gate=y, wall-clock: 'k' (rejected) then '30'; tokens blank; cost blank
    _feed(monkeypatch, ["y", "k", "30", "", ""])
    state: dict = {}
    result = budget_step.run(state)
    assert result == "captured"
    caps = state["budget_caps"]
    assert caps["max_wall_clock_min"] == 30.0
    assert caps["max_tokens"] is None
    assert caps["max_cost_usd"] is None


def test_prompt_axis_blank_returns_none_with_prior():
    """Unit-level: the per-axis helper returns None on blank input even
    when a prior value exists (the heart of the clear-an-axis fix)."""
    import builtins

    orig = builtins.input
    try:
        builtins.input = lambda *_a, **_k: ""
        v = budget_step._prompt_axis(
            "Wall-clock cap in minutes",
            prior=30.0,
            prior_display="30",
            parse=budget_step._parse_optional_float,
        )
    finally:
        builtins.input = orig
    assert v is None


def test_prompt_axis_keep_returns_prior():
    """Unit-level: 'k' returns the exact prior value."""
    import builtins

    orig = builtins.input
    try:
        builtins.input = lambda *_a, **_k: "k"
        v = budget_step._prompt_axis(
            "Token cap",
            prior=50000,
            prior_display="50000",
            parse=budget_step._parse_optional_int,
        )
    finally:
        builtins.input = orig
    assert v == 50000
