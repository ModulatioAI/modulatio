# SPDX-License-Identifier: Apache-2.0
"""Probe executor substrate.

The dedicated harness for exercising an assembled code deliverable: an
immutable snapshot mounted read-only, ONE private scratch as the only
writable bind, mandatory functional enforcement (bypass / profile off /
broken bwrap ⇒ ENGINE_UNAVAILABLE — producer code never runs unsandboxed),
per-phase deadlines with deliverable-caused timeouts attributed as PRODUCT
(hang-to-escape closed), bounded/normalized output, snapshot hash verified
after every phase, scratch destroyed in ``finally``.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from modulatio import code_probes as cp
from modulatio import sandbox


def _tree(tmp: Path, **files: str) -> Path:
    root = tmp / "art"
    for name, content in files.items():
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return root


# ── snapshot materialization  ──────────────────────────


def test_snapshot_copies_closure_and_hash_matches_digest(tmp_path):
    from modulatio import assembly

    root = _tree(tmp_path, **{"pyproject.toml": "[project]\nname='x'\n",
                              "src/p/m.py": "a = 1\n"})
    units = ["pyproject.toml", "src/p/m.py"]
    snap = cp.materialize_snapshot(units, root, tmp_path / "scratch")
    assert (snap.path / "pyproject.toml").read_text().startswith("[project]")
    assert (snap.path / "src/p/m.py").read_text() == "a = 1\n"
    # ONE identity: the snapshot hash IS the digest's snapshot hash.
    d = assembly.build_deliverable_digest({"units": units}, units, root,
                                          strategy="code")
    assert snap.content_hash == d.structure["snapshot_hash"]


def test_snapshot_refuses_symlink_escape(tmp_path):
    # Pin: a symlink pointing outside the closure is an explicit refusal,
    # never silently followed into the host.
    root = _tree(tmp_path, **{"ok.py": "x = 1\n"})
    secret = tmp_path / "cred.pem"
    secret.write_text("PRIVATE")
    (root / "leak.py").symlink_to(secret)
    with pytest.raises(cp.SnapshotError, match="symlink"):
        cp.materialize_snapshot(["ok.py", "leak.py"], root, tmp_path / "s")


def test_snapshot_verify_detects_mutation(tmp_path):
    root = _tree(tmp_path, **{"a.py": "a = 1\n"})
    snap = cp.materialize_snapshot(["a.py"], root, tmp_path / "s")
    assert snap.verify_unchanged() is True
    (snap.path / "a.py").chmod(0o644)
    (snap.path / "a.py").write_text("tampered")
    assert snap.verify_unchanged() is False


def test_snapshot_verify_compares_the_exact_tree(tmp_path):
    """verify_unchanged holds the EXACT tree — names, types, bytes: a file
    PLANTED into the snapshot, a planted directory, a removed file, or a
    recorded file retyped to a directory all void the evidence, not just
    rewritten bytes."""
    root = _tree(tmp_path, **{"a.py": "a = 1\n", "pkg/b.py": "b = 2\n"})
    snap = cp.materialize_snapshot(["a.py", "pkg/b.py"], root, tmp_path / "s")
    assert snap.verify_unchanged() is True

    planted = snap.path / "planted.py"
    planted.write_text("evil")
    assert snap.verify_unchanged() is False        # added file
    planted.unlink()
    assert snap.verify_unchanged() is True

    (snap.path / "newdir").mkdir()
    assert snap.verify_unchanged() is False        # added directory
    (snap.path / "newdir").rmdir()
    assert snap.verify_unchanged() is True

    b = snap.path / "pkg" / "b.py"
    b.chmod(0o644)
    b.unlink()
    assert snap.verify_unchanged() is False        # removed file
    b.mkdir()
    assert snap.verify_unchanged() is False        # retyped to directory


# ── mandatory enforcement — the bypass pins ──────────


def test_probe_refuses_to_run_unsandboxed(tmp_path, monkeypatch):
    """Bypass / profile off / broken bwrap ⇒ ENGINE_UNAVAILABLE and ZERO
    producer bytes execute — there is no soft fallback in this harness."""
    root = _tree(tmp_path, **{"a.py": "x = 1\n"})
    snap = cp.materialize_snapshot(["a.py"], root, tmp_path / "s")
    monkeypatch.setattr(
        sandbox, "enforcement_state",
        lambda: sandbox.EnforcementState.DEGRADED_ALLOWLIST)
    ran = []
    monkeypatch.setattr(cp.subprocess, "run",
                        lambda *a, **k: ran.append(a) or None)
    res = cp.run_probe_phase(["/usr/bin/true"], phase="install",
                             snapshot=snap, scratch=tmp_path / "s")
    assert res.status is cp.ProbeStatus.ENGINE_UNAVAILABLE
    assert res.origin == "engine" and res.phase == "install"
    assert ran == []                     # not a single producer byte


def test_trusted_profile_cannot_widen_probe_network(tmp_path, monkeypatch):
    """The operator's `trusted` posture forces network ON for agent shells;
    the probe evidence gate must NOT inherit that widening — the built argv
    keeps ``--unshare-net`` even when the global profile is trusted."""
    root = _tree(tmp_path, **{"a.py": "x = 1\n"})
    snap = cp.materialize_snapshot(["a.py"], root, tmp_path / "s")
    monkeypatch.setenv("MODULATIO_SANDBOX_PROFILE", "trusted")
    monkeypatch.setattr(sandbox, "enforcement_state",
                        lambda: sandbox.EnforcementState.SANDBOXED_FULL)
    seen = []

    def _capture(argv, **kwargs):
        seen.append(list(argv))
        raise OSError("argv captured; nothing spawned")

    monkeypatch.setattr(cp.subprocess, "Popen", _capture)
    cp.run_probe_phase(["/usr/bin/true"], phase="install",
                       snapshot=snap, scratch=tmp_path / "s")
    assert seen and "--unshare-net" in seen[0]


def test_probe_payload_carries_prlimit_prefix_inside_sandbox(
        tmp_path, monkeypatch):
    """Per-process OS limits must land on the PAYLOAD, not just bwrap's
    monitor (the monitor forks the payload; limits set on it after that fork
    never propagate). The payload argv — after bwrap's ``--`` — is wrapped in
    a ``prlimit`` prefix carrying the probe caps, established before exec."""
    import shutil as _shutil
    root = _tree(tmp_path, **{"a.py": "x = 1\n"})
    snap = cp.materialize_snapshot(["a.py"], root, tmp_path / "s")
    monkeypatch.setattr(sandbox, "enforcement_state",
                        lambda: sandbox.EnforcementState.SANDBOXED_FULL)
    seen = []

    def _capture(argv, **kwargs):
        seen.append(list(argv))
        raise OSError("argv captured; nothing spawned")

    monkeypatch.setattr(cp.subprocess, "Popen", _capture)
    cp.run_probe_phase(["/usr/bin/true"], phase="install",
                       snapshot=snap, scratch=tmp_path / "s", timeout_s=60)
    assert seen[0][-7:] == [
        _shutil.which("prlimit"),
        f"--as={cp._PROBE_RLIMIT_AS}",
        f"--fsize={cp._PROBE_RLIMIT_FSIZE}",
        "--core=0",
        "--cpu=65",
        "--",
        "/usr/bin/true",
    ]


def test_probe_without_prlimit_is_engine_unavailable(tmp_path, monkeypatch):
    """The probe envelope PROMISES per-process OS limits: with ``prlimit``
    absent the phase refuses disclosed (engine-attributed), never runs the
    payload uncapped."""
    root = _tree(tmp_path, **{"a.py": "x = 1\n"})
    snap = cp.materialize_snapshot(["a.py"], root, tmp_path / "s")
    monkeypatch.setattr(sandbox, "enforcement_state",
                        lambda: sandbox.EnforcementState.SANDBOXED_FULL)
    monkeypatch.setattr(cp, "_payload_prlimit_prefix", lambda timeout_s: [])
    ran = []
    monkeypatch.setattr(cp.subprocess, "Popen",
                        lambda *a, **k: ran.append(a) or None)
    res = cp.run_probe_phase(["/usr/bin/true"], phase="install",
                             snapshot=snap, scratch=tmp_path / "s")
    assert res.status is cp.ProbeStatus.ENGINE_UNAVAILABLE
    assert res.origin == "engine" and "prlimit" in res.reason
    assert ran == []                     # not a single producer byte


def test_missing_engine_executable_is_engine_unavailable(tmp_path, monkeypatch):
    """A missing ENGINE executable (the argv the engine authored, not the
    producer) is engine-unavailable — never product-attributed, and zero
    producer bytes execute."""
    root = _tree(tmp_path, **{"a.py": "x = 1\n"})
    snap = cp.materialize_snapshot(["a.py"], root, tmp_path / "s")
    monkeypatch.setattr(sandbox, "enforcement_state",
                        lambda: sandbox.EnforcementState.SANDBOXED_FULL)
    ran = []
    monkeypatch.setattr(cp.subprocess, "Popen",
                        lambda *a, **k: ran.append(a) or None)
    res = cp.run_probe_phase(["/nonexistent/bin/frobnicate"], phase="install",
                             snapshot=snap, scratch=tmp_path / "s")
    assert res.status is cp.ProbeStatus.ENGINE_UNAVAILABLE
    assert res.origin == "engine" and "executable" in res.reason
    assert ran == []                     # not a single producer byte


def test_exhausted_overall_budget_refuses_phase_as_engine(tmp_path, monkeypatch):
    """The overall digest deadline is a HARD wall, not an inter-phase
    checkpoint: a phase asked to start with the total budget already spent
    refuses as ENGINE_UNAVAILABLE with zero producer bytes."""
    import time as _time
    root = _tree(tmp_path, **{"a.py": "x = 1\n"})
    snap = cp.materialize_snapshot(["a.py"], root, tmp_path / "s")
    monkeypatch.setattr(sandbox, "enforcement_state",
                        lambda: sandbox.EnforcementState.SANDBOXED_FULL)
    ran = []
    monkeypatch.setattr(cp.subprocess, "Popen",
                        lambda *a, **k: ran.append(a) or None)
    token = cp._DEADLINE_VAR.set(_time.monotonic() - 1)
    try:
        res = cp.run_probe_phase(["/usr/bin/true"], phase="install",
                                 snapshot=snap, scratch=tmp_path / "s")
    finally:
        cp._DEADLINE_VAR.reset(token)
    assert res.status is cp.ProbeStatus.ENGINE_UNAVAILABLE
    assert res.origin == "engine" and "deadline" in res.reason
    assert ran == []                     # not a single producer byte


def test_drain_bounded_kills_at_fractional_wall():
    """With ~0.2s of budget left a silent payload is killed at ~0.2s — the
    wall is an absolute float, never rounded up to a gifted full second."""
    import subprocess as sp
    import sys as _sys
    import time as _time

    proc = sp.Popen([_sys.executable, "-c", "import time; time.sleep(30)"],
                    stdout=sp.PIPE, stderr=sp.STDOUT)
    try:
        t0 = _time.monotonic()
        with pytest.raises(sp.TimeoutExpired):
            cp._drain_bounded(proc, _time.monotonic() + 0.2)
        elapsed = _time.monotonic() - t0
        assert 0.1 <= elapsed < 0.9, elapsed
    finally:
        proc.kill()
        proc.wait()


def test_drain_bounded_no_wait_floor_after_eof():
    """A payload that closes stdout (EOF) and then lingers gains no
    one-second proc.wait floor — the same absolute wall binds the wait."""
    import subprocess as sp
    import sys as _sys
    import time as _time

    proc = sp.Popen(
        [_sys.executable, "-c", "import os, time; os.close(1); time.sleep(30)"],
        stdout=sp.PIPE, stderr=sp.DEVNULL)
    try:
        t0 = _time.monotonic()
        with pytest.raises(sp.TimeoutExpired):
            cp._drain_bounded(proc, _time.monotonic() + 0.3)
        elapsed = _time.monotonic() - t0
        assert elapsed < 0.9, elapsed
    finally:
        proc.kill()
        proc.wait()


def test_phase_wall_is_absolute_not_relative_after_spawn(tmp_path, monkeypatch):
    """Spawn/setup time consumes the SAME absolute budget: the wall handed
    to the drain is the total deadline itself, not a fresh relative timeout
    of the pre-spawn remaining value."""
    import time as _time
    root = _tree(tmp_path, **{"a.py": "x = 1\n"})
    snap = cp.materialize_snapshot(["a.py"], root, tmp_path / "s")
    monkeypatch.setattr(sandbox, "enforcement_state",
                        lambda: sandbox.EnforcementState.SANDBOXED_FULL)

    class _P:
        pid = 2 ** 22          # nonexistent: prlimit no-ops via lookup error
        returncode = 0
        args = ["x"]
        stdout = None

    monkeypatch.setattr(cp.subprocess, "Popen", lambda *a, **k: _P())
    seen = {}

    def _fake_drain(proc, wall):
        seen["wall"] = wall
        return b""

    monkeypatch.setattr(cp, "_drain_bounded", _fake_drain)
    total_deadline = _time.monotonic() + 5.0
    token = cp._DEADLINE_VAR.set(total_deadline)
    try:
        res = cp.run_probe_phase(["/usr/bin/true"], phase="install",
                                 snapshot=snap, scratch=tmp_path / "s",
                                 timeout_s=300)
    finally:
        cp._DEADLINE_VAR.reset(token)
    assert res.status is cp.ProbeStatus.OK
    assert abs(seen["wall"] - total_deadline) < 0.05


def test_expiry_during_engine_setup_never_spawns(tmp_path, monkeypatch):
    """The wall is rechecked immediately before Popen: when engine-side
    setup (argv construction) consumes the remaining budget, the phase
    refuses typed — bwrap is never started, so there is no race for payload
    bytes before the drain's first check."""
    root = _tree(tmp_path, **{"a.py": "x = 1\n"})
    snap = cp.materialize_snapshot(["a.py"], root, tmp_path / "s")
    monkeypatch.setattr(sandbox, "enforcement_state",
                        lambda: sandbox.EnforcementState.SANDBOXED_FULL)
    clock = {"t": 1000.0}
    monkeypatch.setattr(cp.time, "monotonic", lambda: clock["t"])

    def _slow_build(*a, **k):
        clock["t"] += 1.0                # setup outlives the 0.5s remaining
        return ["bwrap", "--", "/usr/bin/true"], {}

    monkeypatch.setattr(sandbox, "build_sandboxed_argv", _slow_build)
    ran = []
    monkeypatch.setattr(cp.subprocess, "Popen",
                        lambda *a, **k: ran.append(a) or None)
    token = cp._DEADLINE_VAR.set(1000.5)
    try:
        res = cp.run_probe_phase(["/usr/bin/true"], phase="install",
                                 snapshot=snap, scratch=tmp_path / "s",
                                 timeout_s=60)
    finally:
        cp._DEADLINE_VAR.reset(token)
    assert res.status is cp.ProbeStatus.ENGINE_UNAVAILABLE
    assert res.origin == "engine" and "deadline" in res.reason
    assert ran == []                     # Popen has ZERO calls


def test_rollup_deadline_binds_from_the_first_phase(tmp_path, monkeypatch):
    """run_execution_probes sets the absolute wall that every phase reads:
    with a zero total budget the FIRST phase (wheel build — previously
    unchecked) refuses as an engine deadline, and the rollup reports
    engine_unavailable."""
    monkeypatch.setattr(cp, "DIGEST_DEADLINE_S", 0)
    monkeypatch.setenv("MODULATIO_WHEELHOUSE", str(tmp_path))
    monkeypatch.setattr(sandbox, "enforcement_state",
                        lambda: sandbox.EnforcementState.SANDBOXED_FULL)
    ran = []
    monkeypatch.setattr(cp.subprocess, "Popen",
                        lambda *a, **k: ran.append(a) or None)
    root = _tree(tmp_path, **{
        "pyproject.toml": (
            "[build-system]\nrequires = ['hatchling']\n"
            "build-backend = 'hatchling.build'\n"
            "[project]\nname = 'pkg'\nversion = '0.1.0'\n"),
        "pkg/__init__.py": "x = 1\n",
    })
    facts = cp.run_execution_probes(
        ["pyproject.toml", "pkg/__init__.py"], root,
        scratch_root=tmp_path / "sr")
    assert facts["status"] == "engine_unavailable"
    assert "deadline" in facts["reason"]
    assert ran == []                     # not a single producer byte
    # The provenance envelope is disclosed on every digest, whatever the
    # outcome — complete undeclared-dep verification is never claimed.
    assert "not provenance audited" in facts["import_provenance"]


# ── live phase execution (bwrap required) ───────────────────────────────────

_needs_bwrap = pytest.mark.skipif(
    not sandbox.is_sandbox_available(), reason="bwrap required")


@pytest.fixture
def enforceable(monkeypatch):
    """The suite-wide conftest sets MODULATIO_RUN_SHELL_UNSAFE=1 so ordinary
    tests never need bwrap — under which the probe harness CORRECTLY refuses
    (bypass never runs producer code). Live pins need the real posture."""
    monkeypatch.delenv("MODULATIO_RUN_SHELL_UNSAFE", raising=False)
    monkeypatch.delenv("MODULATIO_SANDBOX_PROFILE", raising=False)
    sandbox.reset_enforcement_state_cache()
    yield
    sandbox.reset_enforcement_state_cache()


@_needs_bwrap
def test_phase_sees_snapshot_ro_and_scratch_rw(tmp_path, enforceable):
    root = _tree(tmp_path, **{"a.py": "x = 1\n"})
    scratch = tmp_path / "scratch"
    snap = cp.materialize_snapshot(["a.py"], root, scratch)
    probe = (
        "import sys\n"
        f"snap = {str(snap.path)!r}\n"
        f"scratch = {str(scratch)!r}\n"
        "open(snap + '/a.py').read()\n"                    # snapshot readable
        "try:\n"
        "    open(snap + '/evil', 'w')\n"
        "    sys.exit(3)\n"                                # write must fail
        "except OSError:\n"
        "    pass\n"
        "open(scratch + '/work.txt', 'w').write('ok')\n"   # scratch writable
    )
    res = cp.run_probe_phase(["/usr/bin/python3", "-c", probe],
                             phase="import_smoke", snapshot=snap,
                             scratch=scratch)
    assert res.status is cp.ProbeStatus.OK, res.output_tail
    assert (scratch / "work.txt").read_text() == "ok"
    assert snap.verify_unchanged() is True


@_needs_bwrap
def test_deliverable_hang_is_product_failed_not_unavailable(tmp_path, enforceable):
    """The hang-to-escape refinement : a
    deliverable-controlled timeout is PRODUCT evidence — an artifact cannot
    sleep its way past the gate."""
    root = _tree(tmp_path, **{"a.py": "x = 1\n"})
    scratch = tmp_path / "s"
    snap = cp.materialize_snapshot(["a.py"], root, scratch)
    res = cp.run_probe_phase(["/usr/bin/python3", "-c",
                              "import time; time.sleep(60)"],
                             phase="test", snapshot=snap, scratch=scratch,
                             timeout_s=2)
    assert res.status is cp.ProbeStatus.PRODUCT_FAILED
    assert res.origin == "deliverable"
    assert "timeout" in res.reason


@_needs_bwrap
def test_nonzero_exit_is_product_failed_with_bounded_clean_tail(tmp_path, enforceable):
    """status derives from the exit code, never the text; captured
    output is bounded and C0/ANSI-normalized before anyone reads it."""
    root = _tree(tmp_path, **{"a.py": "x = 1\n"})
    scratch = tmp_path / "s"
    snap = cp.materialize_snapshot(["a.py"], root, scratch)
    noisy = (
        "import sys\n"
        "sys.stderr.write('\\x1b[31mRED\\x07' + 'x' * 100000 + 'TAIL-END')\n"
        "sys.exit(4)\n"
    )
    res = cp.run_probe_phase(["/usr/bin/python3", "-c", noisy],
                             phase="entry_point", snapshot=snap,
                             scratch=scratch)
    assert res.status is cp.ProbeStatus.PRODUCT_FAILED
    assert res.returncode == 4
    assert "\x1b" not in res.output_tail and "\x07" not in res.output_tail
    assert len(res.output_tail) <= cp.OUTPUT_TAIL_CAP
    assert "TAIL-END" in res.output_tail            # the tail survives the cap


def test_destroy_scratch_always(tmp_path):
    root = _tree(tmp_path, **{"a.py": "x = 1\n"})
    scratch = tmp_path / "s"
    cp.materialize_snapshot(["a.py"], root, scratch)   # read-only files inside
    assert scratch.exists()
    cp.destroy_scratch(scratch)
    assert not scratch.exists()
    cp.destroy_scratch(scratch)                     # idempotent, never raises


# ── pipeline steps 4-6: wheel → metadata → pristine env → probes ───────


def _mini_wheel(dest: Path, name="demo_pkg", version="1.0.0",
                requires=(), console=None, body="def main():\n    print('hi')\n",
                extra_modules=(), provides=()) -> Path:
    """Hand-built pure wheel (stdlib zipfile — wheels install WITHOUT
    executing code, which is why the install phase is engine-side)."""
    import zipfile

    dist = f"{name}-{version}.dist-info"
    meta = ["Metadata-Version: 2.1", f"Name: {name}", f"Version: {version}"]
    meta += [f"Requires-Dist: {r}" for r in requires]
    meta += [f"Provides-Extra: {e}" for e in provides]
    entries = {
        f"{name}/__init__.py": body,
        f"{dist}/METADATA": "\n".join(meta) + "\n",
        f"{dist}/WHEEL": ("Wheel-Version: 1.0\nGenerator: test\n"
                          "Root-Is-Purelib: true\nTag: py3-none-any\n"),
    }
    for mod, mbody in extra_modules:
        entries[f"{name}/{mod}.py"] = mbody
    if console:
        entries[f"{dist}/entry_points.txt"] = (
            f"[console_scripts]\n{console} = {name}:main\n")
    entries[f"{dist}/RECORD"] = "".join(f"{p},,\n" for p in entries) + f"{dist}/RECORD,,\n"
    dest.mkdir(parents=True, exist_ok=True)
    whl = dest / f"{name}-{version}-py3-none-any.whl"
    with zipfile.ZipFile(whl, "w") as z:
        for p, c in entries.items():
            z.writestr(p, c)
    return whl


def test_clean_tail_fails_closed_when_redaction_unavailable(monkeypatch):
    """A redactor exception must WITHHOLD the excerpt, never pass unscrubbed
    output onward — failing open is the wrong direction for a mandatory
    secret boundary."""
    from modulatio import logstore

    def _boom(text):
        raise RuntimeError("redactor down")

    monkeypatch.setattr(logstore, "scrub_secrets", _boom)
    out = cp._clean_tail(b"planted AKIA1234567890EXAMPLE token")
    assert "withheld" in out
    assert "AKIA1234567890EXAMPLE" not in out


def test_wheel_metadata_caps_script_and_module_counts(tmp_path):
    """Entry-point/module COUNTS are bounded at the metadata layer (the
    zip-bomb ceiling class): a wheel minting thousands of console scripts
    would otherwise mint thousands of probe phases — facts, memo, and
    prompt all grow with it."""
    import zipfile

    dist = "big-1.0.0.dist-info"
    eps = "[console_scripts]\n" + "".join(
        f"s{i} = big:main\n"
        for i in range(cp._WHEEL_MAX_CONSOLE_SCRIPTS + 1))
    whl = tmp_path / "big-1.0.0-py3-none-any.whl"
    with zipfile.ZipFile(whl, "w") as z:
        z.writestr("big/__init__.py", "def main():\n    pass\n")
        z.writestr(f"{dist}/METADATA",
                   "Metadata-Version: 2.1\nName: big\nVersion: 1.0.0\n")
        z.writestr(f"{dist}/WHEEL", "Wheel-Version: 1.0\n")
        z.writestr(f"{dist}/entry_points.txt", eps)
    res = cp._validated_wheel_metadata([whl])
    assert isinstance(res, cp.ProbePhaseResult)
    assert res.status is cp.ProbeStatus.PRODUCT_FAILED
    assert "console script" in res.reason

    dist2 = "big2-1.0.0.dist-info"
    whl2 = tmp_path / "big2-1.0.0-py3-none-any.whl"
    with zipfile.ZipFile(whl2, "w") as z:
        z.writestr(f"{dist2}/METADATA",
                   "Metadata-Version: 2.1\nName: big2\nVersion: 1.0.0\n")
        z.writestr(f"{dist2}/WHEEL", "Wheel-Version: 1.0\n")
        for i in range(cp._WHEEL_MAX_MODULES + 1):
            z.writestr(f"big2/m{i}.py", "x = 1\n")
    res2 = cp._validated_wheel_metadata([whl2])
    assert isinstance(res2, cp.ProbePhaseResult)
    assert res2.status is cp.ProbeStatus.PRODUCT_FAILED
    assert "module" in res2.reason


def test_selected_test_extras_only_test_shaped(tmp_path):
    """Of a wheel's declared extras only the explicit test-shaped set is
    selected (docs/gpu never); the selection is TEST-ENVIRONMENT input —
    the disposable-env placement is pinned live in
    test_extras_install_only_in_disposable_env."""
    assert cp._selected_test_extras(["docs", "gpu", "test", "tests"]) == [
        "test", "tests"]
    assert cp._selected_test_extras(["docs", "gpu"]) == []
    wh = tmp_path / "wh"
    whl = _mini_wheel(wh, provides=("docs", "gpu", "test"))
    assert cp.inspect_wheel_metadata(whl)["extras"] == ["docs", "gpu", "test"]


def test_inactive_extra_direct_url_does_not_block(monkeypatch):
    """An INACTIVE optional extra's direct URL must not manufacture a
    withhold: markers are evaluated, so the docs-only URL passes the base
    scan and the selected-test scan, and activates only when its own extra
    is active. An unconditional URL stays active; malformed metadata stays
    a ValueError (product at the caller)."""
    docs_url = 'docs-helper @ https://example.invalid/docs.whl ; extra == "docs"'
    assert cp._direct_ref_violation([docs_url], active_extras=()) is None
    assert cp._direct_ref_violation([docs_url], active_extras=("test",)) is None
    assert cp._direct_ref_violation(
        [docs_url], active_extras=("docs",)) == docs_url
    plain = "evil @ http://127.0.0.1:1/evil.whl"
    assert cp._direct_ref_violation([plain], active_extras=()) == plain
    with pytest.raises(ValueError):
        cp._direct_ref_violation(["not a valid @@@ req"], active_extras=())


def test_marker_eval_uses_target_interpreter_env(monkeypatch):
    """Python-version/platform markers are evaluated against the SANDBOX
    target interpreter's environment, never accidentally against the
    engine's own interpreter."""
    fake_env = {"python_version": "3.4", "python_full_version": "3.4.0",
                "sys_platform": "linux", "os_name": "posix",
                "platform_system": "Linux", "platform_machine": "x86_64",
                "platform_python_implementation": "CPython",
                "platform_release": "", "platform_version": "",
                "implementation_name": "cpython",
                "implementation_version": "3.4.0"}
    monkeypatch.setattr(cp, "_pep508_env", lambda: fake_env)
    req = 'helper @ https://example.invalid/h.whl ; python_version >= "3.10"'
    # The ENGINE interpreter is >=3.10; the (fake) TARGET is 3.4 — inactive.
    assert cp._direct_ref_violation([req], active_extras=()) is None
    old = 'helper @ https://example.invalid/h.whl ; python_version < "3.10"'
    assert cp._direct_ref_violation([old], active_extras=()) == old


def test_selected_extra_direct_url_refuses_named_prespawn(tmp_path, monkeypatch):
    """Selecting the extra ACTIVATES its direct URL: provisioning refuses
    with the ONE exact named ENGINE_UNAVAILABLE(dependency_source) state and
    the producer seam (clone/seed) receives ZERO calls. The target marker
    environment is stubbed deterministic so this pin passes alone, cold, and
    in any order — the engine's own env query is not a producer spawn."""
    wh = tmp_path / "wh"
    (wh / "x").parent.mkdir(parents=True, exist_ok=True)
    whl = _mini_wheel(
        wh, provides=("test",),
        requires=('fetchy @ https://example.invalid/f.whl ; extra == "test"',))
    (wh / "pytest-1.0-py3-none-any.whl").write_bytes(b"stub")  # pass the guard
    monkeypatch.setenv("MODULATIO_WHEELHOUSE", str(wh))
    monkeypatch.setattr(cp, "_pep508_env", lambda: {
        "python_version": "3.12", "python_full_version": "3.12.0",
        "sys_platform": "linux", "os_name": "posix",
        "platform_system": "Linux", "platform_machine": "x86_64",
        "platform_python_implementation": "CPython",
        "platform_release": "", "platform_version": "",
        "implementation_name": "cpython",
        "implementation_version": "3.12.0"})
    ran = []
    monkeypatch.setattr(cp.subprocess, "Popen",
                        lambda *a, **k: ran.append(a) or None)
    root = _tree(tmp_path, **{"a.py": "x = 1\n"})
    snap = cp.materialize_snapshot(["a.py"], root, tmp_path / "s")
    test_env, res = cp.provision_test_env(
        tmp_path / "env", [whl], extras=("test",),
        snapshot=snap, scratch=tmp_path / "s")
    assert res.status is cp.ProbeStatus.ENGINE_UNAVAILABLE
    assert res.origin == "engine" and "dependency_source" in res.reason
    assert "fetchy" in res.reason
    assert ran == []                     # not a single producer byte


def test_cold_cache_env_query_is_engine_authored_not_producer(
        tmp_path, monkeypatch):
    """Cold-cache variant: with no cached target environment, the ONE
    subprocess is the engine-authored interpreter query — the producer seam
    (clone/seed) is never reached, and the refusal still lands (a failed
    dump degrades conservatively to active)."""
    wh = tmp_path / "wh"
    wh.mkdir(parents=True, exist_ok=True)
    whl = _mini_wheel(
        wh, provides=("test",),
        requires=('fetchy @ https://example.invalid/f.whl ; extra == "test"',))
    (wh / "pytest-1.0-py3-none-any.whl").write_bytes(b"stub")
    monkeypatch.setenv("MODULATIO_WHEELHOUSE", str(wh))
    monkeypatch.setattr(cp, "_PEP508_ENV_CACHE", None)
    ran = []

    def _capture(argv, **kwargs):
        ran.append(list(argv))
        raise OSError("captured; nothing spawned")

    monkeypatch.setattr(cp.subprocess, "Popen", _capture)
    root = _tree(tmp_path, **{"a.py": "x = 1\n"})
    snap = cp.materialize_snapshot(["a.py"], root, tmp_path / "s")
    _, res = cp.provision_test_env(
        tmp_path / "env", [whl], extras=("test",),
        snapshot=snap, scratch=tmp_path / "s")
    assert res.status is cp.ProbeStatus.ENGINE_UNAVAILABLE
    assert "dependency_source" in res.reason
    # the only attempted spawn is the engine's own target-env query
    assert len(ran) == 1 and ran[0][0] == cp._SANDBOX_PYTHON
    assert not any("cp" == a[0] or "pip" in " ".join(map(str, a))
                   for a in ran)         # clone/seed never reached


def test_env_manifest_failure_is_typed_engine_evidence(tmp_path, monkeypatch):
    """A manifest collection failure is a TYPED phase result (engine), never
    a silent empty list — recorded by the rollup, it withholds; a green
    digest can't lack its claimed manifest evidence."""
    root = _tree(tmp_path, **{"a.py": "x = 1\n"})
    snap = cp.materialize_snapshot(["a.py"], root, tmp_path / "s")
    monkeypatch.setattr(
        sandbox, "enforcement_state",
        lambda: sandbox.EnforcementState.DEGRADED_ALLOWLIST)
    manifest, res = cp.env_manifest(tmp_path / "env", snapshot=snap,
                                    scratch=tmp_path / "s")
    assert manifest == []
    assert res.status is cp.ProbeStatus.ENGINE_UNAVAILABLE
    assert res.origin == "engine" and "manifest" in res.reason


def test_wheel_phase_without_wheelhouse_is_engine_unavailable(tmp_path, monkeypatch):
    """Day-one truth on an unpopulated host : a missing approved
    local wheel source is an ENGINE limitation — named, never a product
    defect, never silently green."""
    monkeypatch.delenv("MODULATIO_WHEELHOUSE", raising=False)
    root = _tree(tmp_path, **{"pyproject.toml": "[project]\nname='x'\nversion='0'\n"})
    scratch = tmp_path / "s"
    snap = cp.materialize_snapshot(["pyproject.toml"], root, scratch)
    res = cp.build_wheel_phase(snap, scratch)
    assert res.status is cp.ProbeStatus.ENGINE_UNAVAILABLE
    assert res.origin == "engine" and "wheelhouse" in res.reason


def test_inspect_wheel_metadata_reads_without_importing(tmp_path):
    whl = _mini_wheel(tmp_path / "wh", requires=("left-pad>=1",),
                      console="demo", extra_modules=(("util", "u = 1\n"),))
    md = cp.inspect_wheel_metadata(whl)
    assert md["name"] == "demo_pkg" and md["version"] == "1.0.0"
    assert md["requires_dist"] == ["left-pad>=1"]
    assert md["console_scripts"] == {"demo": "demo_pkg:main"}
    assert sorted(md["modules"]) == ["demo_pkg", "demo_pkg.util"]


@_needs_bwrap
def test_pristine_install_and_missing_declared_dep_attribution(
        tmp_path, enforceable):
    """Hermetic install into a fresh env, all INSIDE the sandbox. A
    DECLARED dep absent from the local source is ENGINE_UNAVAILABLE(
    dependency_source) — the named-dep fact, not a product failure."""
    wh = tmp_path / "wh"
    good = _mini_wheel(wh)
    scratch = tmp_path / "s"
    env, mk = cp.create_pristine_env(scratch)
    assert mk.status is cp.ProbeStatus.OK
    ok = cp.install_wheels_phase(env, [good], wheelhouse=wh,
                                 scratch=scratch)
    assert ok.status is cp.ProbeStatus.OK, ok.output_tail

    needy = _mini_wheel(tmp_path / "wh2", name="needy",
                        requires=("absent-dep>=2",))
    scratch2 = tmp_path / "s2"
    env2, _ = cp.create_pristine_env(scratch2)
    res = cp.install_wheels_phase(env2, [needy], wheelhouse=tmp_path / "wh2",
                                  scratch=scratch2)
    assert res.status is cp.ProbeStatus.ENGINE_UNAVAILABLE
    assert res.origin == "engine"
    assert "absent-dep" in res.reason            # the missing dep is NAMED


@_needs_bwrap
def test_direct_url_dependency_cannot_connect_under_net_off(tmp_path, enforceable):
    """A built wheel declaring a direct-URL dependency can't reach the
    network: requirement inspection refuses it as ONE exact named state
    before any spawn, and no listener connection ever happens."""
    wh = tmp_path / "wh"
    evil = _mini_wheel(wh, name="reachy",
                       requires=("evil @ http://127.0.0.1:1/evil.whl",))
    scratch = tmp_path / "s"
    env, _ = cp.create_pristine_env(scratch)
    res = cp.install_wheels_phase(env, [evil], wheelhouse=wh, scratch=scratch)
    assert res.status is cp.ProbeStatus.ENGINE_UNAVAILABLE
    assert "dependency_source" in res.reason
    assert "Connection refused" not in res.output_tail   # never reached a socket


@_needs_bwrap
def test_probe_network_stays_unshared_under_trusted_profile(
        tmp_path, enforceable, monkeypatch):
    """Live pin for the trusted-widening gate: with the operator posture at
    ``trusted``, a probe phase still runs with the network unshared — the
    in-sandbox connect fails and a real host listener sees ZERO connections."""
    import socket
    import threading

    monkeypatch.setenv("MODULATIO_SANDBOX_PROFILE", "trusted")
    sandbox.reset_enforcement_state_cache()
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    srv.settimeout(0.1)
    port = srv.getsockname()[1]
    hits: list[int] = []
    stop = threading.Event()

    def _accept():
        while not stop.is_set():
            try:
                conn, _ = srv.accept()
            except TimeoutError:
                continue
            hits.append(1)
            conn.close()

    t = threading.Thread(target=_accept, daemon=True)
    t.start()
    try:
        root = _tree(tmp_path, **{"a.py": "x = 1\n"})
        scratch = tmp_path / "scratch"
        snap = cp.materialize_snapshot(["a.py"], root, scratch)
        probe = (
            "import socket, sys\n"
            "s = socket.socket()\n"
            "s.settimeout(3)\n"
            "try:\n"
            f"    s.connect(('127.0.0.1', {port}))\n"
            "    sys.exit(3)\n"                 # a connection = gate widened
            "except OSError:\n"
            "    sys.exit(0)\n"
        )
        res = cp.run_probe_phase(["/usr/bin/python3", "-c", probe],
                                 phase="import_smoke", snapshot=snap,
                                 scratch=scratch)
    finally:
        stop.set()
        t.join(timeout=2)
        srv.close()
    assert res.status is cp.ProbeStatus.OK, res.output_tail
    assert hits == []                    # the listener never saw a connection


@_needs_bwrap
def test_payload_and_forked_child_see_probe_rlimits(tmp_path, enforceable):
    """Live pin: the payload's OWN ``getrlimit`` reflects the probe caps —
    proof the limits reached the process bwrap forked, not merely the
    monitor — and a child the payload forks inherits them."""
    root = _tree(tmp_path, **{"a.py": "x = 1\n"})
    scratch = tmp_path / "scratch"
    snap = cp.materialize_snapshot(["a.py"], root, scratch)
    probe = (
        "import os, resource, sys\n"
        f"want_as = {cp._PROBE_RLIMIT_AS}\n"
        f"want_fs = {cp._PROBE_RLIMIT_FSIZE}\n"
        "def ok():\n"
        "    return (resource.getrlimit(resource.RLIMIT_AS)[0] == want_as\n"
        "            and resource.getrlimit(resource.RLIMIT_FSIZE)[0] == want_fs)\n"
        "if not ok():\n"
        "    sys.exit(3)\n"              # payload itself is uncapped
        "pid = os.fork()\n"
        "if pid == 0:\n"
        "    os._exit(0 if ok() else 4)\n"   # forked child must inherit
        "_, st = os.waitpid(pid, 0)\n"
        "sys.exit(0 if os.WEXITSTATUS(st) == 0 else 4)\n"
    )
    res = cp.run_probe_phase(["/usr/bin/python3", "-c", probe],
                             phase="import_smoke", snapshot=snap,
                             scratch=scratch)
    assert res.status is cp.ProbeStatus.OK, (res.reason, res.output_tail)


@_needs_bwrap
def test_phase_killed_at_total_budget_is_engine_not_product(
        tmp_path, enforceable):
    """A phase whose OWN limit is generous but which crosses the total
    budget dies AT the total boundary as ENGINE_UNAVAILABLE (engine policy
    ceiling) — not after its full per-phase timeout as a product hang."""
    import time as _time
    root = _tree(tmp_path, **{"a.py": "x = 1\n"})
    scratch = tmp_path / "scratch"
    snap = cp.materialize_snapshot(["a.py"], root, scratch)
    token = cp._DEADLINE_VAR.set(_time.monotonic() + 2)
    try:
        res = cp.run_probe_phase(
            ["/usr/bin/python3", "-c", "import time; time.sleep(30)"],
            phase="run_tests", snapshot=snap, scratch=scratch, timeout_s=60)
    finally:
        cp._DEADLINE_VAR.reset(token)
    assert res.status is cp.ProbeStatus.ENGINE_UNAVAILABLE
    assert res.origin == "engine" and "deadline" in res.reason
    assert res.duration_s < 20           # killed at the wall, not at 30/60s


@_needs_bwrap
def test_product_hang_under_ample_budget_stays_product_failed(
        tmp_path, enforceable):
    """The discrimination pin: with plenty of total budget remaining, a
    hang that exhausts the phase's OWN smaller timeout stays deliverable-
    attributed PRODUCT_FAILED."""
    import time as _time
    root = _tree(tmp_path, **{"a.py": "x = 1\n"})
    scratch = tmp_path / "scratch"
    snap = cp.materialize_snapshot(["a.py"], root, scratch)
    token = cp._DEADLINE_VAR.set(_time.monotonic() + 900)
    try:
        res = cp.run_probe_phase(
            ["/usr/bin/python3", "-c", "import time; time.sleep(30)"],
            phase="run_tests", snapshot=snap, scratch=scratch, timeout_s=1)
    finally:
        cp._DEADLINE_VAR.reset(token)
    assert res.status is cp.ProbeStatus.PRODUCT_FAILED
    assert res.origin == "deliverable" and "timeout" in res.reason


@_needs_bwrap
def test_sandbox_setup_failure_is_engine_not_product(
        tmp_path, enforceable, monkeypatch):
    """A bwrap failure BEFORE the payload starts (bad mount graph — here an
    unparsable tmpfs size) has no child-start handshake and is
    deterministically engine-attributed; the payload never runs."""
    monkeypatch.setattr(cp, "_PROBE_TMPFS_SIZE", -1)
    root = _tree(tmp_path, **{"a.py": "x = 1\n"})
    scratch = tmp_path / "scratch"
    snap = cp.materialize_snapshot(["a.py"], root, scratch)
    marker = scratch / "payload_ran.txt"
    res = cp.run_probe_phase(
        ["/usr/bin/python3", "-c",
         f"open({str(marker)!r}, 'w').write('ran')"],
        phase="install", snapshot=snap, scratch=scratch)
    assert res.status is cp.ProbeStatus.ENGINE_UNAVAILABLE
    assert res.origin == "engine" and "payload" in res.reason
    assert not marker.exists()           # not a single producer byte


def test_declared_direct_url_requirement_is_named_deterministic(tmp_path):
    """A declared direct-URL/VCS requirement under the no-network policy is
    ONE exact state — named ENGINE_UNAVAILABLE(dependency_source), decided
    by requirement inspection BEFORE any spawn — never whichever text pip
    happens to print."""
    wh = tmp_path / "wh"
    for i, req in enumerate((
            "evil @ http://127.0.0.1:1/evil.whl",
            "tool @ git+https://example.invalid/x/y.git")):
        whl = _mini_wheel(wh, name=f"reachy{i}", requires=(req,))
        res = cp.install_wheels_phase(
            tmp_path / "env", [whl], wheelhouse=wh, scratch=tmp_path / "s")
        assert res.status is cp.ProbeStatus.ENGINE_UNAVAILABLE
        assert res.origin == "engine"
        assert "dependency_source" in res.reason
        assert req.split(" @ ")[0] in res.reason   # the requirement is NAMED


@_needs_bwrap
def test_entry_point_and_import_probes_run_sandboxed(tmp_path, enforceable):
    """Phase 6 live: console script answers, per-module import smoke passes
    for a good package; an import-time crash in a module NO test touches is
    caught (a module import that only fails at import time)."""
    wh = tmp_path / "wh"
    whl = _mini_wheel(wh, console="demo",
                      extra_modules=(("broken", "import nonexistent_dep_xyz\n"),))
    scratch = tmp_path / "s"
    root = _tree(tmp_path, **{"pyproject.toml": "[project]\nname='demo_pkg'\n"})
    snap = cp.materialize_snapshot(["pyproject.toml"], root, scratch)
    env, _ = cp.create_pristine_env(scratch, snapshot=snap)
    assert cp.install_wheels_phase(
        env, [whl], wheelhouse=wh, snapshot=snap,
        scratch=scratch).status is cp.ProbeStatus.OK

    entry = cp.entry_point_probe(env, "demo", snapshot=snap, scratch=scratch)
    assert entry.status is cp.ProbeStatus.OK, entry.output_tail

    imports = cp.import_smoke_phase(
        env, ["demo_pkg", "demo_pkg.broken"], snapshot=snap, scratch=scratch)
    assert imports.status is cp.ProbeStatus.PRODUCT_FAILED
    assert "demo_pkg.broken" in imports.reason   # the dead module is NAMED


# ── phase 7 + rollup (steps 7-8) ─────────────────────────────────────


def test_wheelhouse_defaults_to_config_dir(tmp_path, monkeypatch):
    """MODULATIO_WHEELHOUSE overrides; else <CONFIG_DIR>/wheelhouse when it
    exists (the doctor/docs population target)."""
    monkeypatch.delenv("MODULATIO_WHEELHOUSE", raising=False)
    from modulatio import config
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    assert cp.wheelhouse_path() is None            # not populated yet
    (tmp_path / "wheelhouse").mkdir()
    assert cp.wheelhouse_path() == tmp_path / "wheelhouse"
    monkeypatch.setenv("MODULATIO_WHEELHOUSE", str(tmp_path))
    assert cp.wheelhouse_path() == tmp_path        # env wins


#: Discovered at IMPORT time (the real host config) — the suite conftest
#: redirects CONFIG_DIR at test time for isolation, so live tests re-pin the
#: real wheelhouse through the env override.
_REAL_WHEELHOUSE = cp.wheelhouse_path()


def _wheelhouse_ready() -> bool:
    return (_REAL_WHEELHOUSE is not None
            and any(_REAL_WHEELHOUSE.glob("pytest-*.whl")))


_needs_wheelhouse = pytest.mark.skipif(
    not _wheelhouse_ready(), reason="populated wheelhouse required")


@pytest.fixture
def real_wheelhouse(monkeypatch):
    monkeypatch.setenv("MODULATIO_WHEELHOUSE", str(_REAL_WHEELHOUSE))
    return _REAL_WHEELHOUSE


@_needs_bwrap
@_needs_wheelhouse
def test_wheel_build_happy_path_and_test_phase_green_and_red(
        tmp_path, enforceable, real_wheelhouse):
    """The full pipeline live: a REAL pyproject builds a wheel in the
    sandbox from the wheelhouse backend; the runner-seeded test phase goes
    green on a passing suite and red on a failing one — with plugin
    autoload disabled and no extras silently added."""
    root = _tree(tmp_path, **{
        "pyproject.toml": (
            "[build-system]\nrequires = ['hatchling']\n"
            "build-backend = 'hatchling.build'\n"
            "[project]\nname = 'livepkg'\nversion = '0.1.0'\n"),
        "livepkg/__init__.py": "def add(a, b):\n    return a + b\n",
        "tests/test_ok.py": (
            "from livepkg import add\n"
            "def test_add():\n    assert add(1, 2) == 3\n"),
    })
    units = ["pyproject.toml", "livepkg/__init__.py", "tests/test_ok.py"]
    scratch = tmp_path / "s"
    snap = cp.materialize_snapshot(units, root, scratch)

    built = cp.build_wheel_phase(snap, scratch)
    assert built.status is cp.ProbeStatus.OK, built.output_tail
    wheels = list((scratch / "wheels").glob("*.whl"))
    assert len(wheels) == 1

    env, mk = cp.create_pristine_env(scratch, snapshot=snap)
    assert mk.status is cp.ProbeStatus.OK, mk.output_tail
    inst = cp.install_wheels_phase(env, wheels, wheelhouse=cp.wheelhouse_path(),
                                   snapshot=snap, scratch=scratch)
    assert inst.status is cp.ProbeStatus.OK, inst.output_tail

    test_env, prov = cp.provision_test_env(env, wheels, snapshot=snap,
                                           scratch=scratch)
    assert prov.status is cp.ProbeStatus.OK, prov.output_tail
    green = cp.run_tests_phase(test_env, snapshot=snap, scratch=scratch)
    assert green.status is cp.ProbeStatus.OK, green.output_tail

    # flip the suite red — the phase reports PRODUCT evidence
    (root / "tests" / "test_ok.py").write_text(
        "def test_bad():\n    assert False\n")
    scratch2 = tmp_path / "s2"
    snap2 = cp.materialize_snapshot(units, root, scratch2)
    env2, _ = cp.create_pristine_env(scratch2, snapshot=snap2)
    cp.build_wheel_phase(snap2, scratch2)
    wheels2 = list((scratch2 / "wheels").glob("*.whl"))
    inst2 = cp.install_wheels_phase(
        env2, wheels2,
        wheelhouse=cp.wheelhouse_path(), snapshot=snap2, scratch=scratch2)
    assert inst2.status is cp.ProbeStatus.OK, inst2.output_tail
    test_env2, prov2 = cp.provision_test_env(env2, wheels2, snapshot=snap2,
                                             scratch=scratch2)
    assert prov2.status is cp.ProbeStatus.OK, prov2.output_tail
    red = cp.run_tests_phase(test_env2, snapshot=snap2, scratch=scratch2)
    assert red.status is cp.ProbeStatus.PRODUCT_FAILED
    assert "test_bad" in red.output_tail


@_needs_bwrap
@_needs_wheelhouse
def test_source_cannot_shadow_the_installed_wheel(
        tmp_path, enforceable, real_wheelhouse):
    """The false green this gate exists to kill: the SOURCE tree carries a module
    the WHEEL omits, and a test imports it. Judged against the wheel the
    suite must fail — passing would mean the snapshot source shadowed the
    installed artifact."""
    root = _tree(tmp_path, **{
        "pyproject.toml": (
            "[build-system]\nrequires = ['hatchling']\n"
            "build-backend = 'hatchling.build'\n"
            "[project]\nname = 'shadowpkg'\nversion = '0.1.0'\n"
            "[tool.hatch.build.targets.wheel]\npackages = ['shadowpkg']\n"),
        "shadowpkg/__init__.py": "x = 1\n",
        "helper.py": "SECRET = 42\n",          # in source, NOT in the wheel
        # tests is a PACKAGE: pytest's prepend import mode walks up past it
        # and puts the snapshot ROOT on sys.path — the shadowing shape.
        "tests/__init__.py": "",
        "tests/test_uses_helper.py": (
            "import helper\n"
            "def test_secret():\n    assert helper.SECRET == 42\n"),
    })
    units = ["pyproject.toml", "shadowpkg/__init__.py", "helper.py",
             "tests/__init__.py", "tests/test_uses_helper.py"]
    scratch = tmp_path / "s"
    snap = cp.materialize_snapshot(units, root, scratch)
    assert cp.build_wheel_phase(snap, scratch).status is cp.ProbeStatus.OK
    wheels = list((scratch / "wheels").glob("*.whl"))
    md = cp.inspect_wheel_metadata(wheels[0])
    assert "helper" not in md["modules"]       # the wheel really omits it
    env, _ = cp.create_pristine_env(scratch, snapshot=snap)
    assert cp.install_wheels_phase(
        env, wheels, wheelhouse=cp.wheelhouse_path(),
        snapshot=snap, scratch=scratch).status is cp.ProbeStatus.OK
    test_env, prov = cp.provision_test_env(env, wheels, snapshot=snap,
                                           scratch=scratch)
    assert prov.status is cp.ProbeStatus.OK, prov.output_tail
    res = cp.run_tests_phase(test_env, snapshot=snap, scratch=scratch)
    assert res.status is cp.ProbeStatus.PRODUCT_FAILED, res.output_tail


@_needs_bwrap
@_needs_wheelhouse
def test_import_smoke_poisoning_cannot_green_the_next_module(
        tmp_path, enforceable, real_wheelhouse):
    """Each module smokes in a FRESH process: an early module pre-seeding
    sys.modules for a broken sibling must not green that sibling's
    import."""
    wh = tmp_path / "wh"
    whl = _mini_wheel(wh, extra_modules=(
        ("aaa_poison",
         "import sys, types\n"
         "sys.modules['demo_pkg.broken'] = types.ModuleType('x')\n"),
        ("broken", "import nonexistent_dep_xyz\n"),
    ))
    scratch = tmp_path / "s"
    root = _tree(tmp_path, **{"pyproject.toml": "[project]\nname='d'\n"})
    snap = cp.materialize_snapshot(["pyproject.toml"], root, scratch)
    env, _ = cp.create_pristine_env(scratch, snapshot=snap)
    assert cp.install_wheels_phase(
        env, [whl], wheelhouse=wh, snapshot=snap,
        scratch=scratch).status is cp.ProbeStatus.OK
    res = cp.import_smoke_phase(
        env, ["demo_pkg.aaa_poison", "demo_pkg.broken"],
        snapshot=snap, scratch=scratch)
    assert res.status is cp.ProbeStatus.PRODUCT_FAILED
    assert "demo_pkg.broken" in res.reason


@_needs_bwrap
def test_runtime_dep_hidden_in_test_extra_fails_pristine_smoke(
        tmp_path, enforceable, monkeypatch):
    """The false separation this pin exists to kill: a wheel imports D at runtime
    but declares D only under its selected `test` extra. The PRISTINE env
    never sees the extra, so eager import smoke fails BY NAME and the
    product manifest excludes D. The same wheel with D as an ordinary
    runtime requirement passes."""
    wh = tmp_path / "wh"
    _mini_wheel(wh, name="reqddep", body="x = 1\n")
    hidden = _mini_wheel(
        wh, name="demo_pkg", provides=("test",),
        requires=('reqddep ; extra == "test"',),
        body="import reqddep\n")
    scratch = tmp_path / "s"
    root = _tree(tmp_path, **{"pyproject.toml": "[project]\nname='d'\n"})
    snap = cp.materialize_snapshot(["pyproject.toml"], root, scratch)
    env, _ = cp.create_pristine_env(scratch, snapshot=snap)
    assert cp.install_wheels_phase(
        env, [hidden], wheelhouse=wh, snapshot=snap,
        scratch=scratch).status is cp.ProbeStatus.OK
    manifest, man_res = cp.env_manifest(env, snapshot=snap, scratch=scratch)
    assert man_res.status is cp.ProbeStatus.OK
    assert not any("reqddep" in m for m in manifest)   # D not in product
    res = cp.import_smoke_phase(env, ["demo_pkg"], snapshot=snap,
                                scratch=scratch)
    assert res.status is cp.ProbeStatus.PRODUCT_FAILED
    assert "demo_pkg" in res.reason                    # fails BY NAME

    # ordinary runtime requirement: same import, declared honestly → passes
    wh2 = tmp_path / "wh2"
    _mini_wheel(wh2, name="reqddep", body="x = 1\n")
    honest = _mini_wheel(wh2, name="demo_pkg", requires=("reqddep",),
                         body="import reqddep\n")
    scratch2 = tmp_path / "s2"
    snap2 = cp.materialize_snapshot(["pyproject.toml"], root, scratch2)
    env2, _ = cp.create_pristine_env(scratch2, snapshot=snap2)
    assert cp.install_wheels_phase(
        env2, [honest], wheelhouse=wh2, snapshot=snap2,
        scratch=scratch2).status is cp.ProbeStatus.OK
    ok = cp.import_smoke_phase(env2, ["demo_pkg"], snapshot=snap2,
                               scratch=scratch2)
    assert ok.status is cp.ProbeStatus.OK, ok.output_tail


@_needs_bwrap
@_needs_wheelhouse
def test_extras_install_only_in_disposable_env(
        tmp_path, enforceable, real_wheelhouse, monkeypatch):
    """Selected test extras are TEST-ENVIRONMENT input: with docs+gpu+test
    declared, only `test` installs, only in the disposable env — the
    product manifest excludes its closure, the PRE-test runner manifest
    includes it. With no selection, provisioning adds nothing beyond the
    runner."""
    import shutil as _shutil
    wh = tmp_path / "wh"
    wh.mkdir()
    for w in Path(_REAL_WHEELHOUSE).glob("*.whl"):
        _shutil.copy(w, wh)
    _mini_wheel(wh, name="reqddep", body="x = 1\n")
    whl = _mini_wheel(
        wh, name="demo_pkg", provides=("docs", "gpu", "test"),
        requires=('reqddep ; extra == "test"',
                  'docshelper ; extra == "docs"'),
        body="x = 1\n")
    monkeypatch.setenv("MODULATIO_WHEELHOUSE", str(wh))
    scratch = tmp_path / "s"
    root = _tree(tmp_path, **{"pyproject.toml": "[project]\nname='d'\n"})
    snap = cp.materialize_snapshot(["pyproject.toml"], root, scratch)
    env, _ = cp.create_pristine_env(scratch, snapshot=snap)
    assert cp.install_wheels_phase(
        env, [whl], wheelhouse=wh, snapshot=snap,
        scratch=scratch).status is cp.ProbeStatus.OK
    product, _pres = cp.env_manifest(env, snapshot=snap, scratch=scratch)
    assert not any("reqddep" in m for m in product)

    selected = cp._selected_test_extras(
        cp.inspect_wheel_metadata(whl)["extras"])
    assert selected == ["test"]                    # docs/gpu never selected
    test_env, prov = cp.provision_test_env(
        env, [whl], extras=tuple(selected), snapshot=snap, scratch=scratch)
    assert prov.status is cp.ProbeStatus.OK, prov.output_tail
    runner, rres = cp.env_manifest(test_env, snapshot=snap, scratch=scratch,
                                   phase="runner_manifest")
    assert rres.status is cp.ProbeStatus.OK
    assert any("reqddep" in m for m in runner)     # test extra: IN runner
    assert any("pytest" in m for m in runner)
    assert not any("docshelper" in m for m in runner)   # docs never rides
    # empty selection adds nothing beyond the runner
    scratch2 = tmp_path / "s2"
    (scratch2 / "envs").mkdir(parents=True)
    snap2 = cp.materialize_snapshot(["pyproject.toml"], root, scratch2)
    env2, _ = cp.create_pristine_env(scratch2, snapshot=snap2)
    assert cp.install_wheels_phase(
        env2, [whl], wheelhouse=wh, snapshot=snap2,
        scratch=scratch2).status is cp.ProbeStatus.OK
    test_env2, prov2 = cp.provision_test_env(
        env2, [whl], extras=(), snapshot=snap2, scratch=scratch2)
    assert prov2.status is cp.ProbeStatus.OK, prov2.output_tail
    runner2, _r2 = cp.env_manifest(test_env2, snapshot=snap2,
                                   scratch=scratch2, phase="runner_manifest")
    assert not any("reqddep" in m for m in runner2)


@_needs_bwrap
def test_oversized_manifest_is_typed_unavailable(
        tmp_path, enforceable, monkeypatch):
    """A manifest larger than the dedicated channel's explicit cap is a
    TYPED unavailable outcome — never a head/tail splice presented as a
    complete manifest."""
    monkeypatch.setattr(cp, "_MANIFEST_FILE_CAP", 10)
    wh = tmp_path / "wh"
    whl = _mini_wheel(wh, body="x = 1\n")
    scratch = tmp_path / "s"
    root = _tree(tmp_path, **{"pyproject.toml": "[project]\nname='d'\n"})
    snap = cp.materialize_snapshot(["pyproject.toml"], root, scratch)
    env, mk = cp.create_pristine_env(scratch, snapshot=snap)
    assert mk.status is cp.ProbeStatus.OK
    assert cp.install_wheels_phase(
        env, [whl], wheelhouse=wh, snapshot=snap,
        scratch=scratch).status is cp.ProbeStatus.OK
    manifest, res = cp.env_manifest(env, snapshot=snap, scratch=scratch)
    assert manifest == []
    assert res.status is cp.ProbeStatus.ENGINE_UNAVAILABLE
    assert "cap" in res.reason


@_needs_bwrap
@_needs_wheelhouse
def test_planted_dist_info_cannot_alter_pretest_runner_manifest(
        tmp_path, enforceable, real_wheelhouse):
    """The runner manifest is recorded BEFORE any product test byte runs:
    test code that plants dist-info into its own environment cannot alter
    the recorded pre-test fact (a post-test collection would show the
    plant — proving the attack ran and the ordering is what protects the
    evidence)."""
    root = _tree(tmp_path, **{
        "pyproject.toml": "[project]\nname='d'\n",
        "tests/test_plant.py": (
            "import os, sysconfig\n"
            "def test_plant():\n"
            "    d = os.path.join(sysconfig.get_paths()['purelib'],\n"
            "                     'evildist-1.0.dist-info')\n"
            "    os.makedirs(d, exist_ok=True)\n"
            "    open(os.path.join(d, 'METADATA'), 'w').write(\n"
            "        'Metadata-Version: 2.1\\nName: evildist\\nVersion: 1.0\\n')\n"
            "    open(os.path.join(d, 'RECORD'), 'w').write('')\n"),
    })
    units = ["pyproject.toml", "tests/test_plant.py"]
    scratch = tmp_path / "s"
    snap = cp.materialize_snapshot(units, root, scratch)
    env, _ = cp.create_pristine_env(scratch, snapshot=snap)
    test_env, prov = cp.provision_test_env(env, [], snapshot=snap,
                                           scratch=scratch)
    assert prov.status is cp.ProbeStatus.OK, prov.output_tail
    pre, pre_res = cp.env_manifest(test_env, snapshot=snap, scratch=scratch,
                                   phase="runner_manifest")
    assert pre_res.status is cp.ProbeStatus.OK
    assert not any("evildist" in m for m in pre)
    run = cp.run_tests_phase(test_env, snapshot=snap, scratch=scratch)
    assert run.status is cp.ProbeStatus.OK, run.output_tail
    post, _post_res = cp.env_manifest(test_env, snapshot=snap,
                                      scratch=scratch, phase="post")
    assert any("evildist" in m for m in post)      # the attack really ran
    assert not any("evildist" in m for m in pre)   # the recorded fact stands


def test_provision_without_runner_bundle_is_engine_unavailable(
        tmp_path, monkeypatch):
    """No pytest wheels in the approved source => the ENGINE cannot seed its
    runner — never a fallback to the live venv's pytest (the explicit
exclusion), never a product failure. Checked at the guard, before any
    sandbox work, so it needs no bwrap."""
    monkeypatch.setenv("MODULATIO_WHEELHOUSE", str(tmp_path / "empty"))
    (tmp_path / "empty").mkdir()
    root = _tree(tmp_path, **{"a.py": "x = 1\n"})
    scratch = tmp_path / "s"
    snap = cp.materialize_snapshot(["a.py"], root, scratch)
    _, res = cp.provision_test_env(_stub_env(scratch), [], snapshot=snap,
                                   scratch=scratch)
    assert res.status is cp.ProbeStatus.ENGINE_UNAVAILABLE
    assert "runner" in res.reason


def _stub_env(scratch):
    """A path standing in for an env — the runner-bundle guard rejects before
    it is ever used."""
    return Path(scratch) / "envs" / "pristine"


@_needs_bwrap
@_needs_wheelhouse
def test_run_execution_probes_rollup_full_pipeline(
        tmp_path, enforceable, real_wheelhouse):
    """Step 8's engine: the whole fixed order as one call — typed facts for
    the digest, every phase recorded, overall status mechanical."""
    root = _tree(tmp_path, **{
        "pyproject.toml": (
            "[build-system]\nrequires = ['hatchling']\n"
            "build-backend = 'hatchling.build'\n"
            "[project]\nname = 'rollpkg'\nversion = '0.1.0'\n"
            "[project.scripts]\nrollpkg = 'rollpkg:main'\n"),
        "rollpkg/__init__.py": (
            "def main():\n    import sys\n"
            "    if '--help' in sys.argv: print('usage: rollpkg'); return\n"),
        "tests/test_ok.py": "def test_ok():\n    assert True\n",
    })
    units = ["pyproject.toml", "rollpkg/__init__.py", "tests/test_ok.py"]
    facts = cp.run_execution_probes(units, root, scratch_root=tmp_path / "sr")
    assert facts["status"] == "ok"
    phases = {p["phase"]: p["status"] for p in facts["phases"]}
    assert phases["wheel"] == "ok"
    assert phases["install"] == "ok"
    assert phases["entry_point"] == "ok"
    assert phases["import_smoke"] == "ok"
    assert phases["test"] == "ok"
    assert facts["wheel"]["name"] == "rollpkg"
    assert facts["install_mode"] == "hermetic"
    assert facts["test_extras"] == []              # none selected, none added
    assert not (tmp_path / "sr").exists() or not any((tmp_path / "sr").iterdir())


def test_run_execution_probes_no_packaging_is_not_applicable(tmp_path):
    root = _tree(tmp_path, **{"script.sh": "echo hi\n"})
    facts = cp.run_execution_probes(["script.sh"], root,
                                    scratch_root=tmp_path / "sr")
    assert facts["status"] == "not_applicable"
    assert facts["reason"].startswith("no supported Python packaging")


# ── containment battery: hostile deliverable code, live ──────────
#
# The build phase runs the deliverable's OWN pyproject/setup.py build hooks —
# arbitrary code. These pins prove the empty-root sandbox contains it: writes
# to source/host denied, host secrets invisible, resource bombs die at the
# ceiling, network unavailable. The containment pin battery.


def _malicious_pkg(tmp: Path, setup_body: str) -> "tuple[list[str], Path]":
    """A setup.py-based package whose build hook runs setup_body at BUILD
    time (setuptools executes setup.py; this is the arbitrary-code surface)."""
    root = tmp / "art"
    (root).mkdir(parents=True, exist_ok=True)
    (root / "pyproject.toml").write_text(
        "[build-system]\nrequires = ['setuptools']\n"
        "build-backend = 'setuptools.build_meta'\n")
    (root / "setup.py").write_text(
        "from setuptools import setup\n" + setup_body + "\n"
        "setup(name='evil', version='0.0.1', py_modules=['evil'])\n")
    (root / "evil.py").write_text("x = 1\n")
    return ["pyproject.toml", "setup.py", "evil.py"], root


@_needs_bwrap
@_needs_wheelhouse
def test_build_hook_cannot_overwrite_the_snapshot_source(tmp_path, enforceable,
                                                         real_wheelhouse):
    units, root = _malicious_pkg(
        tmp_path,
        "import pathlib\n"
        "try:\n"
        "    pathlib.Path(__file__).with_name('evil.py').write_text('PWNED')\n"
        "except OSError:\n    pass\n")
    scratch = tmp_path / "s"
    snap = cp.materialize_snapshot(units, root, scratch)
    cp.build_wheel_phase(snap, scratch)
    # the source in the snapshot is byte-identical; the host original too
    assert (snap.path / "evil.py").read_text() == "x = 1\n"
    assert (root / "evil.py").read_text() == "x = 1\n"
    assert snap.verify_unchanged() is True


@_needs_bwrap
@_needs_wheelhouse
def test_build_hook_cannot_read_a_planted_host_secret(tmp_path, enforceable,
                                                      real_wheelhouse):
    secret = tmp_path / "id_rsa"
    secret.write_text("PRIVATE-KEY-MATERIAL")
    # The hook aborts the build when it can reach the secret, so reachability
    # is carried by the EXIT STATUS. A hook that merely printed what it found
    # would be unobservable here: pip shows build output only when the build
    # fails, so a successful build reveals nothing either way.
    units, root = _malicious_pkg(
        tmp_path,
        "import pathlib\n"
        f"p = pathlib.Path({str(secret)!r})\n"
        "if p.exists():\n"
        "    raise SystemExit('SECRET-READABLE:' + p.read_text())\n")
    scratch = tmp_path / "s"
    snap = cp.materialize_snapshot(units, root, scratch)
    res = cp.build_wheel_phase(snap, scratch)
    assert "PRIVATE-KEY-MATERIAL" not in res.output_tail
    assert "SECRET-READABLE" not in res.output_tail
    assert res.status is cp.ProbeStatus.OK, (
        "the build completes because the host secret is absent inside the "
        "sandbox; a failure here means the hook reached it")


@_needs_bwrap
@_needs_wheelhouse
def test_build_hook_has_no_network(tmp_path, enforceable, real_wheelhouse):
    units, root = _malicious_pkg(
        tmp_path,
        "import socket, sys\n"
        "try:\n"
        "    socket.create_connection(('1.1.1.1', 53), timeout=3)\n"
        "    sys.stderr.write('NET-REACHED')\n"
        "except OSError:\n    sys.stderr.write('NET-BLOCKED')\n")
    scratch = tmp_path / "s"
    snap = cp.materialize_snapshot(units, root, scratch)
    res = cp.build_wheel_phase(snap, scratch)
    assert "NET-REACHED" not in res.output_tail


@_needs_bwrap
def test_memory_bomb_dies_at_the_rlimit_as_product(tmp_path, enforceable):
    """A memory bomb in producer code hits RLIMIT_AS and dies as PRODUCT
    evidence WELL under the phase deadline — the per-process ceiling, not
    the wall-clock, is what stops it (a bounded, host-safe proof of the
    per-process rlimits; unbounded fork-bomb PID capping wants a cgroup,
    tracked as a follow-up)."""
    root = _tree(tmp_path, **{"a.py": "x = 1\n"})
    scratch = tmp_path / "s"
    snap = cp.materialize_snapshot(["a.py"], root, scratch)
    start = __import__("time").monotonic()
    res = cp.run_probe_phase(
        ["/usr/bin/python3", "-c",
         "b = bytearray()\n"
         "while True:\n    b += bytearray(64 * 1024 * 1024)\n"],
        phase="test", snapshot=snap, scratch=scratch, timeout_s=120)
    assert res.status is cp.ProbeStatus.PRODUCT_FAILED
    assert res.origin == "deliverable"
    assert __import__("time").monotonic() - start < 60   # rlimit, not timeout
    assert snap.verify_unchanged() is True


@_needs_bwrap
def test_disk_bomb_is_bounded_by_fsize_rlimit(tmp_path, enforceable):
    """RLIMIT_FSIZE caps a single-file write bomb into the scratch — the
    write fails, the phase is product evidence, the host disk is safe."""
    root = _tree(tmp_path, **{"a.py": "x = 1\n"})
    scratch = tmp_path / "s"
    snap = cp.materialize_snapshot(["a.py"], root, scratch)
    res = cp.run_probe_phase(
        ["/usr/bin/python3", "-c",
         f"f = open({str(scratch)!r} + '/huge', 'wb')\n"
         "chunk = b'x' * (64 * 1024 * 1024)\n"
         "while True:\n    f.write(chunk); f.flush()\n"],
        phase="test", snapshot=snap, scratch=scratch, timeout_s=60)
    assert res.status is cp.ProbeStatus.PRODUCT_FAILED
    assert res.origin == "deliverable"


# ── layout-defect fixtures: the per-part-green / product-dead pins ──
#
# Layout/packaging defect fixtures as verbatim contracts, shaped to run
# OFFLINE anywhere (hand-built wheels; no wheelhouse needed for the shapes
# that fail before the runner). Each is a defect the swarm actually shipped
# on run 20260720T013151Z-90aa53.


def test_task_stub_pyproject_and_src_import_are_layout_facts(tmp_path):
    """Two a real-world tree defects that the DIGEST (facts, no execution) already names:
    a package dir named after a task id, and duplicate modules from a second
    project contaminating the tree. digest_hard_issues stays quiet on these
    (contamination is verifier-judged); the facts carry the evidence."""
    from modulatio import assembly

    root = _tree(tmp_path, **{
        "pyproject.toml": "[project]\nname = 'app'\n",
        "src/app/config.py": "a = 1\n",
        "src/proj_T_001/cli.py": "b = 2\n",     # task-id-named package
        "vendor/other/config.py": "c = 3\n",        # second-project dup
    })
    units = ["pyproject.toml", "src/app/config.py",
             "src/proj_T_001/cli.py", "vendor/other/config.py"]
    d = assembly.build_deliverable_digest({"units": units}, units, root,
                                          strategy="code")
    lay = d.structure["layout"]
    assert lay["task_id_names"] == ["src/proj_T_001/cli.py"]
    assert "config.py" in lay["duplicate_modules"]


@_needs_bwrap
@_needs_wheelhouse
def test_undeclared_import_fails_by_name_in_pristine_env(
        tmp_path, enforceable, real_wheelhouse):
    """The undeclared-import failure, reproduced: a module imports a dependency the package
    never declared. It installs clean (the dep isn't in the metadata) but
    import smoke in the PRISTINE env catches it — NAMED — exactly where a
    sys.path shim would have hidden it."""
    wh = tmp_path / "wh"
    whl = _mini_wheel(
        wh, name="apppkg",
        extra_modules=(("plugins", "import yaml\n"),))    # yaml never declared
    scratch = tmp_path / "s"
    root = _tree(tmp_path, **{"pyproject.toml": "[project]\nname='apppkg'\n"})
    snap = cp.materialize_snapshot(["pyproject.toml"], root, scratch)
    env, _ = cp.create_pristine_env(scratch, snapshot=snap)
    assert cp.install_wheels_phase(
        env, [whl], wheelhouse=wh, snapshot=snap,
        scratch=scratch).status is cp.ProbeStatus.OK
    res = cp.import_smoke_phase(
        env, ["apppkg", "apppkg.plugins"], snapshot=snap, scratch=scratch)
    assert res.status is cp.ProbeStatus.PRODUCT_FAILED
    assert "apppkg.plugins" in res.reason        # the undeclared-import module


def test_correct_tiny_package_passes_all_offline_shapes(tmp_path):
    """The green-path pin : a correct package's DIGEST facts are
    clean and its non-execution shapes all pass — the gate isn't just a
    rejector."""
    from modulatio import assembly

    root = _tree(tmp_path, **{
        "pyproject.toml": "[project]\nname = 'good'\nversion = '0.1.0'\n",
        "src/good/__init__.py": "def main():\n    return 0\n",
    })
    units = ["pyproject.toml", "src/good/__init__.py"]
    d = assembly.build_deliverable_digest({"units": units}, units, root,
                                          strategy="code")
    assert d.structure["packaging"] == {
        "shape": "pyproject", "root": ".", "candidates": ["."]}
    assert d.structure["layout"] == {
        "duplicate_modules": {}, "task_id_names": [], "missing_units": []}
    assert assembly.digest_hard_issues(d) == []


def test_non_python_tree_degrades_disclosed_not_false_green(tmp_path):
    """The disclosed-degrade pin: a non-Python deliverable is NOT_APPLICABLE
    with a stated reason — the verifier sees the absence, never a false
    green."""
    root = _tree(tmp_path, **{"run.sh": "echo hi\n", "README.md": "# x\n"})
    facts = cp.run_execution_probes(["run.sh", "README.md"], root,
                                    scratch_root=tmp_path / "sr")
    assert facts["status"] == "not_applicable"
    assert "no supported Python packaging" in facts["reason"]


@_needs_bwrap
@_needs_wheelhouse
def test_rollup_records_separate_product_manifest_and_runs_pip_check(
        tmp_path, enforceable, real_wheelhouse):
    """The pristine (judged) env's installed distributions are recorded
    as the product manifest BEFORE any runner byte; a pip_check phase runs on
    it. The test runner (pytest) is NOT in the product manifest — it lives in
    the disposable clone."""
    root = _tree(tmp_path, **{
        "pyproject.toml": (
            "[build-system]\nrequires = ['hatchling']\n"
            "build-backend = 'hatchling.build'\n"
            "[project]\nname = 'manifpkg'\nversion = '0.1.0'\n"),
        "manifpkg/__init__.py": "x = 1\n",
        "tests/test_ok.py": "def test_ok():\n    assert True\n",
    })
    units = ["pyproject.toml", "manifpkg/__init__.py", "tests/test_ok.py"]
    facts = cp.run_execution_probes(units, root, scratch_root=tmp_path / "sr")
    assert facts["status"] == "ok"
    phase_names = [p["phase"] for p in facts["phases"]]
    assert "pip_check" in phase_names
    manifest = facts.get("product_manifest", [])
    # Compare DISTRIBUTION NAMES (the token before @/==), not substrings — a
    # file:// wheel URL carries the scratch path, which under pytest contains
    # "pytest-of-cknox" and would false-match a naive substring check.
    names = {re.split(r"[ @=]", m.strip(), 1)[0].lower() for m in manifest}
    assert "manifpkg" in names                             # the product is in it
    assert "pytest" not in names                           # the runner is NOT
    # The runner/test env's manifest is recorded SEPARATELY and holds the
    # runner; both collections are typed phase records in the digest.
    runner = facts.get("runner_manifest", [])
    rnames = {re.split(r"[ @=]", m.strip(), 1)[0].lower() for m in runner}
    assert "pytest" in rnames
    assert "manifest" in phase_names
    assert "runner_manifest" in phase_names
