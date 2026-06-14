# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Pre-ship 0.9.0 regression tests for daemon.py.

Covers two confirmed findings:
  - MEDIUM: daemon log opened before CONFIG_DIR exists (fresh-install crash).
  - LOW: module-global _shutdown Event never cleared at _run_daemon entry.
"""
from __future__ import annotations

import pytest

from modulatio import config, daemon, telegram_notify


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "config"
    monkeypatch.setattr(config, "CONFIG_DIR", cfg_dir)
    monkeypatch.setattr(config, "DEFAULTS_FILE", cfg_dir / "defaults.json")
    monkeypatch.setattr(
        telegram_notify, "CONFIG_FILE", cfg_dir / "telegram-config.json"
    )
    yield


# === MEDIUM: log opened before CONFIG_DIR is created ===

def test_open_log_for_append_creates_config_dir_first():
    """On a fresh install CONFIG_DIR does not exist. _open_log_for_append must
    mkdir it BEFORE opening _log_file(), otherwise open() raises
    FileNotFoundError in the forked daemon child while the parent has already
    reported (false) success. This is the exact ordering the daemon child
    relies on, factored out so it is testable without forking/stdio hijack."""
    assert not config.CONFIG_DIR.exists(), "precondition: fresh install"

    # Before the fix this raised FileNotFoundError (mkdir came after the open).
    f = daemon._open_log_for_append()
    try:
        assert config.CONFIG_DIR.exists(), "log open must create CONFIG_DIR first"
        assert daemon._log_file().exists()
        f.write("regression-marker\n")
    finally:
        f.close()

    assert "regression-marker" in daemon._log_file().read_text(encoding="utf-8")


# === LOW: _shutdown Event not cleared at _run_daemon entry ===

def test_run_daemon_clears_stale_shutdown_flag(monkeypatch):
    """A set() _shutdown from a prior run must not make a fresh _run_daemon
    exit its loop immediately. _run_daemon should clear it on entry."""
    # Simulate a leftover shutdown flag from a previous run.
    daemon._shutdown.set()
    assert daemon._shutdown.is_set()

    # Stub everything _run_daemon touches so the loop body is inert and we can
    # observe whether the flag was cleared. We capture the flag state at the
    # moment the loop's wait() is first reached, then trip it to exit.
    seen = {}

    class _FakeHB:
        def start(self):
            pass

        def stop(self):
            pass

    monkeypatch.setattr(daemon.signal, "signal", lambda *a, **k: None)
    monkeypatch.setattr(daemon.heartbeat, "Heartbeat", lambda **kw: _FakeHB())
    monkeypatch.setattr(daemon, "_make_dispatch_callback", lambda **kw: (lambda *a, **k: ""))
    monkeypatch.setattr(daemon, "_maybe_start_telegram_listener", lambda: None)
    monkeypatch.setattr(daemon.telegram_notify, "notify_event", lambda **kw: None)
    monkeypatch.setattr(daemon.cron, "dispatch_due", lambda: [])

    class _FakePE:
        def tick(self, **kw):
            return []

    monkeypatch.setattr(daemon, "_project_execution_module", lambda: _FakePE())
    monkeypatch.setattr(daemon, "_make_project_loader", lambda **kw: (lambda c: None))
    monkeypatch.setattr(daemon, "_make_runners_for", lambda **kw: (lambda p: {}))

    real_wait = daemon._shutdown.wait

    def _wait(timeout=None):
        # On the first loop iteration, record whether the stale flag survived,
        # then set it so the loop terminates on the next is_set() check.
        seen["was_set_at_loop_entry"] = daemon._shutdown.is_set()
        daemon._shutdown.set()
        return real_wait(0)

    monkeypatch.setattr(daemon._shutdown, "wait", _wait)

    daemon._run_daemon(stub=True)

    # If the clear() were missing, the while-loop guard would have seen the
    # stale set() flag and never entered the body, so _wait would not run.
    assert "was_set_at_loop_entry" in seen, "loop body never ran — stale flag not cleared"
    assert seen["was_set_at_loop_entry"] is False, "_shutdown should be cleared on entry"

    # Leave the global in a clean state for other tests.
    daemon._shutdown.clear()
