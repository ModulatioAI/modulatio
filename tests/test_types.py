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
import threading


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


# ═══ fold: test_types_low_audit.py ═══
# LOW-audit regression tests for modulatio.types.
#
# Finding #56 [race]: ``_SEEN_UNKNOWN_BUDGET_ROLES`` is a module-global set
# mutated inside the ``context_budgets`` pydantic validator without
# synchronization. Project validation runs on concurrent wave-executor
# threads (waves are ON by default), so an unsynchronized "in?-then-add"
# check-then-act can let two threads both treat the same unknown-role tuple
# as a *first sighting* and both emit the WARN. The documented contract is
# exactly one WARN per unknown tuple per process; repeats drop to DEBUG.


def test_unknown_budget_role_warns_once_under_concurrency(caplog):
    """Many threads validating a Project carrying the SAME unknown
    budget_role must produce exactly ONE first-sighting WARNING; all
    others must be deduped to DEBUG. Without the lock, the racy
    check-then-act lets multiple threads both miss the set and both warn.
    """
    unknown_role = "totally-bogus-role-xyz"
    bad_budget = {unknown_role: 4096}
    unknown_key = (unknown_role,)

    # Start from a clean slate so this tuple is genuinely a first sighting.
    types_mod._SEEN_UNKNOWN_BUDGET_ROLES.discard(unknown_key)

    n_threads = 32
    barrier = threading.Barrier(n_threads)
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            barrier.wait()
            _new_project(bad_budget)
        except BaseException as exc:  # noqa: BLE001 - surface in assert
            errors.append(exc)

    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="modulatio.context_budget"):
        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    assert not errors, f"validation raised under concurrency: {errors!r}"

    warns = [
        r for r in caplog.records
        if r.levelno == logging.WARNING and "unknown budget_role" in r.message
    ]
    debugs = [
        r for r in caplog.records
        if r.levelno == logging.DEBUG and "unknown budget_role" in r.message
    ]

    assert len(warns) == 1, (
        f"expected exactly one first-sighting WARN, got {len(warns)} "
        f"(race let multiple threads treat the tuple as first sighting)"
    )
    # The remaining validations should have deduped to DEBUG.
    assert len(debugs) == n_threads - 1, (
        f"expected {n_threads - 1} repeat DEBUG rows, got {len(debugs)}"
    )

    # The tuple is now recorded as seen.
    assert unknown_key in types_mod._SEEN_UNKNOWN_BUDGET_ROLES


def test_unknown_budget_role_still_preserved_and_known_roles_quiet(caplog):
    """Sanity: unknown roles survive round-trip (not dropped) and a known
    role emits no unknown-role warning."""
    from modulatio.context_budget import EXPERIMENTAL_DEFAULTS

    known_role = next(iter(EXPERIMENTAL_DEFAULTS.keys()))
    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="modulatio.context_budget"):
        proj = _new_project({known_role: 8192})
    assert proj.context_budgets == {known_role: 8192}
    assert not [
        r for r in caplog.records if "unknown budget_role" in r.message
    ]
