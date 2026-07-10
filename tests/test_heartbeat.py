"""Slice 6: heartbeat queue + dispatcher tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from modulatio import config, heartbeat
from tests._thread_check import run_threads_checked
import multiprocessing as mp
import json
import os
import subprocess
import sys
import threading
import time


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "config"
    monkeypatch.setattr(config, "CONFIG_DIR", cfg_dir)
    monkeypatch.setattr(config, "DEFAULTS_FILE", cfg_dir / "defaults.json")
    config.save_defaults({"vault_root": str(tmp_path / "vault")})
    config.reload()
    yield


# === CRUD ===

def test_add_task_persists_to_queue_file():
    task = heartbeat.add_task(
        description="ship feature",
        project_code="STA",
        objective="Produce a one-page memo on X.",
    )
    assert task["status"] == "pending"
    assert task["project_code"] == "STA"
    assert task["objective"].startswith("Produce")
    assert heartbeat.get_task(task["id"]) is not None


def test_list_tasks_filters_by_status():
    heartbeat.add_task(description="a", project_code="STA", objective="x")
    t2 = heartbeat.add_task(description="b", project_code="STA", objective="y")
    heartbeat.update_task(t2["id"], status="done")
    pending = heartbeat.list_tasks(status="pending")
    done = heartbeat.list_tasks(status="done")
    assert len(pending) == 1
    assert len(done) == 1


def test_list_tasks_filters_by_project():
    heartbeat.add_task(description="x", project_code="AAA", objective="o1")
    heartbeat.add_task(description="y", project_code="BBB", objective="o2")
    aaa = heartbeat.list_tasks(project_code="AAA")
    assert len(aaa) == 1
    assert aaa[0]["project_code"] == "AAA"


def test_cancel_task_marks_status():
    task = heartbeat.add_task(description="x", project_code="STA", objective="o")
    assert heartbeat.cancel_task(task["id"]) is True
    assert heartbeat.get_task(task["id"])["status"] == "cancelled"


def test_cancel_task_returns_false_for_missing():
    assert heartbeat.cancel_task("not-a-real-id") is False


def test_clear_done_removes_terminal_tasks():
    heartbeat.add_task(description="p", project_code="X", objective="o")
    t2 = heartbeat.add_task(description="d", project_code="X", objective="o")
    heartbeat.update_task(t2["id"], status="done")
    t3 = heartbeat.add_task(description="f", project_code="X", objective="o")
    heartbeat.update_task(t3["id"], status="failed")
    removed = heartbeat.clear_done()
    assert removed == 2
    remaining = heartbeat.list_tasks()
    assert len(remaining) == 1
    assert remaining[0]["status"] == "pending"


# === Stale recovery ===

def test_recover_stale_marks_long_running_as_failed():
    task = heartbeat.add_task(description="stuck", project_code="X", objective="o")
    long_ago = (datetime.now(timezone.utc) - timedelta(minutes=60)).isoformat(timespec="seconds")
    heartbeat.update_task(task["id"], status="running", started=long_ago)
    n = heartbeat.recover_stale_tasks(max_age_minutes=30)
    assert n == 1
    assert heartbeat.get_task(task["id"])["status"] == "failed"


def test_recover_stale_skips_recent_running():
    task = heartbeat.add_task(description="recent", project_code="X", objective="o")
    heartbeat.update_task(
        task["id"], status="running", started=heartbeat._now_iso(),
    )
    n = heartbeat.recover_stale_tasks(max_age_minutes=30)
    assert n == 0
    assert heartbeat.get_task(task["id"])["status"] == "running"


def test_recover_stale_handles_missing_start():
    task = heartbeat.add_task(description="weird", project_code="X", objective="o")
    heartbeat.update_task(task["id"], status="running", started=None)
    n = heartbeat.recover_stale_tasks()
    assert n == 1
    assert heartbeat.get_task(task["id"])["status"] == "failed"


# === next_pending — priority + dependencies + recurrence timing ===

def test_next_pending_returns_lowest_priority_number_first():
    heartbeat.add_task(description="low", project_code="X", objective="o", priority=9)
    heartbeat.add_task(description="high", project_code="X", objective="o", priority=1)
    nxt = heartbeat.next_pending()
    assert nxt["description"] == "high"


def test_next_pending_skips_blocked_dependencies():
    blocker = heartbeat.add_task(description="blocker", project_code="X", objective="o")
    dep = heartbeat.add_task(
        description="dependent", project_code="X", objective="o",
        depends_on=[blocker["id"][-6:]],
    )
    nxt = heartbeat.next_pending()
    # blocker is pending too; dependent is blocked, so nxt should be blocker
    assert nxt["id"] == blocker["id"]
    # complete blocker, now dependent unlocks
    heartbeat.update_task(blocker["id"], status="done")
    nxt = heartbeat.next_pending()
    assert nxt["id"] == dep["id"]


def test_next_pending_skips_unmet_recurrence_window():
    task = heartbeat.add_task(description="recurring", project_code="X", objective="o")
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(timespec="seconds")
    heartbeat.update_task(task["id"], next_run=future)
    assert heartbeat.next_pending() is None


def test_next_pending_returns_none_when_empty():
    assert heartbeat.next_pending() is None


# === claim_next_pending — atomic select-and-claim (race fix, finding H8) ===

def test_claim_next_pending_marks_running_and_returns_task():
    task = heartbeat.add_task(description="t", project_code="X", objective="o")
    claimed = heartbeat.claim_next_pending()
    assert claimed is not None
    assert claimed["id"] == task["id"]
    assert claimed["status"] == "running"
    assert claimed["started"] is not None
    # The on-disk task is now running (claimed), not still pending.
    assert heartbeat.get_task(task["id"])["status"] == "running"


def test_claim_next_pending_returns_none_when_empty():
    assert heartbeat.claim_next_pending() is None


def test_claim_next_pending_is_exclusive_no_double_dispatch():
    """A single pending task must be claimable exactly once. A second claimer
    (the concurrent CLI run-once vs. daemon tick) gets a *different* task or
    None — never the same one twice."""
    heartbeat.add_task(description="only", project_code="X", objective="o")
    first = heartbeat.claim_next_pending()
    second = heartbeat.claim_next_pending()
    assert first is not None
    assert second is None  # no other pending task to fall through to


def test_claim_next_pending_concurrent_threads_claim_distinct_tasks():
    """Two threads claiming concurrently against the same on-disk queue must
    never both claim the same task. Regression for the select-then-claim race
    where next_pending() released the lock before status flipped to running."""
    import threading as _threading

    for i in range(8):
        heartbeat.add_task(
            description=f"t{i}", project_code="X", objective="o", priority=i,
        )
    claimed_ids: list[str] = []
    lock = _threading.Lock()
    barrier = _threading.Barrier(4)

    def _worker():
        barrier.wait()
        for _ in range(4):
            t = heartbeat.claim_next_pending()
            if t is not None:
                with lock:
                    claimed_ids.append(t["id"])

    run_threads_checked([_worker] * 4)

    # Every claim must be of a distinct task — no id claimed twice.
    assert len(claimed_ids) == len(set(claimed_ids)), "a task was claimed twice"
    # All 8 tasks got claimed.
    assert set(claimed_ids) == {t["id"] for t in heartbeat.list_tasks()}


# === _new_id — collision resistance under fast dispatch (finding #26) ===

def test_new_id_unique_under_fast_succession():
    """_new_id() must not collide for ids minted in the same microsecond
    window. The old strftime(...)[:18] dropped 2 of 6 microsecond digits,
    giving ~100µs resolution → colliding task/cron ids under fast dispatch."""
    ids = [heartbeat._new_id() for _ in range(2000)]
    assert len(ids) == len(set(ids)), "duplicate id minted in tight loop"


def test_new_id_starts_with_sortable_utc_timestamp():
    """Ids stay sortable by creation time — the leading 14 chars are the
    YYYYMMDDHHMMSS UTC stamp (next_pending tiebreaks on created order)."""
    import re as _re

    new = heartbeat._new_id()
    assert _re.match(r"^\d{14}", new), new
    # Earlier-minted id sorts before a later one.
    first = heartbeat._new_id()
    later = heartbeat._new_id()
    assert first[:14] <= later[:14]


# === parse_interval + requeue_recurring ===

@pytest.mark.parametrize("s,expected_seconds", [
    ("30m", 30 * 60),
    ("6h", 6 * 3600),
    ("1d", 86400),
    ("2 hours", 2 * 3600),
    ("15 mins", 15 * 60),
])
def test_parse_interval_valid(s, expected_seconds):
    delta = heartbeat.parse_interval(s)
    assert delta is not None
    assert delta.total_seconds() == expected_seconds


@pytest.mark.parametrize("s", ["", "garbage", "30s", "h", "30 weeks"])
def test_parse_interval_invalid(s):
    assert heartbeat.parse_interval(s) is None


def test_requeue_recurring_creates_new_pending_task():
    task = heartbeat.add_task(
        description="daily", project_code="X", objective="o", every="1d",
    )
    new = heartbeat.requeue_recurring(task)
    assert new is not None
    assert new["status"] == "pending"
    assert new["every"] == "1d"
    assert new["next_run"] is not None
    # both old + new exist
    assert len(heartbeat.list_tasks()) == 2


def test_requeue_recurring_noop_for_non_recurring():
    task = heartbeat.add_task(description="oneshot", project_code="X", objective="o")
    assert heartbeat.requeue_recurring(task) is None


# === Output capture ===

def test_save_task_output_writes_markdown(tmp_path):
    task = heartbeat.add_task(description="test", project_code="STA", objective="o")
    heartbeat.update_task(task["id"], started=heartbeat._now_iso())
    refreshed = heartbeat.get_task(task["id"])
    path = heartbeat.save_task_output(refreshed, "Result body line 1\nLine 2")
    assert path.exists()
    content = path.read_text()
    assert "Heartbeat task result" in content
    assert "Result body line 1" in content
    assert "STA" in content


# === Heartbeat dispatcher loop ===

def test_tick_once_dispatches_pending_task():
    heartbeat.add_task(description="t", project_code="X", objective="produce x")
    calls = []
    def _disp(code, obj):
        calls.append((code, obj))
        return "ok"
    hb = heartbeat.Heartbeat(dispatch_callback=_disp)
    result = hb.tick_once()
    assert result is not None
    assert result["status"] == "done"
    assert calls == [("X", "produce x")]


def test_tick_once_returns_none_on_empty_queue():
    hb = heartbeat.Heartbeat(dispatch_callback=lambda c, o: "ok")
    assert hb.tick_once() is None


def test_tick_once_marks_failed_on_dispatch_exception():
    task = heartbeat.add_task(description="t", project_code="X", objective="o")
    def _boom(code, obj):
        raise RuntimeError("dispatcher exploded")
    hb = heartbeat.Heartbeat(dispatch_callback=_boom)
    hb.tick_once()
    refreshed = heartbeat.get_task(task["id"])
    assert refreshed["status"] == "failed"
    assert "exploded" in refreshed["error"]


def test_tick_once_retries_on_failure_when_max_retries_allows():
    task = heartbeat.add_task(
        description="t", project_code="X", objective="o", max_retries=3,
    )
    attempts = {"n": 0}
    def _flaky(code, obj):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise RuntimeError("transient")
        return "finally"
    hb = heartbeat.Heartbeat(dispatch_callback=_flaky)
    hb.tick_once()
    refreshed = heartbeat.get_task(task["id"])
    assert refreshed["status"] == "pending"
    assert refreshed["retries"] == 1
    hb.tick_once()
    refreshed = heartbeat.get_task(task["id"])
    assert refreshed["status"] == "pending"
    assert refreshed["retries"] == 2
    hb.tick_once()
    refreshed = heartbeat.get_task(task["id"])
    assert refreshed["status"] == "done"


def test_tick_once_recurring_requeues_after_done():
    heartbeat.add_task(
        description="cron", project_code="X", objective="o", every="1h",
    )
    hb = heartbeat.Heartbeat(dispatch_callback=lambda c, o: "ok")
    hb.tick_once()
    # original is done; a new pending task with same description exists
    all_tasks = heartbeat.list_tasks()
    statuses = sorted(t["status"] for t in all_tasks)
    assert statuses == ["done", "pending"]


def test_heartbeat_start_stop_lifecycle():
    """Smoke: start a Heartbeat, stop it, no leaked threads."""
    hb = heartbeat.Heartbeat(
        dispatch_callback=lambda c, o: "ok",
        interval_seconds=60,  # high so the loop won't actually tick during test
    )
    hb.start()
    assert hb.is_running()
    hb.stop(timeout=2.0)
    assert not hb.is_running()


# === CLI: heartbeat add --every validation ===

def test_cli_heartbeat_add_rejects_malformed_every():
    """A malformed --every interval must fail loudly, not queue a task that
    can never recur (requeue_recurring silently returns None for it)."""
    from typer.testing import CliRunner

    from modulatio.cli import app

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "heartbeat", "add", "recurring memo",
            "--code", "STA",
            "--objective", "Produce a memo.",
            "--every", "30minutes",  # bad: parse_interval -> None
        ],
    )
    assert result.exit_code == 1
    assert "30minutes" in result.output
    # nothing was queued
    assert heartbeat.list_tasks() == []


def test_cli_heartbeat_add_accepts_valid_every():
    """A well-formed --every interval queues a recurring task."""
    from typer.testing import CliRunner

    from modulatio.cli import app

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "heartbeat", "add", "recurring memo",
            "--code", "STA",
            "--objective", "Produce a memo.",
            "--every", "6h",
        ],
    )
    assert result.exit_code == 0
    tasks = heartbeat.list_tasks()
    assert len(tasks) == 1
    assert tasks[0]["every"] == "6h"


# ═══ fold: test_heartbeat_preship.py ═══
# 0.9.0 pre-ship sweep regressions for ``heartbeat.py``.
#
# Three findings:
#   1. [race]        claim_next_pending must be exclusive ACROSS processes,
#                    not just threads (cross-process flock).
#   2. [correctness] A done/failed write must not clobber a status=cancelled
#                    set out-of-band mid-dispatch.
#   3. [correctness] An unparseable next_run must be skipped (not eligible),
#                    never silently run-now (which busy-loops a recurrence).




# === Finding 3: unparseable next_run is NOT eligible-now ===

def test_unparseable_next_run_is_skipped_not_run_now():
    t = heartbeat.add_task(
        description="recurring", project_code="STA", objective="x", every="1h",
    )
    # Corrupt the schedule stamp.
    heartbeat.update_task(t["id"], next_run="not-a-timestamp")
    # It must NOT be selected (eligible-now) — fail closed.
    assert heartbeat.next_pending() is None
    assert heartbeat.claim_next_pending() is None


def test_valid_future_next_run_still_skipped_and_past_still_runs():
    from datetime import datetime, timedelta, timezone

    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    tf = heartbeat.add_task(description="f", project_code="STA", objective="x",
                            next_run=future)
    tp = heartbeat.add_task(description="p", project_code="STA", objective="y",
                            next_run=past)
    sel = heartbeat.next_pending()
    assert sel is not None and sel["id"] == tp["id"]
    assert tf  # future one stays unselected


# === Finding 2: completion must not clobber a concurrent cancel ===

def test_finalize_task_skips_when_cancelled():
    t = heartbeat.add_task(description="c", project_code="STA", objective="x")
    heartbeat.update_task(t["id"], status="running")
    # Operator cancels mid-dispatch.
    assert heartbeat.cancel_task(t["id"])
    # The completion write must NOT resurrect it to done.
    res = heartbeat.finalize_task(t["id"], status="done", result="done")
    assert res is not None and res["status"] == "cancelled"
    assert heartbeat.get_task(t["id"])["status"] == "cancelled"


def test_finalize_task_writes_when_not_cancelled():
    t = heartbeat.add_task(description="c", project_code="STA", objective="x")
    heartbeat.update_task(t["id"], status="running")
    res = heartbeat.finalize_task(t["id"], status="done", result="ok")
    assert res is not None and res["status"] == "done"
    assert heartbeat.get_task(t["id"])["status"] == "done"


def test_run_task_honors_cancel_and_skips_done_and_requeue():
    t = heartbeat.add_task(
        description="c", project_code="STA", objective="x", every="1h",
    )
    heartbeat.update_task(t["id"], status="running")

    def _cb(project_code, objective, **kw):
        # Operator cancels the running slot while it is in flight.
        heartbeat.cancel_task(t["id"])
        return "produced tokens"

    hb = heartbeat.Heartbeat(dispatch_callback=_cb)
    hb._run_task(dict(heartbeat.get_task(t["id"])))

    assert heartbeat.get_task(t["id"])["status"] == "cancelled"
    # Cancelling the running slot ends the recurrence chain: no fresh pending.
    pendings = heartbeat.list_tasks(status="pending")
    assert pendings == []


# === Finding 1: claim is exclusive across processes ===

def _claim_worker(cfg_dir_str, vault_root_str, return_list):
    # Re-create the same isolated config in the child process.
    from modulatio import config as _config
    from modulatio import heartbeat as _hb
    from pathlib import Path

    _config.CONFIG_DIR = Path(cfg_dir_str)
    _config.DEFAULTS_FILE = Path(cfg_dir_str) / "defaults.json"
    _config.reload()
    claimed = _hb.claim_next_pending()
    return_list.append(claimed["id"] if claimed else None)


def test_claim_is_cross_process_exclusive(tmp_path):
    # One pending task; two processes race to claim it. Exactly one wins.
    heartbeat.add_task(description="solo", project_code="STA", objective="x")

    cfg_dir = str(config.CONFIG_DIR)
    vault_root = str(config.get_vault_root())

    ctx = mp.get_context("spawn")
    mgr = ctx.Manager()
    results = mgr.list()
    procs = [
        ctx.Process(target=_claim_worker, args=(cfg_dir, vault_root, results))
        for _ in range(2)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=30)

    claimed = [r for r in list(results) if r is not None]
    # At most one process may have claimed the single task.
    assert len(claimed) <= 1


# ═══ fold: test_heartbeat_r2_audit.py ═══
# Round-2 audit regressions for heartbeat (Cowboy Opus full-debug r2).
#
# Two MEDIUM defects fixed in src/modulatio/heartbeat.py:
#
# 1. parse_interval accepted a zero value ('0m'/'0h'/'0d') → timedelta(0) →
#    perpetual next_run==now → on the cron path an unbounded immediate-dispatch
#    runaway-cost loop. Fix: reject val<=0 at the parse source (return None).
#
# 2. requeue_recurring created the recurring child via add_task (next_run=None)
#    and only THEN set next_run in a separate lock window. Between those windows
#    the child was on disk eligible-now, so a concurrent claim_next_pending could
#    dispatch it before its interval next_run was set, bypassing recurrence. Fix:
#    add a next_run kwarg to add_task and have requeue_recurring pre-set it so the
#    child is persisted atomically with its interval already in place.




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


# ═══ fold: test_heartbeat_resweep.py ═══
# 0.9.0 pre-ship re-sweep regressions for ``heartbeat.py``.
#
# Two confirmed findings:
#
# - Finding 1 (race): the dispatch-failure retry path must not resurrect an
#   operator-cancelled task back to ``pending`` (which ``_select_next_pending``
#   would re-dispatch, defeating the cancel).
# - Finding 2 (correctness): a ``depends_on`` suffix must not false-satisfy
#   against an unrelated done task via a bare ``endswith`` over the whole id.




# === Finding 1: retry path must honor a mid-dispatch cancel ===

def test_retry_path_does_not_resurrect_cancelled_task():
    """A retryable dispatch failure that races with an operator cancel must
    leave the task ``cancelled`` — NOT re-arm it to ``pending``.

    The callback cancels the task out-of-band (as an operator would) and then
    raises, forcing the retries-remaining branch. Before the fix that branch
    used a plain ``update_task(status='pending')`` which clobbered the cancel.
    """
    task = heartbeat.add_task(
        description="x", project_code="STA", objective="o", max_retries=3,
    )

    def cancel_then_fail(project_code, objective, **kwargs):
        # Operator cancels the in-flight task under the lock, then dispatch fails.
        heartbeat.cancel_task(task["id"])
        raise RuntimeError("dispatch boom")

    hb = heartbeat.Heartbeat(dispatch_callback=cancel_then_fail)
    hb._run_task(dict(task))

    after = heartbeat.get_task(task["id"])
    assert after["status"] == "cancelled"
    # And it must NOT be re-selectable for dispatch.
    assert heartbeat.next_pending() is None


def test_retry_path_still_rearms_when_not_cancelled():
    """Guard the back-compat: a plain retryable failure (no cancel) must still
    re-arm the task to ``pending`` with bumped retries for the next tick."""
    task = heartbeat.add_task(
        description="x", project_code="STA", objective="o", max_retries=3,
    )

    def just_fail(project_code, objective, **kwargs):
        raise RuntimeError("dispatch boom")

    hb = heartbeat.Heartbeat(dispatch_callback=just_fail)
    hb._run_task(dict(task))

    after = heartbeat.get_task(task["id"])
    assert after["status"] == "pending"
    assert after["retries"] == 1
    assert after["started"] is None
    # Re-armed → selectable again.
    assert heartbeat.next_pending()["id"] == task["id"]


# === Finding 2: dependency suffix must not false-match an unrelated id ===

def test_short_dep_does_not_false_match_unrelated_done_task():
    """A short ``depends_on`` fragment that coincidentally tails an unrelated
    done task id must NOT satisfy the dependency.

    ``done`` is some completed task; we derive a short suffix from ITS id and
    attach it as a dependency on a different pending task. Before the fix the
    bare ``endswith`` satisfied it, dispatching the dependent prematurely.
    """
    done = heartbeat.add_task(description="dep", project_code="STA", objective="o")
    heartbeat.update_task(done["id"], status="done")

    # A short tail of the unrelated done id (shorter than the 6-hex token).
    short_suffix = done["id"][-2:]
    waiter = heartbeat.add_task(
        description="waiter",
        project_code="STA",
        objective="o2",
        depends_on=[short_suffix],
    )

    selected = heartbeat.next_pending()
    # The waiter's dep is unsatisfied (no done task whose id matches the FULL
    # token boundary for this fragment), so it must not be selected.
    assert selected is None or selected["id"] != waiter["id"]


def test_full_token_suffix_still_satisfies_dependency():
    """The documented safe case: a suffix spanning the full 6-hex random token
    of the actual predecessor still satisfies the dependency."""
    done = heartbeat.add_task(description="dep", project_code="STA", objective="o")
    heartbeat.update_task(done["id"], status="done")

    full_token = done["id"][-6:]  # the secrets.token_hex(3) segment
    waiter = heartbeat.add_task(
        description="waiter",
        project_code="STA",
        objective="o2",
        depends_on=[full_token],
    )

    selected = heartbeat.next_pending()
    assert selected is not None
    assert selected["id"] == waiter["id"]


def test_full_id_dep_satisfies_dependency():
    """An exact full-id dependency is honored."""
    done = heartbeat.add_task(description="dep", project_code="STA", objective="o")
    heartbeat.update_task(done["id"], status="done")
    waiter = heartbeat.add_task(
        description="waiter",
        project_code="STA",
        objective="o2",
        depends_on=[done["id"]],
    )
    selected = heartbeat.next_pending()
    assert selected is not None and selected["id"] == waiter["id"]


# ═══ fold: test_heartbeat_resweep_r3.py ═══
# 0.9.0 pre-ship round-3 re-sweep regressions for ``heartbeat.py``.
#
# Finding 1 (MEDIUM/race): every queue read-modify-write — not just
# ``claim_next_pending`` — must hold the cross-process ``flock`` and write through
# a per-process-unique tmp. Before the fix, ``add_task``/``update_task``/
# ``finalize_task``/``requeue_task``/``clear_done``/``recover_stale_tasks``
# serialized with the in-process ``RLock`` ONLY (invisible across OS processes),
# so two processes could lose each other's update (lost update) and a shared
# ``.json.tmp`` could publish another process's half-written bytes (torn file).




# === Cross-process lost-update: every mutator holds the flock ===

# A worker run in a SEPARATE OS process: it repeatedly adds tasks to the shared
# on-disk queue. The in-process RLock cannot serialize these against the parent
# process; only the cross-process flock (now held by add_task) can. Without the
# flock on add_task, concurrent load→append→save cycles drop appends.
_WORKER = """
import os, sys
cfg_dir = sys.argv[1]
vault_root = sys.argv[2]
n = int(sys.argv[3])
from modulatio import config
from pathlib import Path
config.CONFIG_DIR = Path(cfg_dir)
config.DEFAULTS_FILE = Path(cfg_dir) / "defaults.json"
config.reload()
from modulatio import heartbeat
for i in range(n):
    heartbeat.add_task(description="w%d" % i, project_code="STA", objective="o")
"""


def _write_worker(tmp_path):
    p = tmp_path / "worker.py"
    p.write_text(_WORKER, encoding="utf-8")
    return p


def test_concurrent_processes_do_not_lose_appends(tmp_path):
    """Two OS processes each appending N tasks to the same queue must end with
    2*N tasks — no lost updates. Exercises the cross-process flock now wrapping
    ``add_task`` (the in-process RLock cannot see across processes)."""
    worker = _write_worker(tmp_path)
    cfg_dir = str(config.CONFIG_DIR)
    vault_root = str(tmp_path / "vault")
    n = 40
    env = dict(os.environ)
    # Make sure the worker imports the same source tree.
    procs = [
        subprocess.Popen(
            [sys.executable, str(worker), cfg_dir, vault_root, str(n)],
            env=env,
        )
        for _ in range(2)
    ]
    for p in procs:
        assert p.wait(timeout=60) == 0
    config.reload()
    tasks = heartbeat.list_tasks()
    assert len(tasks) == 2 * n, (
        f"expected {2 * n} tasks, got {len(tasks)} — concurrent appends were lost"
    )


def test_queue_file_never_torn_under_concurrent_writers(tmp_path):
    """While many threads (proxy for processes via a real file) hammer the
    queue with RMW mutations, an independent reader must ALWAYS parse a complete
    JSON document — never a half-written/truncated file. The per-process-unique
    tmp + atomic rename guarantee the published file is always whole."""
    # Seed a chunky queue so writes take long enough to interleave.
    for i in range(30):
        heartbeat.add_task(description="seed%d" % i, project_code="STA", objective="o")

    qf = heartbeat._queue_file()
    stop = threading.Event()
    torn = []
    _terrs = []

    def writer():
        try:
            while not stop.is_set():
                heartbeat.update_task(
                    heartbeat.list_tasks()[0]["id"], description="bump"
                )
        except BaseException as _e:  # noqa: BLE001
            _terrs.append(_e)

    def reader():
        try:
          while not stop.is_set():
            try:
                raw = qf.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if not raw:
                continue
            try:
                json.loads(raw)
            except json.JSONDecodeError:
                torn.append(raw[:80])
        except BaseException as _e:  # noqa: BLE001
            _terrs.append(_e)

    ws = [threading.Thread(target=writer) for _ in range(4)]
    rs = [threading.Thread(target=reader) for _ in range(2)]
    for t in ws + rs:
        t.start()
    time.sleep(1.0)
    stop.set()
    for t in ws + rs:
        t.join(timeout=10)
    assert not _terrs, f"writer/reader thread raised: {_terrs!r}"
    assert not torn, f"reader observed a torn queue file: {torn[:3]}"


# === Reentrancy: a mutator that nests another must not self-deadlock ===

def test_cross_process_lock_is_reentrant(tmp_path):
    """The cross-process flock must be REENTRANT within a process. A second
    ``flock(LOCK_EX)`` on a fresh fd in the same process blocks against the
    process's own held lock, so a nested acquisition (e.g. a locked mutator
    calling another locked mutator) would self-deadlock without reentrancy.
    Nest two acquisitions directly and require the inner one not to hang."""
    done = threading.Event()

    _rerrs = []

    def run():
        try:
            with heartbeat._cross_process_claim_lock():
                with heartbeat._cross_process_claim_lock():
                    # If non-reentrant, the inner acquire blocks forever here.
                    heartbeat.add_task(
                        description="x", project_code="STA", objective="o"
                    )
            done.set()
        except BaseException as _e:  # noqa: BLE001
            _rerrs.append(_e)
            done.set()

    th = threading.Thread(target=run, daemon=True)
    th.start()
    th.join(timeout=10)
    assert not _rerrs, f"nested-lock thread raised: {_rerrs!r}"
    assert done.is_set(), (
        "nested cross-process lock acquisition deadlocked (not reentrant)"
    )
    assert len(heartbeat.list_tasks()) == 1


def test_no_tmp_files_leak_after_writes(tmp_path):
    """Per-process-unique tmp files must be renamed/cleaned, not left behind."""
    for i in range(5):
        heartbeat.add_task(description="t%d" % i, project_code="STA", objective="o")
    qdir = heartbeat._queue_file().parent
    leftovers = list(qdir.glob("heartbeat-queue.json.tmp*"))
    assert not leftovers, f"leaked tmp files: {leftovers}"
