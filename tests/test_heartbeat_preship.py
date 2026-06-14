"""0.9.0 pre-ship sweep regressions for ``heartbeat.py``.

Three findings:
  1. [race]        claim_next_pending must be exclusive ACROSS processes,
                   not just threads (cross-process flock).
  2. [correctness] A done/failed write must not clobber a status=cancelled
                   set out-of-band mid-dispatch.
  3. [correctness] An unparseable next_run must be skipped (not eligible),
                   never silently run-now (which busy-loops a recurrence).
"""

from __future__ import annotations

import multiprocessing as mp

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
