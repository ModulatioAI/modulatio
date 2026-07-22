# SPDX-License-Identifier: Apache-2.0
"""The typed sandbox enforcement state.

One engine-owned function computes SANDBOXED_FULL / DEGRADED_ALLOWLIST /
REFUSED; disclosure and dispatch both consume it — nothing keys on
``is_sandbox_available()`` alone. The probe exercises the ACTUAL policy
shape (empty root + unshare flags), never ``--ro-bind / /``. The cache
carries a TTL and re-probes after any bwrap exec failure; ``profile=off``
and the unsafe bypass NEVER upgrade to full — and an explicit bypass wins
over REQUIRE (the operator accepted that risk knowingly).
"""
from __future__ import annotations

import subprocess

import pytest

from modulatio import sandbox


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    sandbox.reset_enforcement_state_cache()
    monkeypatch.delenv("MODULATIO_REQUIRE_SANDBOX", raising=False)
    monkeypatch.delenv("MODULATIO_RUN_SHELL_UNSAFE", raising=False)
    monkeypatch.delenv("MODULATIO_SANDBOX_PROFILE", raising=False)
    yield
    sandbox.reset_enforcement_state_cache()


def _probe(monkeypatch, ok: bool):
    monkeypatch.setattr(sandbox, "_probe_policy_shape", lambda: ok)


def test_full_when_probe_ok_and_no_unsafe_posture(monkeypatch):
    _probe(monkeypatch, True)
    assert sandbox.enforcement_state() is sandbox.EnforcementState.SANDBOXED_FULL


def test_degraded_when_probe_fails_without_require(monkeypatch):
    _probe(monkeypatch, False)
    assert sandbox.enforcement_state() is sandbox.EnforcementState.DEGRADED_ALLOWLIST


def test_refused_when_probe_fails_under_require(monkeypatch):
    _probe(monkeypatch, False)
    monkeypatch.setenv("MODULATIO_REQUIRE_SANDBOX", "1")
    assert sandbox.enforcement_state() is sandbox.EnforcementState.REFUSED


def test_off_profile_and_bypass_never_upgrade_to_full(monkeypatch):
    _probe(monkeypatch, True)
    monkeypatch.setenv("MODULATIO_SANDBOX_PROFILE", "off")
    assert sandbox.enforcement_state() is sandbox.EnforcementState.DEGRADED_ALLOWLIST
    sandbox.reset_enforcement_state_cache()
    monkeypatch.delenv("MODULATIO_SANDBOX_PROFILE")
    monkeypatch.setenv("MODULATIO_RUN_SHELL_UNSAFE", "1")
    assert sandbox.enforcement_state() is sandbox.EnforcementState.DEGRADED_ALLOWLIST


def test_explicit_bypass_wins_over_require(monkeypatch):
    # The operator accepted the risk knowingly — REQUIRE governs only the
    # IMPLICIT missing-bwrap path (the documented is_sandbox_required rule).
    _probe(monkeypatch, False)
    monkeypatch.setenv("MODULATIO_REQUIRE_SANDBOX", "1")
    monkeypatch.setenv("MODULATIO_RUN_SHELL_UNSAFE", "1")
    assert sandbox.enforcement_state() is sandbox.EnforcementState.DEGRADED_ALLOWLIST


def test_state_is_cached_and_exec_failure_forces_reprobe(monkeypatch):
    calls = []

    def probe():
        calls.append(1)
        return True

    monkeypatch.setattr(sandbox, "_probe_policy_shape", probe)
    assert sandbox.enforcement_state() is sandbox.EnforcementState.SANDBOXED_FULL
    assert sandbox.enforcement_state() is sandbox.EnforcementState.SANDBOXED_FULL
    assert len(calls) == 1                      # cached
    # A live bwrap exec failure invalidates the cache — the very next state
    # read re-probes (a wrapper failure never silently rides a stale FULL).
    sandbox.note_bwrap_exec_failure()
    sandbox.enforcement_state()
    assert len(calls) == 2


def test_policy_probe_exercises_the_empty_root_shape(monkeypatch):
    """The probe must prove the flags the REAL mount uses — empty root +
    namespace unshares — never `--ro-bind / /` (a host can pass that probe
    and still refuse the policy shape)."""
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"] = argv

        class R:
            returncode = 0

        return R()

    monkeypatch.setattr(sandbox, "is_sandbox_installed", lambda: True)
    monkeypatch.setattr(subprocess, "run", fake_run)
    assert sandbox._probe_policy_shape() is True
    argv = seen["argv"]
    assert argv[0] == "bwrap"
    joined = " ".join(argv)
    assert "--tmpfs /" in joined                 # empty root, not the host's
    assert "--unshare-pid" in joined
    assert "--unshare-net" in joined
    assert "--ro-bind / /" not in joined


# ── the empty-root mount ────────────────────────────────────────


def _argv(tmp_path, **kw):
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir(exist_ok=True)
    argv, env = sandbox.build_sandboxed_argv(["true"], artifacts, **kw)
    return argv, " ".join(argv)


def test_mount_starts_from_an_empty_root_not_the_host(tmp_path):
    argv, joined = _argv(tmp_path)
    assert "--ro-bind / /" not in joined       # the empty-root prerequisite
    assert "--tmpfs /" in joined               # empty root
    assert "--ro-bind /usr /usr" in joined     # runtime, read-only
    assert "--cap-drop ALL" in joined          # child hygiene, explicit


def test_run_and_var_are_empty_inside(tmp_path):
    # No host pathname socket is visible by default — /run and /var exist
    # (tools expect them) but are EMPTY tmpfs, never the host's.
    argv, joined = _argv(tmp_path)
    assert "--tmpfs /run" in joined
    assert "--tmpfs /var" in joined


def test_network_etc_files_bind_only_with_network(tmp_path):
    off_argv, off = _argv(tmp_path, allow_network=False)
    on_argv, on = _argv(tmp_path, allow_network=True)
    for f in ("/etc/resolv.conf", "/etc/ssl"):
        assert f not in off                    # no egress config without net
        assert f in on
    # the loader's files bind regardless — execution needs them, net or not
    assert "/etc/ld.so.cache" in off


@pytest.mark.skipif(not sandbox.is_sandbox_available(),
                    reason="bwrap required for live mount pins")
def test_negative_visibility_of_host_paths(tmp_path):
    """The visibility pin battery: host /etc (beyond the runtime allowlist), /opt,
    /mnt, /media, /srv, host /var and /run content are INVISIBLE."""
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    probe = (
        "import os\n"
        "print('etc:', sorted(os.listdir('/etc')))\n"
        "for p in ('/opt', '/mnt', '/media', '/srv'):\n"
        "    print(p, os.path.exists(p))\n"
        "print('run:', os.listdir('/run'))\n"
        "print('var:', os.listdir('/var'))\n"
    )
    argv, env = sandbox.build_sandboxed_argv(
        ["/usr/bin/python3", "-c", probe], artifacts)
    out = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    text = out.stdout
    # /etc carries ONLY the runtime allowlist — the host's sshd/passwd-shadow
    # world is gone (hostname is a canary any real /etc has).
    assert "hostname" not in text.split("etc:")[1].splitlines()[0]
    assert "shadow" not in text
    for p in ("/opt", "/mnt", "/media", "/srv"):
        assert f"{p} False" in text
    assert "run: []" in text
    assert "var: []" in text


@pytest.mark.skipif(not sandbox.is_sandbox_available(),
                    reason="bwrap required for live mount pins")
def test_python_and_artifacts_still_work_inside(tmp_path):
    """The #82 regression class: after any mount change, prove real
    execution still works — python runs, the artifacts root is writable."""
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    target = artifacts / "out.txt"
    argv, env = sandbox.build_sandboxed_argv(
        ["/usr/bin/python3", "-c",
         f"open({str(target)!r}, 'w').write('alive')"],
        artifacts)
    out = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert target.read_text() == "alive"


@pytest.mark.skipif(not sandbox.is_sandbox_available(),
                    reason="bwrap required for live mount pins")
def test_planted_host_secret_is_invisible_to_executed_code(tmp_path):
    """The executed-code secret floor: a credential OUTSIDE the binds cannot
    be read by code inside — the mount, not the tool allowlist, stops it."""
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    secret = tmp_path / "cred.pem"
    secret.write_text("PRIVATE")
    argv, env = sandbox.build_sandboxed_argv(
        ["/usr/bin/python3", "-c",
         f"import os; print(os.path.exists({str(secret)!r}))"],
        artifacts)
    out = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "False"
