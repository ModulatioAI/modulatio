"""Regression: cron.dispatch_due must not double-fire a due job across
concurrent OS processes (finding 1, MEDIUM/race, cron.py:419).

`dispatch_due` ran check_due (load-decide-release) → add_task → update across
three separate in-process-lock windows, and `_cron_lock` is an in-process RLock
only. The daemon's per-tick `cron.dispatch_due()` and a separate `modulatio
cron dispatch-due` CLI invocation are distinct OS processes sharing the same
on-disk cron-config; both could observe the same job as due and both fire
add_task (duplicate kickoff, duplicate cost). The fix wraps the
select-advance-dispatch window in a cross-process POSIX flock.
"""

from __future__ import annotations

import multiprocessing as mp
import os
import time
from datetime import datetime, timedelta, timezone

import pytest

from modulatio import config, cron
from modulatio import vault


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "config"
    monkeypatch.setattr(config, "CONFIG_DIR", cfg_dir)
    monkeypatch.setattr(config, "DEFAULTS_FILE", cfg_dir / "defaults.json")
    config.save_defaults({"vault_root": str(tmp_path / "vault")})
    config.reload()
    vault.init_project("TEST", "TEST", "o", exist_ok=True)
    yield


def _add_overdue_job() -> dict:
    job = cron.add(
        name="resweep-job",
        schedule="1d",
        project_code="TEST",
        objective="do the thing",
    )
    # Pin next_run into the past so it is unambiguously due.
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(timespec="seconds")
    cron.update(job["id"], next_run=past)
    return job


def test_dispatch_holds_cross_process_lock_during_window(monkeypatch):
    """While dispatch_due is selecting-advancing-dispatching, the sidecar lock
    file must be exclusively held — proven by a NON-BLOCKING flock from a fresh
    fd failing inside the add_task seam. Without the fix there is no flock and
    the non-blocking acquire succeeds.
    """
    import fcntl

    _add_overdue_job()

    observed = {}

    def fake_add_task(**kwargs):
        lock_path = cron._dispatch_lock_file()
        fd = os.open(str(lock_path), os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                # Acquired ⇒ the dispatch window is NOT protecting it.
                observed["locked_during_dispatch"] = False
                fcntl.flock(fd, fcntl.LOCK_UN)
            except (BlockingIOError, OSError):
                observed["locked_during_dispatch"] = True
        finally:
            os.close(fd)
        return {"id": "task-x"}

    monkeypatch.setattr(cron.heartbeat, "add_task", fake_add_task)

    fired = cron.dispatch_due()
    assert len(fired) == 1
    assert observed.get("locked_during_dispatch") is True


# --- True cross-process double-fire test (subprocess workers) ---

def _worker(cfg_dir_str, count_file, ready, go):
    """Run in a separate OS process: configure the same on-disk cron-config and
    call dispatch_due. add_task appends a marker line to a shared file so the
    parent can count total fires across both processes. A barrier makes the two
    workers' check_due windows overlap so the race is exercised."""
    from pathlib import Path

    from modulatio import config as _config
    from modulatio import cron as _cron
    from modulatio import vault as _vault

    _config.CONFIG_DIR = Path(cfg_dir_str)
    _config.DEFAULTS_FILE = Path(cfg_dir_str) / "defaults.json"
    _config.reload()
    _vault.reload()  # sync VAULT_ROOT (a real daemon process does this at startup)

    def fake_add_task(**kwargs):
        # Widen the window between check_due and the advancing update so both
        # workers, absent a cross-process lock, would both get here.
        with open(count_file, "a", encoding="utf-8") as fh:
            fh.write("fire\n")
        time.sleep(0.3)
        return {"id": "t"}

    _cron.heartbeat.add_task = fake_add_task  # type: ignore[assignment]

    ready.set()
    go.wait(5)
    _cron.dispatch_due()


def test_concurrent_processes_fire_due_job_once(tmp_path):
    """Two real OS processes calling dispatch_due on the same overdue job must
    produce exactly ONE heartbeat add_task — the cross-process flock makes the
    loser re-read an already-advanced next_run and skip it."""
    if not hasattr(__import__("fcntl"), "flock"):  # pragma: no cover
        pytest.skip("flock unavailable")

    cfg_dir = tmp_path / "config"
    vault_root = tmp_path / "vault"
    config.CONFIG_DIR = cfg_dir
    config.DEFAULTS_FILE = cfg_dir / "defaults.json"
    config.save_defaults({"vault_root": str(vault_root)})
    config.reload()
    vault.reload()  # sync VAULT_ROOT to this test's vault (matches the subprocess)
    vault.init_project("TEST", "TEST", "o", exist_ok=True)  # the cron's project must exist
    _add_overdue_job()

    count_file = tmp_path / "fires.txt"
    count_file.write_text("", encoding="utf-8")

    ctx = mp.get_context("spawn")
    ready1, ready2 = ctx.Event(), ctx.Event()
    go = ctx.Event()
    args = (str(cfg_dir), str(count_file))
    p1 = ctx.Process(target=_worker, args=(*args, ready1, go))
    p2 = ctx.Process(target=_worker, args=(*args, ready2, go))
    p1.start()
    p2.start()
    ready1.wait(5)
    ready2.wait(5)
    go.set()
    p1.join(10)
    p2.join(10)

    fires = [ln for ln in count_file.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(fires) == 1, f"expected exactly one fire, got {len(fires)}"
