"""Round-3 (0.9.0 pre-ship) re-sweep regression tests for ``modulatio.tools``.

Separate, additive file (do NOT collide with ``test_tools_resweep.py`` from a
prior round). Covers one confirmed re-sweep finding:

  1. [MEDIUM/security] H3a memory/disk/core rlimit cap did not reach the
     payload on the SANDBOXED (production) path. ``_apply_rlimits_to_pid``
     clamps the PID ``Popen`` returns, which under bwrap is the MONITOR
     process; bwrap forks the real payload into its own PID namespace AFTER,
     so the per-process limits set on the monitor never inherit to the
     payload. Fix: prefix the payload argv with ``prlimit --as --fsize
     --core -- <argv>`` so the caps are set on the payload's own PID at exec
     time, INSIDE the sandbox (before bwrap, so the prefix lands after `--`).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from modulatio import tools
from modulatio import sandbox as _sandbox


def _art(tmp_path: Path) -> Path:
    art = tmp_path / "artifacts"
    art.mkdir()
    return art


class _CapturePipe:
    def close(self):
        pass


class _CaptureProc:
    """A fake Popen that records the argv it was launched with and returns
    a clean exit so run_shell takes the happy path."""

    last_argv: list[str] | None = None

    def __init__(self, argv, *a, **k):
        type(self).last_argv = list(argv)
        self.pid = 424242
        self.returncode = 0
        self.stdout = _CapturePipe()
        self.stderr = _CapturePipe()

    def communicate(self, timeout=None):
        return ("", "")

    def kill(self):
        pass

    def poll(self):
        return self.returncode


def _patch_common(monkeypatch):
    monkeypatch.setattr(
        subprocess, "Popen", lambda argv, *a, **k: _CaptureProc(argv, *a, **k)
    )
    # The fake pid isn't a real child; neuter the parent-side prlimit clamp.
    monkeypatch.setattr(tools, "_apply_rlimits_to_pid", lambda pid: None)
    monkeypatch.setattr(tools.os, "killpg", lambda *a, **k: None)
    monkeypatch.setattr(tools.os, "getpgid", lambda pid: pid)


def _force_prlimit_available(monkeypatch):
    """Pin the prlimit prefix to a stable, present-looking value so the test
    is deterministic on a host without util-linux."""
    prefix = [
        "/usr/bin/prlimit",
        f"--as={tools._RUN_SHELL_RLIMIT_AS_BYTES}",
        f"--fsize={tools._RUN_SHELL_RLIMIT_FSIZE_BYTES}",
        "--core=0",
        "--",
    ]
    monkeypatch.setattr(tools, "_prlimit_wrapper_prefix", lambda: list(prefix))
    return prefix


# ── prefix helper ──────────────────────────────────────────────────────────

def test_prlimit_prefix_present_when_binary_available(monkeypatch):
    import shutil

    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/prlimit")
    prefix = tools._prlimit_wrapper_prefix()
    assert prefix[0] == "/usr/bin/prlimit"
    assert f"--as={tools._RUN_SHELL_RLIMIT_AS_BYTES}" in prefix
    assert f"--fsize={tools._RUN_SHELL_RLIMIT_FSIZE_BYTES}" in prefix
    assert "--core=0" in prefix
    assert prefix[-1] == "--"


def test_prlimit_prefix_empty_when_binary_missing(monkeypatch):
    import shutil

    monkeypatch.setattr(shutil, "which", lambda name: None)
    assert tools._prlimit_wrapper_prefix() == []


# ── Finding 1: caps reach the payload on the SANDBOXED path ─────────────────

def test_sandboxed_payload_is_prlimit_wrapped_inside_bwrap(tmp_path, monkeypatch):
    """On the sandboxed (production) path the prlimit prefix must land INSIDE
    the bwrap argv — i.e. ``build_sandboxed_argv`` receives the prlimit-wrapped
    payload, so the caps are set on the real payload PID in its own PID
    namespace, not on the bwrap monitor.

    Without the fix the payload handed to bwrap is the bare command and the
    only rlimit application is the parent-side clamp on the monitor PID — which
    never reaches the payload."""
    _patch_common(monkeypatch)
    prefix = _force_prlimit_available(monkeypatch)

    monkeypatch.setattr(_sandbox, "current_profile", lambda: "passive")
    monkeypatch.setattr(_sandbox, "is_bypass_requested", lambda: False)
    monkeypatch.setattr(_sandbox, "is_sandbox_available", lambda: True)

    captured: dict[str, list[str]] = {}

    def _fake_build(payload_argv, artifacts_root, *, profile=None, **kwargs):
        # **kwargs absorbs build_sandboxed_argv's optional binds (extra_binds,
        # extra_rw_roots [exec-widen], allow_network, pass_env) so this stub
        # tracks the real signature without re-listing each.
        captured["payload"] = list(payload_argv)
        return (["bwrap", "--die-with-parent", "--", *payload_argv], {})

    monkeypatch.setattr(_sandbox, "build_sandboxed_argv", _fake_build)

    root = _art(tmp_path)
    run_shell = tools.make_run_shell(root)
    run_shell("ls", profile="passive", cwd=str(root))

    # build_sandboxed_argv received the prlimit-wrapped payload.
    assert captured["payload"][: len(prefix)] == prefix
    assert captured["payload"][len(prefix):] == ["ls"]

    # And the actual launched argv has prlimit AFTER bwrap's `--` (inside the
    # sandbox), not as the outer process.
    launched = _CaptureProc.last_argv
    assert launched is not None
    assert launched[0] == "bwrap"
    dd = launched.index("--")
    assert launched[dd + 1 :][: len(prefix)] == prefix


def test_unsandboxed_payload_is_prlimit_wrapped(tmp_path, monkeypatch):
    """On the bypass/unsandboxed path the launched argv must still be
    prlimit-wrapped so the caps reach the (direct-child) payload."""
    _patch_common(monkeypatch)
    prefix = _force_prlimit_available(monkeypatch)

    monkeypatch.setattr(_sandbox, "current_profile", lambda: "passive")
    monkeypatch.setattr(_sandbox, "is_bypass_requested", lambda: True)

    root = _art(tmp_path)
    run_shell = tools.make_run_shell(root)
    run_shell("ls", profile="passive", cwd=str(root))

    launched = _CaptureProc.last_argv
    assert launched is not None
    assert launched[: len(prefix)] == prefix
    assert launched[len(prefix):] == ["ls"]
