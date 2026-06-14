"""0.9.0 pre-ship round-3 re-sweep regressions for ``authorize_metered_tool``.

Finding 1 [MEDIUM/cost]: an idempotent metered replay (the SAME (cost_class,
task, key) call already authorized today) returns allowed-but-not-re-charged.
That contract is correct and test-locked (``test_authorizer_happy_path_then_
idempotent``) — moving the short-circuit after the per-task cap would break it.
The actual cost leak is that the runner re-invokes ``tool.call`` on every
allowed call with NO way to tell an idempotent replay apart from a fresh spend,
because the signal lived only in a ``reason`` substring.

Fix (in comptroller.py): ``Authorization`` carries a STRUCTURED
``idempotent_reuse`` flag (default False, back-compat), set True only on the
key-seen branch. Downstream (metered.py/runners.py) can short-circuit the
provider re-invoke off the flag instead of substring-matching the reason.

These tests fail without the fix:
  * the flag does not exist on ``Authorization`` (AttributeError), and
  * the key-seen branch returns the default-False flag.
The cap-still-bounds and not-re-charged invariants are asserted to remain.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from modulatio import comptroller, vault


PROJECT_CODE = "MR3"


@pytest.fixture
def project_with_budget(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    vault.init_project(PROJECT_CODE, "Resweep", "obj")
    (tmp_path / PROJECT_CODE.lower() / "comptroller.md").write_text(
        "---\npaid_cloud_escalations_per_day: 5\n---\n"
    )
    return tmp_path


def test_authorization_has_idempotent_reuse_field_defaulting_false():
    # Back-compat: constructing without the new field works and defaults False.
    auth = comptroller.Authorization(allowed=True, refresh_at=None, reason="ok")
    assert auth.idempotent_reuse is False


def test_first_metered_call_is_not_flagged_idempotent(project_with_budget):
    a1 = comptroller.authorize_metered_tool(
        PROJECT_CODE, "paid-cloud", "render", "T-1", "key-abc", "agent-1",
    )
    assert a1.allowed is True
    assert a1.idempotent_reuse is False  # a fresh spend, not a replay


def test_idempotent_replay_sets_structured_flag_and_is_not_recharged(project_with_budget):
    # First call: real spend (one ledger entry).
    a1 = comptroller.authorize_metered_tool(
        PROJECT_CODE, "paid-cloud", "render", "T-1", "key-abc", "agent-1",
    )
    # Identical replay (same cost_class, task, key): allowed, structurally flagged.
    a2 = comptroller.authorize_metered_tool(
        PROJECT_CODE, "paid-cloud", "render", "T-1", "key-abc", "agent-1",
    )
    assert a1.allowed is True and a1.idempotent_reuse is False
    assert a2.allowed is True
    assert a2.idempotent_reuse is True  # THE structured signal the runner reads
    # Not re-charged: still exactly one distinct ledger spend for this key.
    cost, task_count, key_seen = comptroller._scan_metered_today(
        PROJECT_CODE, "paid-cloud", "T-1", "key-abc",
    )
    assert cost == 1
    assert key_seen is True


def test_per_task_cap_still_bounds_a_DISTINCT_runaway_call(project_with_budget):
    # The flag must NOT widen the cap: a *different* key (a distinct metered call)
    # in the same task is still denied once the per-task cap (default 1) is hit.
    a1 = comptroller.authorize_metered_tool(
        PROJECT_CODE, "paid-cloud", "render", "T-1", "key-one", "agent-1",
    )
    a2 = comptroller.authorize_metered_tool(
        PROJECT_CODE, "paid-cloud", "render", "T-1", "key-two", "agent-1",
    )
    assert a1.allowed is True
    assert a2.allowed is False  # distinct second call bounded by per-task cap
    assert "per-task metered cap" in a2.reason
    assert a2.idempotent_reuse is False
