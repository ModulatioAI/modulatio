"""Round-3 cron re-sweep regressions (additive to test_cron_resweep.py).

Finding 1 [MEDIUM/race, cron.py:129] — the cross-process dispatch flock was held
ONLY by dispatch_due. The CLI-facing mutators add/update/remove (and
enable/disable via update) took only the in-process _cron_lock RLock, which can't
see across the OS-process boundary. A daemon dispatch (which RMWs the config via
_dispatch_one -> update) and a concurrent `modulatio cron add/update/remove`
process could interleave their load/modify/save and lose a write. Fix: wrap the
mutators in the same _cross_process_dispatch_lock, made re-entrant per thread so
the dispatch path (which already holds it) can still call update without
deadlocking against a fresh-fd flock.

Finding 2 [LOW/integration, cron.py:332] — cron.add only .upper()'d project_code,
never validating its shape (heartbeat.add_task does). A malformed/path-hostile
code was accepted at add-time then rejected on every headless dispatch. Fix:
validate up front via vault.validate_project_code so the operator is told
immediately.
"""

from __future__ import annotations

import fcntl
import os
import threading
from datetime import datetime, timedelta, timezone

import pytest

from modulatio import config, cron
from modulatio import vault
from tests._thread_check import run_threads_checked


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "config"
    monkeypatch.setattr(config, "CONFIG_DIR", cfg_dir)
    monkeypatch.setattr(config, "DEFAULTS_FILE", cfg_dir / "defaults.json")
    config.save_defaults({"vault_root": str(tmp_path / "vault")})
    config.reload()
    vault.init_project("TEST", "TEST", "o", exist_ok=True)
    yield


def _nonblocking_flock_is_blocked() -> bool:
    """True if a fresh-fd non-blocking LOCK_EX on the sidecar fails — i.e. the
    cross-process dispatch lock is currently held by someone."""
    lock_path = cron._dispatch_lock_file()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
            return False
        except (BlockingIOError, OSError):
            return True
    finally:
        os.close(fd)


# --- Finding 1: mutators hold the cross-process lock during their RMW ---


def test_add_holds_cross_process_lock_during_rmw(monkeypatch):
    """While cron.add does its load-append-save, the sidecar flock must be held
    exclusively. We probe it from inside _save (the write seam) using a fresh fd
    in another thread, so the non-blocking acquire there must fail. Without the
    fix add takes only the in-process RLock and the probe succeeds."""
    observed = {}
    real_save = cron._save

    def probing_save(jobs):
        # Probe from a SEPARATE thread: same-thread re-entrant acquire would ride
        # the held flock and not reflect cross-process exclusion.
        result = {}

        def probe():
            result["blocked"] = _nonblocking_flock_is_blocked()

        run_threads_checked([probe])
        observed["blocked_during_save"] = result["blocked"]
        return real_save(jobs)

    monkeypatch.setattr(cron, "_save", probing_save)

    cron.add(name="j", schedule="1d", project_code="test", objective="do it")
    assert observed.get("blocked_during_save") is True


def test_update_holds_cross_process_lock_during_rmw(monkeypatch):
    job = cron.add(name="j", schedule="1d", project_code="test", objective="do it")
    observed = {}
    real_save = cron._save

    def probing_save(jobs):
        result = {}

        def probe():
            result["blocked"] = _nonblocking_flock_is_blocked()

        run_threads_checked([probe])
        observed["blocked_during_save"] = result["blocked"]
        return real_save(jobs)

    monkeypatch.setattr(cron, "_save", probing_save)

    cron.update(job["id"], priority=9)
    assert observed.get("blocked_during_save") is True


def test_remove_holds_cross_process_lock_during_rmw(monkeypatch):
    job = cron.add(name="j", schedule="1d", project_code="test", objective="do it")
    observed = {}
    real_save = cron._save

    def probing_save(jobs):
        result = {}

        def probe():
            result["blocked"] = _nonblocking_flock_is_blocked()

        run_threads_checked([probe])
        observed["blocked_during_save"] = result["blocked"]
        return real_save(jobs)

    monkeypatch.setattr(cron, "_save", probing_save)

    assert cron.remove(job["id"]) is True
    assert observed.get("blocked_during_save") is True


# --- Finding 1: re-entrancy — dispatch_due -> _dispatch_one -> update must NOT
#     deadlock against the outer flock, and must still fire exactly once. ---


def test_dispatch_due_does_not_deadlock_on_nested_update(monkeypatch):
    """dispatch_due holds the flock, then _dispatch_one calls update() which now
    ALSO takes the flock. A non-re-entrant flock would deadlock (two fresh-fd
    LOCK_EX from the same process block). The re-entrant guard must let the
    nested update proceed and the dispatch complete within a hard timeout."""
    job = cron.add(name="j", schedule="1d", project_code="test", objective="do it")
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(timespec="seconds")
    cron.update(job["id"], next_run=past)

    monkeypatch.setattr(cron.heartbeat, "add_task", lambda **kw: {"id": "t"})

    result = {}

    _errs = []

    def run():
        try:
            result["fired"] = cron.dispatch_due()
        except BaseException as _e:  # noqa: BLE001 — surface to assert, no ghost warning
            _errs.append(_e)

    t = threading.Thread(target=run)
    t.start()
    t.join(timeout=10)
    assert not _errs, f"dispatch_due thread raised: {_errs!r}"
    assert not t.is_alive(), "dispatch_due deadlocked on the nested update flock"
    assert len(result["fired"]) == 1

    # And next_run was actually advanced past now (the nested update took effect).
    advanced = cron.get(job["id"])
    assert datetime.fromisoformat(advanced["next_run"]) > datetime.now(timezone.utc)


def test_lock_releases_after_mutator_so_dispatch_can_still_acquire(monkeypatch):
    """After add/update/remove return, the cross-process flock must be fully
    released (fd closed, lock dropped) — a leaked hold would wedge the daemon's
    next dispatch_due. Probe from a fresh fd after each call: must be free."""
    cron.add(name="j", schedule="1d", project_code="test", objective="do it")
    assert _nonblocking_flock_is_blocked() is False
    job2 = cron.add(name="k", schedule="1d", project_code="test", objective="do it")
    assert _nonblocking_flock_is_blocked() is False
    cron.update(job2["id"], priority=3)
    assert _nonblocking_flock_is_blocked() is False
    cron.remove(job2["id"])
    assert _nonblocking_flock_is_blocked() is False


def test_no_fd_leak_across_many_mutations(monkeypatch):
    """Each outermost lock acquisition opens an fd; it must be closed on release.
    Hammer the mutators well past a typical soft fd budget and assert the open-fd
    count for THIS process stays bounded (no per-call leak)."""
    fd_dir = f"/proc/{os.getpid()}/fd"
    if not os.path.isdir(fd_dir):  # pragma: no cover — non-Linux
        pytest.skip("/proc fd introspection unavailable")

    before = len(os.listdir(fd_dir))
    for i in range(300):
        j = cron.add(name=f"j{i}", schedule="1d", project_code="test", objective="x")
        cron.update(j["id"], priority=(i % 9) + 1)
        cron.remove(j["id"])
    after = len(os.listdir(fd_dir))
    # Allow a small slack for incidental fds; a leak would be ~900.
    assert after - before < 20, f"fd leak: {before} -> {after}"


# --- Finding 2: cron.add validates project_code shape at add-time ---


@pytest.mark.parametrize(
    "bad_code",
    [
        "../etc",          # path traversal
        "bad code",        # whitespace
        "9starts_digit",   # must start with a letter
        "has/slash",       # path separator
        "x" * 33,          # too long (>32)
        "",                # empty
    ],
)
def test_add_rejects_malformed_project_code(bad_code):
    """A malformed/path-hostile project code must raise ValueError at add-time
    (operator present), mirroring heartbeat.add_task — not be silently accepted
    and rejected on every headless dispatch. Without the fix add() stored it."""
    with pytest.raises(ValueError):
        cron.add(name="j", schedule="1d", project_code=bad_code, objective="do it")
    # Nothing was persisted.
    assert cron.list_jobs() == []


def test_add_accepts_valid_code_and_stores_uppercased():
    """A valid code (case-permissive) is accepted and stored upper, as before."""
    job = cron.add(name="j", schedule="1d", project_code="myproj", objective="do it")
    assert job["project_code"] == "MYPROJ"
    job2 = cron.add(name="k", schedule="1d", project_code="MixedCase1", objective="x")
    assert job2["project_code"] == "MIXEDCASE1"


def test_added_job_dispatches_without_code_error(monkeypatch):
    """End-to-end: a job added with a valid code dispatches cleanly (the code is
    already validated, so heartbeat.add_task's own validate won't reject it)."""
    job = cron.add(name="j", schedule="1d", project_code="test", objective="do it")
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(timespec="seconds")
    cron.update(job["id"], next_run=past)

    seen = {}

    def fake_add_task(**kwargs):
        seen["project_code"] = kwargs["project_code"]
        return {"id": "t"}

    monkeypatch.setattr(cron.heartbeat, "add_task", fake_add_task)
    fired = cron.dispatch_due()
    assert len(fired) == 1
    assert seen["project_code"] == "TEST"
