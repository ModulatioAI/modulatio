"""Tests for the bubblewrap sandbox layer (SEC-001 + SEC-002).

The sandbox is the trust boundary for ``run_shell``. These tests verify:

1. Argv shape — ``build_sandboxed_argv`` produces a well-formed bwrap
   command line with the expected confinement flags.
2. Env stripping — sensitive vars (API keys, tokens) never cross the
   boundary even when the parent has them set.
3. Per-skill opt-ins — ``needs_network`` flips ``--unshare-net`` off;
   ``pass_env`` adds named vars to the forwarded set.
4. Defense-in-depth — the deny pattern catches ``pass_env`` requests
   for sensitive names.
5. Sandbox availability detection.
6. Behavior when ``bwrap`` is on disk (functional confinement test) —
   xfail/skip on machines without bubblewrap so CI stays portable.

The ``conftest.py`` autouse fixture sets ``MODULATIO_RUN_SHELL_UNSAFE=1``
for the rest of the suite. Tests in this module that need to exercise
the sandbox path explicitly delete the env var locally.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from modulatio import sandbox


# ── Argv shape ───────────────────────────────────────────────────────────


def test_build_sandboxed_argv_basic_shape(tmp_path):
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    argv, env = sandbox.build_sandboxed_argv(
        ["python3", "-c", "print('hi')"], artifacts,
    )
    assert argv[0] == "bwrap"
    assert "--ro-bind" in argv and "/" in argv
    assert "--proc" in argv and "/proc" in argv
    assert "--tmpfs" in argv
    assert "--bind" in argv
    assert str(artifacts.resolve()) in argv
    assert "--unshare-pid" in argv
    assert "--die-with-parent" in argv
    # Network unshared by default
    assert "--unshare-net" in argv
    # The original argv lives after the `--` separator
    sep_idx = argv.index("--")
    assert argv[sep_idx + 1:] == ["python3", "-c", "print('hi')"]


def test_allow_network_omits_unshare_net(tmp_path):
    artifacts = tmp_path / "a"
    artifacts.mkdir()
    argv, _ = sandbox.build_sandboxed_argv(
        ["echo", "hi"], artifacts, allow_network=True,
    )
    assert "--unshare-net" not in argv


def test_extra_binds_added_as_ro(tmp_path):
    artifacts = tmp_path / "a"
    artifacts.mkdir()
    extra = tmp_path / "venv"
    extra.mkdir()
    argv, _ = sandbox.build_sandboxed_argv(
        ["echo"], artifacts, extra_binds=(extra,),
    )
    assert "--ro-bind-try" in argv
    assert str(extra.resolve()) in argv


def test_extra_rw_roots_bound_writable(tmp_path):
    """exec-widen 2c: a granted exec root is rw `--bind`-ed into the sandbox so
    commands can write there (pytest .pyc, build output)."""
    artifacts = tmp_path / "a"
    artifacts.mkdir()
    granted = tmp_path / "proj"
    granted.mkdir()
    argv, _ = sandbox.build_sandboxed_argv(
        ["true"], artifacts, profile="standard", extra_rw_roots=(granted,),
    )
    g = str(granted.resolve())
    # a writable --bind (not --ro-bind) pair for the granted root
    joined = " ".join(argv)
    assert f"--bind {g} {g}" in joined


# ── Env stripping policy ─────────────────────────────────────────────────


def test_env_stripping_drops_api_keys(tmp_path, monkeypatch):
    """Pattern-deny: API keys must never cross the boundary even if
    they're set in the parent."""
    artifacts = tmp_path / "a"
    artifacts.mkdir()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret-1")
    monkeypatch.setenv("OPENAI_API_KEY", "secret-2")
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret-3")
    monkeypatch.setenv("XAI_API_KEY", "secret-4")
    monkeypatch.setenv("BOT_TOKEN", "telegram-secret")
    monkeypatch.setenv("MY_PASSWORD", "secret-5")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret-6")

    _, env = sandbox.build_sandboxed_argv(["echo"], artifacts)
    for k in env:
        assert "API_KEY" not in k
        assert "TOKEN" not in k
        assert "SECRET" not in k.upper()
        assert "PASSWORD" not in k.upper()


def test_env_passes_safe_keys(tmp_path, monkeypatch):
    artifacts = tmp_path / "a"
    artifacts.mkdir()
    monkeypatch.setenv("HOME", "/home/test")
    monkeypatch.setenv("USER", "test")
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    _, env = sandbox.build_sandboxed_argv(["echo"], artifacts)
    assert env["HOME"] == "/home/test"
    assert env["USER"] == "test"
    assert env["LANG"] == "en_US.UTF-8"


def test_env_constrains_path(tmp_path, monkeypatch):
    """PATH should be constrained, not inherited — an attacker who can
    set parent's PATH can't redirect tool resolution."""
    artifacts = tmp_path / "a"
    artifacts.mkdir()
    monkeypatch.setenv("PATH", "/totally/evil/bin:/usr/bin")
    _, env = sandbox.build_sandboxed_argv(["echo"], artifacts)
    assert "/totally/evil/bin" not in env["PATH"]
    assert "/usr/bin" in env["PATH"]


def test_env_blocks_pip_install(tmp_path):
    """PIP_INDEX_URL=file:///dev/null inside the sandbox prevents
    `python -m pip install ...` from reaching the network."""
    artifacts = tmp_path / "a"
    artifacts.mkdir()
    _, env = sandbox.build_sandboxed_argv(["echo"], artifacts)
    assert env["PIP_INDEX_URL"] == "file:///dev/null"
    assert env["PYTHONNOUSERSITE"] == "1"


# ── Per-skill pass_env ──────────────────────────────────────────────────


def test_pass_env_forwards_named_vars(tmp_path, monkeypatch):
    artifacts = tmp_path / "a"
    artifacts.mkdir()
    monkeypatch.setenv("MY_CONFIG_PATH", "/some/path")
    _, env = sandbox.build_sandboxed_argv(
        ["echo"], artifacts, pass_env=("MY_CONFIG_PATH",),
    )
    assert env.get("MY_CONFIG_PATH") == "/some/path"


def test_pass_env_drops_deny_patterns(tmp_path, monkeypatch):
    """Defense in depth: even if a skill's pass_env names a deny-pattern
    var, the sandbox still strips it."""
    artifacts = tmp_path / "a"
    artifacts.mkdir()
    monkeypatch.setenv("HACKER_API_KEY", "should-not-cross")
    _, env = sandbox.build_sandboxed_argv(
        ["echo"], artifacts, pass_env=("HACKER_API_KEY",),
    )
    assert "HACKER_API_KEY" not in env


# ── ContextVar + skill_context manager ──────────────────────────────────


def test_skill_context_sets_and_resets():
    assert sandbox.allow_network_var.get() is False
    assert sandbox.pass_env_var.get() == ()
    with sandbox.skill_context(needs_network=True, pass_env=("FOO",)):
        assert sandbox.allow_network_var.get() is True
        assert sandbox.pass_env_var.get() == ("FOO",)
    assert sandbox.allow_network_var.get() is False
    assert sandbox.pass_env_var.get() == ()


def test_skill_context_resets_on_exception():
    try:
        with sandbox.skill_context(needs_network=True):
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert sandbox.allow_network_var.get() is False


def test_build_sandboxed_argv_picks_up_contextvars(tmp_path):
    artifacts = tmp_path / "a"
    artifacts.mkdir()
    with sandbox.skill_context(needs_network=True, pass_env=()):
        argv, _ = sandbox.build_sandboxed_argv(["echo"], artifacts)
    assert "--unshare-net" not in argv


# ── Availability detection ──────────────────────────────────────────────


def test_is_sandbox_installed_returns_path_check():
    """``is_sandbox_installed`` reports whether bwrap is on PATH —
    independent of whether it can actually create namespaces. Use this
    when distinguishing "absent" from "installed but unusable" (e.g. in
    the doctor output)."""
    expected = shutil.which("bwrap") is not None
    assert sandbox.is_sandbox_installed() is expected


def test_is_sandbox_available_uses_functional_probe(monkeypatch):
    """availability must be a
    functional probe, not just `which bwrap`. When the probe returns
    non-zero (e.g. on hosts where unprivileged user namespaces are
    disabled), `is_sandbox_available` must return False even when
    `bwrap` is on PATH."""
    sandbox.reset_sandbox_probe_cache()
    monkeypatch.setattr(sandbox.shutil, "which", lambda _name: "/usr/bin/bwrap")

    class _FakeResult:
        returncode = 1
        stderr = b"bwrap: No permissions to create new namespace"
        stdout = b""

    def fake_run(*args, **kwargs):
        return _FakeResult()

    monkeypatch.setattr(sandbox.subprocess, "run", fake_run)

    assert sandbox.is_sandbox_installed() is True
    assert sandbox.is_sandbox_available() is False
    sandbox.reset_sandbox_probe_cache()


def test_is_sandbox_available_true_when_probe_succeeds(monkeypatch):
    """Mirror of the above: probe returns rc=0 → sandbox is live."""
    sandbox.reset_sandbox_probe_cache()
    monkeypatch.setattr(sandbox.shutil, "which", lambda _name: "/usr/bin/bwrap")

    class _OkResult:
        returncode = 0
        stderr = b""
        stdout = b""

    monkeypatch.setattr(sandbox.subprocess, "run", lambda *a, **k: _OkResult())

    assert sandbox.is_sandbox_available() is True
    sandbox.reset_sandbox_probe_cache()


def test_is_sandbox_available_false_when_bwrap_absent(monkeypatch):
    """No bwrap on PATH → no probe attempted → not available."""
    sandbox.reset_sandbox_probe_cache()
    monkeypatch.setattr(sandbox.shutil, "which", lambda _name: None)
    assert sandbox.is_sandbox_installed() is False
    assert sandbox.is_sandbox_available() is False
    sandbox.reset_sandbox_probe_cache()


def test_is_sandbox_available_handles_probe_oserror(monkeypatch):
    """If the probe subprocess itself raises (rare — e.g. permission
    denied invoking the binary), treat as unavailable rather than
    crashing the caller. Fail-closed."""
    sandbox.reset_sandbox_probe_cache()
    monkeypatch.setattr(sandbox.shutil, "which", lambda _name: "/usr/bin/bwrap")

    def fake_run_raises(*args, **kwargs):
        raise OSError("permission denied")

    monkeypatch.setattr(sandbox.subprocess, "run", fake_run_raises)
    assert sandbox.is_sandbox_available() is False
    sandbox.reset_sandbox_probe_cache()


def test_is_sandbox_available_caches_probe_result(monkeypatch):
    """The probe forks a subprocess; do that once per process."""
    sandbox.reset_sandbox_probe_cache()
    monkeypatch.setattr(sandbox.shutil, "which", lambda _name: "/usr/bin/bwrap")
    call_count = {"n": 0}

    class _OkResult:
        returncode = 0
        stderr = b""
        stdout = b""

    def counting_run(*args, **kwargs):
        call_count["n"] += 1
        return _OkResult()

    monkeypatch.setattr(sandbox.subprocess, "run", counting_run)

    sandbox.is_sandbox_available()
    sandbox.is_sandbox_available()
    sandbox.is_sandbox_available()
    assert call_count["n"] == 1
    sandbox.reset_sandbox_probe_cache()


def test_is_bypass_requested_default_false(monkeypatch):
    monkeypatch.delenv("MODULATIO_RUN_SHELL_UNSAFE", raising=False)
    assert sandbox.is_bypass_requested() is False


def test_is_bypass_requested_true_when_set(monkeypatch):
    monkeypatch.setenv("MODULATIO_RUN_SHELL_UNSAFE", "1")
    assert sandbox.is_bypass_requested() is True


def test_is_bypass_requested_strict_match(monkeypatch):
    """Empty/other values do NOT enable bypass — only the literal '1'."""
    monkeypatch.setenv("MODULATIO_RUN_SHELL_UNSAFE", "")
    assert sandbox.is_bypass_requested() is False
    monkeypatch.setenv("MODULATIO_RUN_SHELL_UNSAFE", "true")
    assert sandbox.is_bypass_requested() is False


# ── Functional confinement tests (require a working bwrap) ───────────────
#
# These tests exercise the real sandbox end-to-end. We skip on hosts
# where bwrap is absent OR installed-but-unusable (audit Wave 2,
# F2 — the prior `shutil.which("bwrap")` gate let these run on hardened
# distros where every confinement test then failed with "No
# permissions to create new namespace"). Functional check protects
# CI portability without lying about coverage.


def _bwrap_functional() -> bool:
    if shutil.which("bwrap") is None:
        return False
    try:
        result = subprocess.run(
            ["bwrap", "--ro-bind", "/", "/", "true"],
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


bwrap_available = _bwrap_functional()


@pytest.mark.skipif(not bwrap_available, reason="bubblewrap not functional on this host")
def test_sandbox_strips_api_key_from_child(tmp_path, monkeypatch):
    """End-to-end: a child process running inside the real sandbox
    cannot read ANTHROPIC_API_KEY from os.environ."""
    artifacts = tmp_path / "a"
    artifacts.mkdir()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "should-not-leak")
    argv, env = sandbox.build_sandboxed_argv(
        ["python3", "-c", "import os, sys; sys.stdout.write(repr(os.environ.get('ANTHROPIC_API_KEY')))"],
        artifacts,
    )
    proc = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=10.0)
    assert proc.returncode == 0, f"stderr: {proc.stderr}"
    # The child sees None for ANTHROPIC_API_KEY because it was stripped
    assert proc.stdout == "None", f"got {proc.stdout!r}"


@pytest.mark.skipif(not bwrap_available, reason="bubblewrap not functional on this host")
def test_sandbox_blocks_writes_outside_artifacts_root(tmp_path):
    """End-to-end: a child cannot write outside the artifacts root.
    The host filesystem is bound read-only; only artifacts_root is rw."""
    artifacts = tmp_path / "a"
    artifacts.mkdir()
    # Try to write to /etc which is bound read-only; expect failure.
    argv, env = sandbox.build_sandboxed_argv(
        ["python3", "-c", "open('/etc/evil', 'w').write('pwn')"],
        artifacts,
    )
    proc = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=10.0)
    assert proc.returncode != 0
    assert "Permission denied" in proc.stderr or "Read-only" in proc.stderr


@pytest.mark.skipif(not bwrap_available, reason="bubblewrap not functional on this host")
def test_sandbox_blocks_network_by_default(tmp_path):
    """End-to-end: --unshare-net means the child can't reach DNS or
    open external sockets. We probe by trying to resolve a hostname."""
    artifacts = tmp_path / "a"
    artifacts.mkdir()
    argv, env = sandbox.build_sandboxed_argv(
        ["python3", "-c",
         "import socket; "
         "socket.create_connection(('1.1.1.1', 80), timeout=2)"],
        artifacts,
    )
    proc = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=10.0)
    # Either a network unreachable error or a timeout is acceptable —
    # both indicate the namespace is unshared. What we want is NOT a
    # successful connection (returncode == 0).
    assert proc.returncode != 0


@pytest.mark.skipif(not bwrap_available, reason="bubblewrap not functional on this host")
def test_sandbox_allows_writes_inside_artifacts_root(tmp_path):
    """End-to-end: artifacts root is writable inside the sandbox."""
    artifacts = tmp_path / "a"
    artifacts.mkdir()
    argv, env = sandbox.build_sandboxed_argv(
        ["python3", "-c",
         f"open({str(artifacts / 'ok.txt')!r}, 'w').write('hello')"],
        artifacts,
    )
    proc = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=10.0)
    assert proc.returncode == 0, f"stderr: {proc.stderr}"
    assert (artifacts / "ok.txt").read_text() == "hello"


# ── #82: interpreter/venv bind so code actually execs ────────────────────


def test_interpreter_binds_include_sys_prefix():
    """The active venv root (sys.prefix) must be in the bind set so the
    --tmpfs /home mask doesn't hide the interpreter that run_shell rewrites
    python3/pytest to."""
    binds = {str(p) for p in sandbox._interpreter_binds()}
    assert str(Path(sys.prefix).resolve()) in binds


def test_build_sandboxed_argv_binds_the_venv(tmp_path):
    """The wrapped argv ro-binds the venv prefix back in AFTER --tmpfs /home."""
    artifacts = tmp_path / "a"
    artifacts.mkdir()
    argv, _ = sandbox.build_sandboxed_argv(["echo"], artifacts)
    prefix = str(Path(sys.prefix).resolve())
    # appears as a --ro-bind-try pair
    assert prefix in argv
    i = argv.index(prefix)
    assert argv[i - 1] == "--ro-bind-try"
    # and it comes AFTER the /home tmpfs mask so it isn't clobbered
    assert "--tmpfs" in argv and "/home" in argv
    tmpfs_home = max(
        j for j, a in enumerate(argv[:-1]) if a == "--tmpfs" and argv[j + 1] == "/home"
    )
    assert i > tmpfs_home


@pytest.mark.skipif(not bwrap_available, reason="bubblewrap not functional on this host")
def test_sandbox_execs_venv_interpreter(tmp_path):
    """Direct #82 regression: the venv interpreter (sys.executable, which
    lives under the masked /home for a project venv) execs inside the
    sandbox instead of dying with `bwrap: execvp ... .venv ...`. run_shell
    rewrites python3 -> sys.executable, so this is the real path."""
    artifacts = tmp_path / "a"
    artifacts.mkdir()
    argv, env = sandbox.build_sandboxed_argv(
        [sys.executable, "-c", "print('VENV_EXEC_OK')"],
        artifacts,
    )
    proc = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=15.0)
    assert proc.returncode == 0, f"stderr: {proc.stderr}"
    assert "VENV_EXEC_OK" in proc.stdout


# ── Sandbox profile knob (MODULATIO_SANDBOX_PROFILE) ─────────────────────


def test_profile_defaults_to_standard(monkeypatch):
    monkeypatch.delenv("MODULATIO_SANDBOX_PROFILE", raising=False)
    assert sandbox.current_profile() == "standard"


def test_profile_is_case_insensitive(monkeypatch):
    monkeypatch.setenv("MODULATIO_SANDBOX_PROFILE", "TRUSTED")
    assert sandbox.current_profile() == "trusted"


def test_profile_typo_fails_safe_to_standard(monkeypatch):
    """A typo must NEVER silently widen the sandbox."""
    monkeypatch.setenv("MODULATIO_SANDBOX_PROFILE", "trustted")
    assert sandbox.current_profile() == "standard"


def test_trusted_forces_network_on_and_keeps_secret_floor(tmp_path, monkeypatch):
    """trusted = don't hamper (network on, pip functional) BUT the secret
    deny-list still strips cloud keys."""
    artifacts = tmp_path / "a"
    artifacts.mkdir()
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-leak")
    argv, env = sandbox.build_sandboxed_argv(
        ["echo"], artifacts, profile="trusted", allow_network=False,
    )
    # network forced on despite allow_network=False
    assert "--unshare-net" not in argv
    # pip not poisoned in trusted
    assert env.get("PIP_INDEX_URL") is None
    # secret floor still holds
    assert "OPENAI_API_KEY" not in env


def test_standard_keeps_network_off_and_pip_blocked(tmp_path):
    artifacts = tmp_path / "a"
    artifacts.mkdir()
    argv, env = sandbox.build_sandboxed_argv(
        ["echo"], artifacts, profile="standard",
    )
    assert "--unshare-net" in argv
    assert env["PIP_INDEX_URL"] == "file:///dev/null"
