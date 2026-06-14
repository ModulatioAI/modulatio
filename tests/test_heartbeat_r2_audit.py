"""Round-2 audit regressions for heartbeat (Cowboy Opus full-debug r2).

Two MEDIUM defects fixed in src/modulatio/heartbeat.py:

1. parse_interval accepted a zero value ('0m'/'0h'/'0d') → timedelta(0) →
   perpetual next_run==now → on the cron path an unbounded immediate-dispatch
   runaway-cost loop. Fix: reject val<=0 at the parse source (return None).

2. requeue_recurring created the recurring child via add_task (next_run=None)
   and only THEN set next_run in a separate lock window. Between those windows
   the child was on disk eligible-now, so a concurrent claim_next_pending could
   dispatch it before its interval next_run was set, bypassing recurrence. Fix:
   add a next_run kwarg to add_task and have requeue_recurring pre-set it so the
   child is persisted atomically with its interval already in place.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from modulatio import config, heartbeat


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "config"
    monkeypatch.setattr(config, "CONFIG_DIR", cfg_dir)
    monkeypatch.setattr(config, "DEFAULTS_FILE", cfg_dir / "defaults.json")
    config.save_defaults({"vault_root": str(tmp_path / "vault")})
    config.reload()
    yield


# === Finding 3: parse_interval rejects zero/negative ===

@pytest.mark.parametrize("s", ["0m", "0h", "0d", "0 min", "0 hours", "0 days"])
def test_parse_interval_rejects_zero(s):
    # Before the fix this returned timedelta(0) → next_run==now forever.
    assert heartbeat.parse_interval(s) is None


def test_parse_interval_still_accepts_positive():
    # Guard against an over-broad fix that rejects legitimate intervals.
    assert heartbeat.parse_interval("1m").total_seconds() == 60
    assert heartbeat.parse_interval("6h").total_seconds() == 6 * 3600


def test_requeue_recurring_treats_zero_interval_as_non_recurring():
    # A degenerate '0m' recurrence must not re-enqueue (delta is None now).
    task = heartbeat.add_task(
        description="degenerate", project_code="X", objective="o", every="0m",
    )
    assert heartbeat.requeue_recurring(task) is None
    # Only the original task remains; no runaway child was created.
    assert len(heartbeat.list_tasks()) == 1


# === Finding 1: requeue_recurring is atomic — child never eligible-now ===

def test_add_task_accepts_next_run_kwarg():
    nr = "2099-01-01T00:00:00+00:00"
    task = heartbeat.add_task(
        description="future", project_code="X", objective="o", next_run=nr,
    )
    assert task["next_run"] == nr
    # Default behaviour unchanged: omitting the kwarg yields next_run=None.
    plain = heartbeat.add_task(description="now", project_code="X", objective="o")
    assert plain["next_run"] is None


def test_requeue_recurring_child_is_persisted_with_next_run_set():
    """The recurring child must be on disk with next_run set the moment it
    exists — never in the eligible-now (next_run=None) window a concurrent
    claim could dispatch through."""
    task = heartbeat.add_task(
        description="daily", project_code="X", objective="o", every="1d",
    )
    new = heartbeat.requeue_recurring(task)
    assert new is not None
    assert new["next_run"] is not None

    # Re-read the child straight from disk (not the returned dict) and confirm
    # it is in the future — i.e. it was persisted atomically with its interval,
    # not briefly stored eligible-now.
    on_disk = heartbeat.get_task(new["id"])
    assert on_disk is not None
    assert on_disk["next_run"] is not None
    nr = datetime.fromisoformat(on_disk["next_run"])
    assert nr > datetime.now(timezone.utc)


def test_recurring_child_is_not_immediately_claimable():
    """End-to-end: a freshly requeued recurring child must not be claimed
    before its interval elapses. Before the fix, the brief next_run=None
    window let _select_next_pending treat it as eligible-now."""
    task = heartbeat.add_task(
        description="daily", project_code="X", objective="o", every="1d",
    )
    # Mark the original done so only the recurring child could be selected.
    heartbeat.update_task(task["id"], status="done")
    child = heartbeat.requeue_recurring(task)
    assert child is not None

    # The child's next_run is ~1 day out → it must NOT be selected now.
    claimed = heartbeat.claim_next_pending()
    assert claimed is None
