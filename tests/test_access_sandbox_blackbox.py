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


def _bwrap_functional() -> "tuple[bool, str]":
    """Binary-present is NOT sandbox-functional: probe an actual confined
    child (namespaces + die-with-parent) the way the policy does. Returns
    (functional, evidence) so a required cell can FAIL with the real
    prerequisite named — never skip-green on a host whose bwrap exists but
    cannot confine (no user namespaces, nested-sandbox hosts)."""
    import subprocess
    try:
        probe = subprocess.run(
            ["bwrap", "--unshare-all", "--die-with-parent",
             "--ro-bind", "/", "/", "true"],
            capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"bwrap probe could not run: {exc}"
    if probe.returncode != 0:
        return False, (
            f"bwrap present but cannot confine (rc {probe.returncode}): "
            f"{probe.stderr.strip()[:200]}")
    return True, "bwrap confines (namespace probe succeeded)"


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
    to the confined child — a real confinement signal, not a lambda).

    REQUIRED on the designated Linux gate: a host whose bwrap binary exists
    but cannot confine FAILS here with the prerequisite named — the cell
    never skips green."""
    functional, evidence = _bwrap_functional()
    assert functional, (
        f"designated-gate prerequisite unmet — {evidence}; the "
        f"SANDBOXED_FULL observation requires a host with functional "
        f"bubblewrap/user namespaces")
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


def test_convention_import_smoke_runs_genuinely_confined(art, monkeypatch):
    """The convention import smoke's command shape, through the REAL
    sandboxed run_shell on the designated gate: the declared module
    imports green from its declared layout inside a confined child, and a
    wrong-name component stays RED — the same witness the unit tier
    proves deterministically without a substrate claim."""
    functional, evidence = _bwrap_functional()
    assert functional, f"designated-gate prerequisite unmet — {evidence}"
    pkg = art / "webapp"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    rs = tools.make_run_shell(art)
    cmd = "python3 -c 'import sys; sys.path.insert(0, \".\"); import webapp'"
    out = rs(cmd=cmd, profile="full", timeout=30.0)
    assert out.startswith("exit_code: 0")
    wrong = rs(
        cmd="python3 -c 'import sys; sys.path.insert(0, \".\"); "
            "import webapp2'",
        profile="full", timeout=30.0)
    assert not wrong.startswith("exit_code: 0")   # real failure stays RED


def test_black_box_states_cover_every_substrate_descriptor():
    """The tier exercises every SUBSTRATE_STATES descriptor value (the three
    enforcement states plus the off bypass) as a real state, not a lambda."""
    from modulatio import access_surface as axs
    covered = {"sandboxed_full", "degraded_allowlist", "refused", "off"}
    assert covered == set(axs.SUBSTRATE_STATES)


def test_committed_substrate_evidence_is_internally_consistent():
    """The committed artifact must not claim provenance it does not contain.

    It is assembled in an external temporary file and copied into the tracked
    path last (``scripts/capture-substrate-evidence.sh``) precisely so the
    porcelain it records is the porcelain of the commit under test. Writing
    into the tracked path first dirties the worktree before porcelain is
    measured, and the artifact then lists ITSELF as modified while its header
    claims a clean capture — a self-contradicting record that a return letter
    can go on to repeat as fact."""
    import pathlib
    import re

    text = (pathlib.Path(__file__).resolve().parents[1]
            / "docs" / "gate-evidence" / "blackbox-substrate-tier.txt"
            ).read_text(encoding="utf-8")

    assert "captured BEFORE the run, from the clean commit under test" in text
    # A full 40-char sha names the tested code commit.
    assert re.search(r"^git rev-parse HEAD : [0-9a-f]{40}$", text,
                     re.MULTILINE), "no full code commit sha recorded"
    # The claim of a clean capture must be backed by an empty porcelain, and
    # in particular the artifact must never list itself.
    assert "<empty — clean worktree>" in text, "porcelain not recorded clean"
    assert "blackbox-substrate-tier.txt" not in text.split("## Host")[0], (
        "the artifact records itself as modified in its own provenance")
    # The six substrate cases and both timestamps survive.
    assert text.count(" PASSED") == 6, "six passing substrate cases expected"
    assert "6 passed" in text
    assert re.search(r"^run-started-utc\s*: \d{4}-\d\d-\d\dT", text,
                     re.MULTILINE)
    assert re.search(r"^run-finished-utc: \d{4}-\d\d-\d\dT", text,
                     re.MULTILINE)
