"""Re-sweep (0.9.0 pre-ship) regression tests for ``modulatio.tools``.

Uniquely named to avoid colliding with concurrent agents editing the
primary ``test_tools.py`` and the other audit modules.

Covers one confirmed re-sweep finding:

  1. [LOW/resource-leak] On run_shell's give-up drain branch (every
     ``communicate`` drain times out because a double-forking grandchild
     keeps the pipes open), the SIGKILL'd child was never reaped, so it
     lingered as a zombie until ``Popen.__del__`` ran under the chat
     loop's GC. The fix reaps it promptly with a non-blocking ``poll()``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from modulatio import tools


def _art(tmp_path: Path) -> Path:
    art = tmp_path / "artifacts"
    art.mkdir()
    return art


# ── Finding 1: give-up drain branch reaps the SIGKILL'd child ──────────

def test_give_up_branch_reaps_child(tmp_path, monkeypatch):
    """When every drain ``communicate`` times out, run_shell must reap the
    SIGKILL'd child (a non-blocking ``poll()``) rather than leave it as a
    zombie for ``Popen.__del__`` to clean up under GC.

    We simulate a Popen whose communicate always raises TimeoutExpired —
    driving the pathological give-up branch — and assert ``poll`` is
    called there (the reap) while a TIMEOUT result is still returned.
    """
    polled: dict[str, int] = {"count": 0}

    class _FakePipe:
        def close(self):
            pass

    class _FakeProc:
        def __init__(self, *a, **k):
            self.pid = 525252
            self.returncode = -9
            self.stdout = _FakePipe()
            self.stderr = _FakePipe()

        def communicate(self, timeout=None):
            raise subprocess.TimeoutExpired(cmd="x", timeout=timeout)

        def kill(self):
            pass

        def poll(self):
            # Non-blocking reap; updates returncode in real Popen.
            polled["count"] += 1
            return self.returncode

    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: _FakeProc())
    # Neutralize process-group / rlimit side effects the fake pid lacks.
    monkeypatch.setattr(tools.os, "killpg", lambda *a, **k: None)
    monkeypatch.setattr(tools.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(tools, "_apply_rlimits_to_pid", lambda pid: None)

    root = _art(tmp_path)
    run_shell = tools.make_run_shell(root)
    # A command that passes the passive profile so we reach the Popen path.
    result = run_shell("ls", profile="passive", cwd=str(root))

    assert "[TIMEOUT" in result
    # Without the fix, poll() is never called on the give-up branch.
    assert polled["count"] >= 1
