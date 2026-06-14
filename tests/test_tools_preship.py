"""Pre-ship (0.9.0) regression tests for ``modulatio.tools``.

Uniquely named to avoid colliding with concurrent agents editing the
primary ``test_tools.py`` / ``test_tools_low_audit.py``.

Covers three confirmed pre-ship findings:

  1. [MEDIUM/security] ``go vet -vettool=BIN`` / ``-flag=PATH`` bypass the
     passive allowlist (arbitrary-binary exec + unconfined path arg in a
     no-execution profile).
  2. [MEDIUM/security] Single-dash flags slipped through ``_is_safe_file_arg``
     as "files" for ls/cat/head/tail/wc.
  3. [LOW/resource-leak] Popen pipes leaked fds on the pathological
     triple-timeout drain path.
"""

from __future__ import annotations

from pathlib import Path

from modulatio import tools


def _art(tmp_path: Path) -> Path:
    art = tmp_path / "artifacts"
    art.mkdir()
    return art


# ── Finding 1: go vet flag bypass ──────────────────────────────────────

def test_go_vet_rejects_vettool_binary_exec(tmp_path):
    """``-vettool=<binary>`` makes go vet EXECUTE an arbitrary binary —
    a direct violation of the no-execution passive contract. Refused.

    Fails before the fix (old code returned True for any ``-``-leading arg).
    """
    root = _art(tmp_path)
    assert not tools._check_passive(
        ["go", "vet", "-vettool=/usr/bin/id", "./..."], root
    )
    assert not tools._check_passive(["go", "vet", "-vettool=/bin/sh"], root)
    assert not tools._check_passive(["go", "vet", "--vettool=/bin/sh"], root)
    # bare -vettool (value as the next token) is still not a value-less flag
    assert not tools._check_passive(["go", "vet", "-vettool"], root)


def test_go_vet_confines_flag_value_paths(tmp_path):
    """``-flag=value`` whose value is a traversal/absolute path is refused;
    a benign value-flag and a value-less flag stay passive."""
    root = _art(tmp_path)
    # path payload in a flag value must not escape the root
    assert not tools._check_passive(["go", "vet", "-tags=../../etc", "./..."], root)
    assert not tools._check_passive(
        ["go", "vet", "-mod=/etc/passwd", "./..."], root
    )
    # benign relative flag value is fine
    assert tools._check_passive(["go", "vet", "-tags=integration", "./..."], root)
    # value-less flag carries no path — still passive
    assert tools._check_passive(["go", "vet", "-json", "./..."], root)


# ── Finding 2: single-dash flags as "files" ────────────────────────────

def test_is_safe_file_arg_rejects_leading_dash(tmp_path):
    """A dash-leading token is a flag, never a file."""
    root = _art(tmp_path)
    assert not tools._is_safe_file_arg("-R", root)
    assert not tools._is_safe_file_arg("-A", root)
    assert not tools._is_safe_file_arg("--color=always", root)
    # a real relative file still passes
    assert tools._is_safe_file_arg("notes.txt", root)
    assert tools._is_safe_file_arg("sub/notes.txt", root)


def test_passive_heads_reject_dash_flags(tmp_path):
    """ls/cat/head/tail must not admit arbitrary flags via the file-arg path.

    Fails before the fix (``-R``/``--color`` were accepted as filenames).
    """
    root = _art(tmp_path)
    assert not tools._check_passive(["ls", "-R"], root)
    assert not tools._check_passive(["ls", "--color=always"], root)
    assert not tools._check_passive(["cat", "-A", "notes.txt"], root)
    assert not tools._check_passive(["head", "--bytes=99999"], root)
    assert not tools._check_passive(["tail", "-f", "notes.txt"], root)


def test_passive_heads_keep_legitimate_forms(tmp_path):
    """The hardening must not break the real read forms."""
    root = _art(tmp_path)
    assert tools._check_passive(["ls"], root)
    assert tools._check_passive(["ls", "-la", "notes.txt"], root)
    assert tools._check_passive(["cat", "notes.txt"], root)
    assert tools._check_passive(["head", "-n", "5", "notes.txt"], root)
    assert tools._check_passive(["head", "-5", "notes.txt"], root)
    assert tools._check_passive(["tail", "-n", "5", "notes.txt"], root)
    assert tools._check_passive(["wc", "-l", "notes.txt"], root)


# ── Finding 3: fd leak on the triple-timeout drain path ────────────────

def test_triple_timeout_drain_closes_pipes(tmp_path, monkeypatch):
    """On the pathological path where every drain ``communicate`` times out,
    run_shell must close the Popen pipe ends rather than leak the fds.

    We simulate a Popen whose communicate always raises TimeoutExpired and
    assert both pipe ends end up closed and a TIMEOUT result is returned.
    """
    import subprocess

    closed: dict[str, bool] = {"stdout": False, "stderr": False}

    class _FakePipe:
        def __init__(self, name: str):
            self._name = name

        def close(self):
            closed[self._name] = True

    class _FakeProc:
        def __init__(self, *a, **k):
            self.pid = 424242
            self.returncode = -9
            self.stdout = _FakePipe("stdout")
            self.stderr = _FakePipe("stderr")

        def communicate(self, timeout=None):
            raise subprocess.TimeoutExpired(cmd="x", timeout=timeout)

        def kill(self):
            pass

    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: _FakeProc())
    # Neutralize process-group / rlimit side effects that the fake pid lacks.
    monkeypatch.setattr(tools.os, "killpg", lambda *a, **k: None)
    monkeypatch.setattr(tools.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(tools, "_apply_rlimits_to_pid", lambda pid: None)

    root = _art(tmp_path)
    run_shell = tools.make_run_shell(root)
    # A command that passes the passive profile so we reach the Popen path.
    result = run_shell("ls", profile="passive", cwd=str(root))

    assert "[TIMEOUT" in result
    assert closed["stdout"] is True
    assert closed["stderr"] is True
