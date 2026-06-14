"""Round-4 (0.9.0 pre-ship) re-sweep regression tests for ``modulatio.tools``.

Separate, additive file (do NOT collide with ``test_tools_resweep.py`` /
``test_tools_resweep_r3.py`` from prior rounds). Covers one confirmed
re-sweep finding:

  1. [MEDIUM/integration] The H3a ``prlimit`` wrapper prefix masks the
     friendly ``[INFO] tool 'X' not installed`` body for standalone binaries.
     Once the payload argv is wrapped as ``prlimit -- <payload>``, ``Popen``
     execs ``prlimit`` (which exists), so a missing payload (ruby/go/node/...)
     no longer raises ``FileNotFoundError`` — prlimit runs and exits 127, and
     the documented friendly body never fires. Fix: pre-check the payload
     binary against the host PATH BEFORE wrapping, returning the ``[INFO]``
     body (exit_code -1) directly when it isn't installed.
"""

from __future__ import annotations

from pathlib import Path

from modulatio import tools


def _art(tmp_path: Path) -> Path:
    art = tmp_path / "artifacts"
    art.mkdir()
    return art


def test_missing_standalone_payload_returns_info_body_even_with_prlimit(
    tmp_path, monkeypatch
):
    """With the prlimit wrapper ACTIVE (production path) and the payload
    binary missing, run_shell must still return the friendly [INFO] body with
    exit_code -1 — not a prlimit exit 127, and not a FileNotFoundError.

    This is the regression: pre-fix, ``_payload_argv = [prlimit, ..., '--',
    'ruby', ...]`` so Popen execs prlimit (present), prlimit fails to exec
    'ruby' and exits 127, and the FileNotFoundError handler never fires.
    """
    art = _art(tmp_path)
    rs = tools.make_run_shell(art)

    # Force the prlimit prefix ON, as on a real Linux production host, so the
    # FileNotFoundError handler would be dead for a standalone binary.
    monkeypatch.setattr(
        tools, "_prlimit_wrapper_prefix",
        lambda: ["/usr/bin/prlimit", "--as=1", "--core=0", "--"],
    )

    # Force the payload binary 'ruby' to resolve as NOT installed, regardless
    # of whether the test host actually has it. (ruby --version passes the
    # passive allowlist as an interpreter-version probe.) The resolver imports
    # shutil locally, so patch the real module attribute.
    import shutil as _shutil

    real_which = _shutil.which

    def fake_which(name, *a, **k):
        if name == "ruby":
            return None
        return real_which(name, *a, **k)

    monkeypatch.setattr(_shutil, "which", fake_which)

    out = rs(cmd="ruby --version", profile="passive", timeout=5)

    assert "[INFO]" in out
    assert "ruby" in out
    assert "not installed" in out
    assert "exit_code: -1" in out
    # The masked prlimit-127 path must NOT be what surfaces.
    assert "exit_code: 127" not in out


def test_present_payload_still_runs_and_is_not_masked(tmp_path, monkeypatch):
    """A payload that IS installed must NOT be short-circuited by the
    not-installed pre-check — it must actually run."""
    art = _art(tmp_path)
    # Write a trivial script the python interpreter executes.
    (art / "ok.py").write_text("print('hello-present')\n")
    rs = tools.make_run_shell(art)

    out = rs(cmd="python3 ok.py", profile="full", timeout=10)

    # python3 is rewritten to sys.executable (an existing absolute exe), so the
    # pre-check passes and the script runs.
    assert "hello-present" in out
    assert "[INFO]" not in out


def test_resolve_payload_binary_handles_bare_and_path_forms(tmp_path):
    """Unit cover for the resolver: bare missing name -> None; existing
    executable path -> itself; non-existent path -> None."""
    # A bare name that won't exist on any PATH.
    assert tools._resolve_payload_binary("totally_not_a_real_binary_xyz") is None
    # An absolute path to a real executable (the running interpreter).
    import sys

    assert tools._resolve_payload_binary(sys.executable) == sys.executable
    # A path form pointing at a non-existent file.
    missing = str(tmp_path / "nope" / "ghost")
    assert tools._resolve_payload_binary(missing) is None
