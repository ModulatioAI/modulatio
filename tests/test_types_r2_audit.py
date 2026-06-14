# SPDX-License-Identifier: Apache-2.0
"""R2-audit regression tests for modulatio.types.

LOW [resource-leak]: ``_SEEN_UNKNOWN_BUDGET_ROLES`` (the per-process cache
of already-warned unknown ``context_budgets`` role tuples) grew unbounded
across the process lifetime — a buggy/adversarial caller feeding an endless
stream of *distinct* unknown role tuples added a new entry each time and
nothing ever evicted them. The fix caps the cache at
``_SEEN_UNKNOWN_BUDGET_ROLES_MAX`` and flushes wholesale on overflow, while
keeping it a plain ``set`` so existing ``.add`` / ``.discard`` / ``.clear``
/ membership contracts (used by callers and sibling tests) stay valid.
"""

from __future__ import annotations

import logging

from modulatio import types as types_mod
from modulatio.types import Project


def _new_project(context_budgets: dict[str, int]):
    return Project(
        code="R2A",
        name="R2Audit",
        objective="obj",
        leader_model="stub",
        wiki_path="/tmp/r2audit",
        context_budgets=context_budgets,
    )


def test_seen_unknown_budget_roles_is_bounded(monkeypatch, caplog):
    """Feeding many DISTINCT unknown role tuples must not grow the cache
    past the cap — it flushes wholesale on overflow instead of leaking
    one entry per distinct tuple forever.
    """
    # Small cap + clean slate so the test is fast and deterministic.
    monkeypatch.setattr(types_mod, "_SEEN_UNKNOWN_BUDGET_ROLES_MAX", 8)
    monkeypatch.setattr(types_mod, "_SEEN_UNKNOWN_BUDGET_ROLES", set())

    cap = types_mod._SEEN_UNKNOWN_BUDGET_ROLES_MAX

    # Each project carries a brand-new, never-before-seen unknown role.
    with caplog.at_level(logging.DEBUG, logger="modulatio.context_budget"):
        for i in range(cap * 5):
            _new_project({f"bogus-role-{i}": 4096})

    seen = types_mod._SEEN_UNKNOWN_BUDGET_ROLES
    # Before the fix this set would hold cap*5 entries (one per tuple);
    # bounded, it can never exceed the cap.
    assert len(seen) <= cap, (
        f"cache grew to {len(seen)} entries; cap is {cap} — leak not bounded"
    )


def test_seen_unknown_budget_roles_stays_a_set(monkeypatch):
    """The cache must remain a plain ``set`` so set-only operations relied
    on by callers/tests (``.add`` / ``.discard`` / ``.clear``) keep working.
    """
    assert isinstance(types_mod._SEEN_UNKNOWN_BUDGET_ROLES, set)
    # Exercise the set-only methods that sibling tests depend on.
    probe = ("r2-probe-role",)
    types_mod._SEEN_UNKNOWN_BUDGET_ROLES.add(probe)
    assert probe in types_mod._SEEN_UNKNOWN_BUDGET_ROLES
    types_mod._SEEN_UNKNOWN_BUDGET_ROLES.discard(probe)
    assert probe not in types_mod._SEEN_UNKNOWN_BUDGET_ROLES


def test_unknown_role_still_warns_once_then_dedups(monkeypatch, caplog):
    """The bound must not change the core contract: a freshly-seen unknown
    tuple WARNs exactly once; immediate repeats drop to DEBUG.
    """
    monkeypatch.setattr(types_mod, "_SEEN_UNKNOWN_BUDGET_ROLES", set())
    bad = {"r2-fresh-role": 4096}

    with caplog.at_level(logging.DEBUG, logger="modulatio.context_budget"):
        _new_project(bad)
        _new_project(bad)
        _new_project(bad)

    warns = [
        r for r in caplog.records
        if r.levelno == logging.WARNING and "unknown budget_role" in r.message
    ]
    debugs = [
        r for r in caplog.records
        if r.levelno == logging.DEBUG and "unknown budget_role" in r.message
    ]
    assert len(warns) == 1, f"expected exactly one WARN, got {len(warns)}"
    assert len(debugs) == 2, f"expected two repeat DEBUGs, got {len(debugs)}"
