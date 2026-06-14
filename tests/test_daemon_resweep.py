"""0.9.0 pre-ship re-sweep regression tests for src/modulatio/daemon.py.

Dedicated file (do not merge into the existing daemon test modules) so the
re-sweep work stays isolated.

Finding #1 (LOW/error-path): the post-fork child body ran log-open + PID-write
BEFORE the try/finally that calls os._exit(). If one of those raised (full /
read-only disk), the exception unwound out of the fork and ran parent-side
CLI/atexit teardown INSIDE the forked child. The fix wraps the whole child
body so any startup failure lands on os._exit(1) instead of unwinding.

These tests actually fork so they observe the real process exit status — the
whole point of the fix is what happens to the CHILD process on failure, which
a pure mock cannot prove.
"""

from __future__ import annotations

import os

import pytest

from modulatio import config, daemon, telegram_notify


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "config"
    monkeypatch.setattr(config, "CONFIG_DIR", cfg_dir)
    monkeypatch.setattr(config, "DEFAULTS_FILE", cfg_dir / "defaults.json")
    monkeypatch.setattr(telegram_notify, "CONFIG_FILE", cfg_dir / "telegram-config.json")


def _run_child_in_subprocess(target) -> int:
    """Fork, run ``target()`` in the child, and return the child's wait status
    code. ``target`` is expected to terminate the child via os._exit(); if it
    instead RETURNS (the pre-fix unwind behavior), we mark that distinctly so
    the test can fail with a clear message.
    """
    pid = os.fork()
    if pid == 0:
        # Child. If target returns instead of os._exit()-ing, exit 99 to signal
        # "control flowed back out of the child body" (the bug).
        try:
            target()
        except BaseException:
            os._exit(98)  # raised back into us without being caught (also a bug)
        os._exit(99)  # returned normally without os._exit() (the unwind bug)
    _, status = os.waitpid(pid, 0)
    return os.waitstatus_to_exitcode(status)


def test_child_main_exits_1_when_log_open_fails(monkeypatch):
    """A failing log open in the detached child must exit(1), not unwind.

    Without the fix, _open_log_for_append's exception propagates out of the
    child body; here it would surface as exit code 98 (raised back) since the
    pre-fix code had no wrapping try/except around the log open. With the fix,
    _child_main catches it and calls os._exit(1).
    """
    def boom():
        raise OSError("simulated disk full opening daemon.log")

    monkeypatch.setattr(daemon, "_open_log_for_append", boom)

    code = _run_child_in_subprocess(lambda: daemon._child_main(stub=True))
    assert code == 1, (
        f"child should os._exit(1) on log-open failure, got exit code {code} "
        "(98=raised out of child, 99=returned out of child — both are the "
        "unwind-into-parent-teardown bug)"
    )


def test_child_main_exits_1_when_pid_write_fails(tmp_path, monkeypatch):
    """A failing PID-file write in the detached child must exit(1), not unwind.

    The PID write happens AFTER stdout has been redirected to the log, exercising
    the later-failure path: logger.exception in _child_main must not itself crash
    and the child must still exit(1). The log open returns a REAL file (with a
    usable fileno() for the dup2 detach step) so the failure point is the PID
    write, not the stream setup.
    """
    log_path = tmp_path / "daemon.log"

    def open_real_log():
        return open(log_path, "a", buffering=1)

    monkeypatch.setattr(daemon, "_open_log_for_append", open_real_log)

    real_write_text = daemon.Path.write_text

    def write_text_guard(self, *args, **kwargs):
        if self.name == "daemon.pid":
            raise OSError("simulated read-only fs writing daemon.pid")
        return real_write_text(self, *args, **kwargs)

    monkeypatch.setattr(daemon.Path, "write_text", write_text_guard)

    code = _run_child_in_subprocess(lambda: daemon._child_main(stub=True))
    assert code == 1, (
        f"child should os._exit(1) on PID-write failure, got exit code {code}"
    )
