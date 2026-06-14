"""Round-3 re-sweep regressions for src/modulatio/auth_alerts.py.

Finding (MEDIUM/race): raise_alert/clear_alert/clear_all did
load_alerts() -> mutate -> save_alerts() with no lock spanning the
read-modify-write, and save_alerts is an atomic *replace*, not an atomic
update. Under the concurrent daemon (heartbeat + cron + Telegram listener)
and the parallel wave executor, two writers each load the same baseline and
one's atomic replace clobbers the other's change.

These tests are additive and live in a dedicated _r3 file so they don't
collide with the existing tests/test_auth_alerts.py or any prior _resweep file.
"""

from __future__ import annotations

import threading

import pytest

from modulatio import auth_alerts, config
from tests._thread_check import run_threads_checked


@pytest.fixture(autouse=True)
def isolate_alerts(tmp_path, monkeypatch):
    cfg = tmp_path / "config"
    monkeypatch.setattr(config, "CONFIG_DIR", cfg)
    monkeypatch.setattr(config, "AUTH_ALERTS_FILE", cfg / "auth_alerts.json")


@pytest.fixture(autouse=True)
def disable_external_channels(monkeypatch):
    monkeypatch.setattr(auth_alerts, "_try_desktop_notification", lambda *a, **k: None)
    monkeypatch.setattr(auth_alerts, "_try_telegram_notification", lambda *a, **k: None)


def test_interleaved_save_does_not_clobber(monkeypatch):
    """Force the classic lost-update interleaving from a SEPARATE thread: while
    the main writer is paused between its load and its save, a second thread
    commits a change to a DIFFERENT provider.

    Without a lock spanning load->mutate->save (and a re-read inside it), the
    main writer's save — built from a snapshot taken before the second thread's
    commit — atomically replaces the file and wipes that provider. With the
    lock the second thread can't even commit until the main writer releases,
    and the main writer's save reflects the latest state, so both survive.

    We pause the main writer mid-operation by stalling its first load_alerts()
    call; the second thread (a different thread, so the non-reentrant in-process
    lock blocks rather than deadlocks) then races the write. With the lock held
    across load->mutate->save, the second thread can't commit until the main
    writer releases, so the main save can't be built from a snapshot that
    pre-dates the second commit — both providers survive.
    """
    real_load = auth_alerts.load_alerts
    paused = threading.Event()
    release = threading.Event()
    first_load = {"seen": False}

    def stalling_load():
        # Stall only the main writer's FIRST load (the one inside its locked
        # region), then behave normally for everything else.
        if not first_load["seen"]:
            first_load["seen"] = True
            data = real_load()
            paused.set()
            release.wait(timeout=5)
            return data
        return real_load()

    monkeypatch.setattr(auth_alerts, "load_alerts", stalling_load)

    _errs = []

    def main_writer():
        try:
            auth_alerts.raise_alert("primary_provider", error_message="primary", auth_type="api_key")
        except BaseException as _e:  # noqa: BLE001 — surface to assert, no ghost warning
            _errs.append(_e)

    t = threading.Thread(target=main_writer)
    t.start()
    assert paused.wait(timeout=5)

    # Concurrent commit from this thread. Use the real loader so it reads true
    # on-disk state. With the fix it blocks on _ALERTS_LOCK until release; we
    # release immediately after kicking it off so the test can't wedge.
    monkeypatch.setattr(auth_alerts, "load_alerts", real_load)

    def concurrent_writer():
        try:
            auth_alerts.raise_alert("concurrent_provider", error_message="conc", auth_type="api_key")
        except BaseException as _e:  # noqa: BLE001
            _errs.append(_e)

    ct = threading.Thread(target=concurrent_writer)
    ct.start()
    # Give the concurrent writer a moment to either commit (no-lock: clobbers)
    # or block on the lock (fixed: waits), then let the main writer finish.
    ct.join(timeout=0.5)
    release.set()
    t.join(timeout=5)
    ct.join(timeout=5)

    assert not _errs, f"writer thread raised: {_errs!r}"
    final = real_load()
    assert "primary_provider" in final
    assert "concurrent_provider" in final


def test_concurrent_raises_all_persist(monkeypatch):
    """Many threads raising distinct-provider alerts at once: every alert must
    survive. A lost update (atomic replace from a stale snapshot) would drop
    some providers from the final file."""
    monkeypatch.setattr(auth_alerts, "load_alerts", auth_alerts.load_alerts)
    n = 24
    barrier = threading.Barrier(n)

    def worker(i):
        barrier.wait()
        auth_alerts.raise_alert(f"prov_{i}", error_message=f"e{i}", auth_type="api_key")

    run_threads_checked([(lambda i=i: worker(i)) for i in range(n)])

    final = auth_alerts.load_alerts()
    assert len(final) == n
    for i in range(n):
        assert f"prov_{i}" in final


def test_concurrent_clears_do_not_resurrect(monkeypatch):
    """Clearing one provider while another is raised concurrently must not
    resurrect the cleared one nor drop the raised one."""
    auth_alerts.raise_alert("to_clear", error_message="x", auth_type="api_key")
    n = 20
    barrier = threading.Barrier(n + 1)

    def raiser(i):
        barrier.wait()
        auth_alerts.raise_alert(f"keep_{i}", error_message="k", auth_type="api_key")

    def clearer():
        barrier.wait()
        auth_alerts.clear_alert("to_clear")

    run_threads_checked([(lambda i=i: raiser(i)) for i in range(n)] + [clearer])

    final = auth_alerts.load_alerts()
    assert "to_clear" not in final
    assert len(final) == n
    for i in range(n):
        assert f"keep_{i}" in final


def test_single_flight_uses_sidecar_lock_file(monkeypatch):
    """The cross-process lock path is a sidecar beside the alerts file, and the
    in-process lock is genuinely held inside the context manager (so it
    serializes daemon-vs-cron-vs-listener-vs-wave)."""
    assert auth_alerts._ALERTS_LOCK.acquire(blocking=False)
    auth_alerts._ALERTS_LOCK.release()

    with auth_alerts._alerts_single_flight():
        # While inside, the in-process lock must be held.
        assert not auth_alerts._ALERTS_LOCK.acquire(blocking=False)
