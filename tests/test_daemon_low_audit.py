"""LOW-audit regressions for daemon.start() (findings #58, #59).

#58 [race] start() re-reads the PID file after is_running() returned True;
    a concurrent stop()/daemon-exit can unlink/truncate the file between the
    two reads, so the unguarded int(read_text()) crashed (ValueError/OSError).
    The fix guards the re-read and falls through to start a fresh daemon.

#59 [resource-leak] the daemonizing child rebound sys.stdin/stdout/stderr
    to new file objects but never closed the inherited terminal streams,
    leaking those objects (and the controlling-terminal fds) for the
    daemon's lifetime. The fix closes the originals after redirecting.

Neither test actually forks a real daemon; both drive start()'s control
flow with monkeypatched primitives.
"""

from __future__ import annotations

import io
import os

import pytest

from modulatio import config, daemon, telegram_notify


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "config"
    monkeypatch.setattr(config, "CONFIG_DIR", cfg_dir)
    monkeypatch.setattr(telegram_notify, "CONFIG_FILE", cfg_dir / "telegram-config.json")
    yield
    pf = config.CONFIG_DIR / "daemon.pid"
    if pf.exists():
        try:
            pf.unlink()
        except OSError:
            pass


# === #58: TOCTOU re-read guard ===

def test_start_survives_pid_file_vanishing_after_is_running(monkeypatch):
    """is_running() says True, but the PID file is gone by the re-read
    (concurrent stop/daemon-exit). Pre-fix: int(read_text()) raised
    FileNotFoundError. Post-fix: start() falls through and forks a fresh
    daemon (we stub fork to the parent path) instead of crashing."""
    monkeypatch.setattr(daemon, "is_running", lambda: True)
    # PID file does NOT exist -> read_text() raises FileNotFoundError(OSError).
    assert not daemon._pid_file().exists()
    # Stub the fork so we take the parent branch and never spawn a child.
    monkeypatch.setattr(os, "fork", lambda: 4321)
    monkeypatch.setattr(daemon.time, "sleep", lambda *_a, **_k: None)

    # Must NOT raise; falls through to the (stubbed) fork and returns its pid.
    pid = daemon.start(stub=True)
    assert pid == 4321


def test_start_survives_malformed_pid_file_after_is_running(monkeypatch):
    """is_running() True but the PID file is truncated/garbage at re-read
    time -> int() would raise ValueError pre-fix. Post-fix: guarded."""
    monkeypatch.setattr(daemon, "is_running", lambda: True)
    pf = daemon._pid_file()
    pf.parent.mkdir(parents=True, exist_ok=True)
    pf.write_text("not-a-pid")
    monkeypatch.setattr(os, "fork", lambda: 8765)
    monkeypatch.setattr(daemon.time, "sleep", lambda *_a, **_k: None)

    pid = daemon.start(stub=True)
    assert pid == 8765


def test_start_returns_existing_pid_when_running_and_readable(monkeypatch):
    """Happy path preserved: running daemon with a valid PID file returns
    that pid without forking."""
    monkeypatch.setattr(daemon, "is_running", lambda: True)
    pf = daemon._pid_file()
    pf.parent.mkdir(parents=True, exist_ok=True)
    pf.write_text("12345")

    def _no_fork():
        raise AssertionError("start() must NOT fork when daemon already running")

    monkeypatch.setattr(os, "fork", _no_fork)
    assert daemon.start(stub=True) == 12345


# === #59: inherited stdio streams closed in the daemon child ===

def test_child_closes_inherited_stdio_streams(tmp_path, monkeypatch):
    """Drive start()'s child branch (fork -> 0) far enough to exercise the
    redirect + close loop, then assert the inherited terminal streams were
    closed. Pre-fix they leaked open."""
    monkeypatch.setattr(daemon, "is_running", lambda: False)
    monkeypatch.setattr(os, "fork", lambda: 0)  # child branch
    monkeypatch.setattr(os, "setsid", lambda: None)
    # The child opens the log file before mkdir-ing CONFIG_DIR; in production
    # the dir already exists. Ensure it does here.
    config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    old_in = io.StringIO()
    old_out = io.StringIO()
    old_err = io.StringIO()
    monkeypatch.setattr(daemon.sys, "stdin", old_in)
    monkeypatch.setattr(daemon.sys, "stdout", old_out)
    monkeypatch.setattr(daemon.sys, "stderr", old_err)

    # Stop the child before it actually runs the daemon loop / os._exit.
    class _StopHere(Exception):
        pass

    def _boom(**_k):
        raise _StopHere()

    monkeypatch.setattr(daemon, "_run_daemon", _boom)
    monkeypatch.setattr(os, "_exit", lambda *_a: (_ for _ in ()).throw(_StopHere()))

    with pytest.raises(_StopHere):
        daemon.start(stub=True)

    assert old_in.closed, "inherited stdin was not closed (leak)"
    assert old_out.closed, "inherited stdout was not closed (leak)"
    assert old_err.closed, "inherited stderr was not closed (leak)"
