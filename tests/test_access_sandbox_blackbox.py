# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Linux black-box substrate tier: drive the REAL sandbox through its actual
env/PATH knobs and observe the real run_shell behavior for each substrate
state — no monkeypatched availability. A fresh ``bwrap`` on PATH gives the
sandboxed-full state; a stub ``bwrap`` that exits non-zero makes the policy
probe fail, yielding degraded (soft-fallback) or refused (under REQUIRE);
the unsafe bypass gives the off state. Each cell runs a genuine child and
asserts execution, confinement, or refusal.

Separate, non-default file (mounts the real sandbox and spawns children):
run it in the Linux gate. Skips only where the platform genuinely cannot
host the tier (non-Linux, or bwrap not installed) — never to dodge a
result on a capable host.
"""
from __future__ import annotations

import os
import stat
import sys

import pytest

from modulatio import sandbox
from modulatio import tools

pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or not sandbox.is_sandbox_installed(),
    reason="Linux + installed bwrap required for the black-box substrate tier",
)


@pytest.fixture
def art(tmp_path):
    a = tmp_path / "artifacts"
    a.mkdir()
    return a


@pytest.fixture(autouse=True)
def _clean_substrate_env(monkeypatch):
    """Start each cell from a known substrate: standard profile, no bypass,
    not required, caches cleared. Each cell then drives its own state."""
    for var in ("MODULATIO_RUN_SHELL_UNSAFE", "MODULATIO_REQUIRE_SANDBOX"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("MODULATIO_SANDBOX_PROFILE", "standard")
    sandbox.reset_enforcement_state_cache()
    sandbox.reset_sandbox_probe_cache()
    yield
    sandbox.reset_enforcement_state_cache()
    sandbox.reset_sandbox_probe_cache()


def _break_bwrap(monkeypatch, tmp_path):
    """Prepend a stub ``bwrap`` that exits non-zero, so the real policy probe
    runs it and fails — the host looks bwrap-installed-but-unusable."""
    stub_dir = tmp_path / "brokenbin"
    stub_dir.mkdir()
    stub = stub_dir / "bwrap"
    stub.write_text("#!/bin/sh\nexit 1\n")
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", f"{stub_dir}:{os.environ['PATH']}")
    sandbox.reset_enforcement_state_cache()
    sandbox.reset_sandbox_probe_cache()


def test_state_sandboxed_full_runs_confined(art, monkeypatch):
    """Real bwrap present + standard profile → SANDBOXED_FULL, a child runs,
    and the parent env is stripped (a secret set in the parent is invisible
    to the confined child — a real confinement signal, not a lambda)."""
    assert sandbox.enforcement_state() == sandbox.EnforcementState.SANDBOXED_FULL
    monkeypatch.setenv("MODULATIO_LEAK_CANARY", "sekret")
    rs = tools.make_run_shell(art)
    out = rs(cmd="python3 -c \"import os; print(os.environ.get("
                 "'MODULATIO_LEAK_CANARY','ABSENT'))\"",
             profile="full", timeout=30.0)
    assert out.startswith("exit_code: 0")
    assert "ABSENT" in out           # env stripped → really confined
    assert "sekret" not in out


def test_state_off_bypass_runs_unsandboxed(art, monkeypatch):
    """The unsafe bypass (off state) runs the child with the parent env —
    the canary IS visible, proving the sandbox was really off."""
    monkeypatch.setenv("MODULATIO_RUN_SHELL_UNSAFE", "1")
    monkeypatch.setenv("MODULATIO_LEAK_CANARY", "sekret")
    sandbox.reset_enforcement_state_cache()
    rs = tools.make_run_shell(art)
    out = rs(cmd="python3 -c \"import os; print(os.environ.get("
                 "'MODULATIO_LEAK_CANARY','ABSENT'))\"",
             profile="full", timeout=30.0)
    assert out.startswith("exit_code: 0")
    assert "sekret" in out           # parent env passed → really unsandboxed


def test_state_degraded_soft_falls_and_runs(art, monkeypatch, tmp_path):
    """Broken bwrap, NOT required, no bypass → DEGRADED_ALLOWLIST: run_shell
    soft-falls to an unsandboxed child rather than refusing."""
    _break_bwrap(monkeypatch, tmp_path)
    assert (sandbox.enforcement_state()
            == sandbox.EnforcementState.DEGRADED_ALLOWLIST)
    rs = tools.make_run_shell(art)
    out = rs(cmd="python3 -c \"print('degraded-ran')\"",
             profile="full", timeout=30.0)
    assert out.startswith("exit_code: 0")
    assert "degraded-ran" in out


def test_state_refused_denies_run(art, monkeypatch, tmp_path):
    """Broken bwrap + REQUIRE_SANDBOX + no bypass → REFUSED: run_shell refuses
    to start a child rather than run it unconfined."""
    _break_bwrap(monkeypatch, tmp_path)
    monkeypatch.setenv("MODULATIO_REQUIRE_SANDBOX", "1")
    sandbox.reset_enforcement_state_cache()
    assert sandbox.enforcement_state() == sandbox.EnforcementState.REFUSED
    rs = tools.make_run_shell(art)
    with pytest.raises(RuntimeError, match="refused"):
        rs(cmd="python3 -c \"print('must-not-run')\"",
           profile="full", timeout=30.0)


def test_black_box_states_cover_every_substrate_descriptor():
    """The tier exercises every SUBSTRATE_STATES descriptor value (the three
    enforcement states plus the off bypass) as a real state, not a lambda."""
    from modulatio import access_surface as axs
    covered = {"sandboxed_full", "degraded_allowlist", "refused", "off"}
    assert covered == set(axs.SUBSTRATE_STATES)
