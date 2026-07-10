"""Slice 8: daemon — lifecycle + dispatch_callback assembly tests.

Doesn't actually fork/spawn the daemon (cross-platform/CI brittleness).
Verifies the building blocks: PID file management, status reporting,
callback construction, telegram-listener gating.
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from modulatio import config, daemon, telegram_notify


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    from modulatio import vault as vault_mod
    cfg_dir = tmp_path / "config"
    monkeypatch.setattr(config, "CONFIG_DIR", cfg_dir)
    monkeypatch.setattr(config, "DEFAULTS_FILE", cfg_dir / "defaults.json")
    monkeypatch.setattr(telegram_notify, "CONFIG_FILE", cfg_dir / "telegram-config.json")
    # Defense in depth: every daemon test must redirect vault.VAULT_ROOT.
    # The `_make_dispatch_callback` callback calls `vault.init_project`
    # BEFORE raising on missing default_models, which means even error-
    # path tests (test_make_dispatch_callback_real_mode_requires_default_models)
    # silently create a real-vault project directory unless this is patched.
    # Earlier dev iterations hit exactly that bug — leaked "dtest" into
    # ~/modulatio/projects/ via the error-path test.
    monkeypatch.setattr(vault_mod, "VAULT_ROOT", tmp_path / "vault")
    config.save_defaults({"vault_root": str(tmp_path / "vault")})
    config.reload()
    yield
    # Clean up any PID file left behind
    pf = config.CONFIG_DIR / "daemon.pid"
    if pf.exists():
        try:
            pf.unlink()
        except OSError:
            pass


# === PID file / is_running ===

def test_is_running_false_when_no_pid_file():
    assert daemon.is_running() is False


def test_is_running_false_when_pid_file_points_at_dead_process(tmp_path):
    pf = daemon._pid_file()
    pf.parent.mkdir(parents=True, exist_ok=True)
    # Use a PID that's almost certainly not in use (very high)
    pf.write_text("999999")
    assert daemon.is_running() is False
    # Stale pid file should be cleaned up
    assert not pf.exists()


def test_is_running_true_when_pid_file_points_at_self():
    pf = daemon._pid_file()
    pf.parent.mkdir(parents=True, exist_ok=True)
    pf.write_text(str(os.getpid()))
    assert daemon.is_running() is True


def test_is_running_handles_malformed_pid_file():
    pf = daemon._pid_file()
    pf.parent.mkdir(parents=True, exist_ok=True)
    pf.write_text("not-a-number")
    assert daemon.is_running() is False


# === status() ===

def test_status_reports_not_running_when_no_pid():
    s = daemon.status()
    assert s["running"] is False
    assert s["pid"] is None
    assert "log" in s["log_file"]


def test_status_reports_running_when_pid_alive():
    daemon._pid_file().parent.mkdir(parents=True, exist_ok=True)
    daemon._pid_file().write_text(str(os.getpid()))
    s = daemon.status()
    assert s["running"] is True
    assert s["pid"] == os.getpid()


# === stop() — only signals when running ===

def test_stop_returns_false_when_not_running():
    assert daemon.stop() is False


# === Dispatch callback assembly ===

def test_make_dispatch_callback_stub_mode_runs_kickoff(tmp_path):
    """Stub-mode dispatch_callback should run a real (canned) Orchestrator.kickoff.

    Defends against vault-leak: snapshots the real ~/modulatio/projects/
    listing before AND after, then asserts the test didn't write a new
    project there. The autouse fixture monkeypatches vault.VAULT_ROOT
    so this test (and every other in this file) is protected from the
    real vault.
    """
    from pathlib import Path
    real_vault = Path.home() / "modulatio" / "projects"
    before = set(p.name for p in real_vault.iterdir()) if real_vault.exists() else set()

    cb = daemon._make_dispatch_callback(stub=True)
    result = cb("DTEST", "produce a one-liner artifact")
    assert "goals=" in result
    assert "tasks=" in result

    # Project should land in tmp_path/vault/dtest, NOT in the real vault.
    assert (tmp_path / "vault" / "dtest").exists(), (
        "monkeypatched VAULT_ROOT didn't take effect — project landed elsewhere"
    )
    after = set(p.name for p in real_vault.iterdir()) if real_vault.exists() else set()
    leaked = after - before
    assert "dtest" not in leaked, (
        f"BUG: test leaked project to real vault: {real_vault / 'dtest'}. "
        f"Delete it manually."
    )


def test_make_dispatch_callback_real_mode_requires_complete_roster():
    """An incomplete roster (here: empty — no Leader/QC/producer) must fail loudly
    rather than silently downgrade. A kickoff needs the full triad."""
    cb = daemon._make_dispatch_callback(stub=False)
    with pytest.raises(RuntimeError, match="roster is incomplete"):
        cb("DTEST", "anything")


# === Telegram listener gating ===

def test_telegram_listener_skipped_when_disabled():
    telegram_notify.save_config({"enabled": False, "bot_token": "x", "chat_id": "1"})
    listener = daemon._maybe_start_telegram_listener()
    assert listener is None


def test_telegram_listener_skipped_when_no_token():
    telegram_notify.save_config({"enabled": True, "bot_token": "", "chat_id": "1"})
    listener = daemon._maybe_start_telegram_listener()
    assert listener is None


def test_telegram_listener_skipped_when_no_chat_id():
    telegram_notify.save_config({"enabled": True, "bot_token": "x", "chat_id": ""})
    listener = daemon._maybe_start_telegram_listener()
    assert listener is None


def test_make_dispatch_callback_real_mode_builds_and_passes_agent_runners(
    tmp_path, monkeypatch
):
    """Routing-reality regression: the daemon (headless) path must build the
    Layer-2 per-agent model pool and pass it to the Orchestrator. Without it
    (the pre-fix state) dispatch's agent selection is cosmetic and every
    producer task collapses onto the single role-keyed runners["drafter"]
    model. FAILS on the pre-fix code (no agent_runners kwarg)."""
    from types import SimpleNamespace

    import modulatio.orchestration as orch_mod
    from modulatio import roster, runners, semantic_router, vault

    # Pre-create the project + a known custom-model producer so the dispatch
    # callback sees net_new=False (no default-roster seeding) and the pool is
    # deterministic: exactly this agent's model.
    vault.init_project("DT2", "DT2", "obj", exist_ok=True)
    roster.save(
        roster.Agent(
            id="prod",
            name="Producer",
            identity="p.",
            skills=["drafter"],
            model="custom/daemon-model",
            cost_class="paid-cloud",
        ),
        project_code="DT2",
    )
    # A real kickoff needs the full triad (Leader + QC + a producer), each with a
    # model — build_role_runners refuses an incomplete roster.
    roster.save(
        roster.Agent(id="leader", name="Leader", tier="leader", model="L"),
        project_code="DT2",
    )
    roster.save(
        roster.Agent(id="qc", name="QC", tier="qc", model="Q"),
        project_code="DT2",
    )

    # Avoid heavy embedder / real model construction.
    monkeypatch.setattr(semantic_router, "FastEmbedder", lambda *a, **k: object())
    monkeypatch.setattr(semantic_router, "default_matcher", lambda *a, **k: None)
    monkeypatch.setattr(runners, "litellm_runner", lambda m, **k: (lambda p: f"ran:{m}"))
    monkeypatch.setattr(runners, "maybe_build_chat_runner", lambda m, **k: (lambda **kw: m))

    captured: dict = {}

    class _SpyOrch:
        def __init__(self, project, runners_, **kwargs):
            captured["kwargs"] = kwargs

        def kickoff(self, *a, **k):
            return SimpleNamespace(goals=[], tasks=[], drafts=[], errors=[])

    monkeypatch.setattr(orch_mod, "Orchestrator", _SpyOrch)

    cb = daemon._make_dispatch_callback(stub=False)
    cb("DT2", "an objective")

    agent_runners = captured["kwargs"].get("agent_runners")
    assert agent_runners, (
        "daemon path passed no (or empty) agent_runners — the keystone is not "
        "wired on the headless path; producer work would collapse onto one model"
    )
    # The rostered producer's own model is in the per-agent pool.
    assert "custom/daemon-model" in agent_runners
    # ...and the tool-using producer channel (chat runners) is ALSO per-agent —
    # the primary producer path, since the skill-library builtins put every
    # producer in a tool-loop.
    chat_models = captured["kwargs"].get("chat_runner_models") or {}
    assert chat_models.get("prod") == "custom/daemon-model", (
        "daemon path did not pass per-agent chat runners — tool-using producers "
        "would collapse onto a single chat model regardless of dispatch"
    )


# ═══ daemonize / start() lifecycle regressions (audit-family fold) ═══════
# Folded from the 0.9.0-era round files (r2_audit / resweep / low_audit /
# preship). The suite's autouse `isolate` supersedes their fixtures.


DRIVER = textwrap.dedent(
    """
    import os, sys, json

    config_dir = sys.argv[1]
    result_path = sys.argv[2]

    from pathlib import Path
    from modulatio import config, daemon
    config.CONFIG_DIR = Path(config_dir)

    def _probe(*a, **k):
        # Runs inside the detached child, after the fd redirection.
        info = {}
        for fd in (0, 1, 2):
            try:
                os.fstat(fd)
                info[str(fd)] = "open"
            except OSError as e:
                info[str(fd)] = "closed:%s" % e.errno
        # Where does fd 1 actually point? Resolve via /proc.
        try:
            info["fd1_target"] = os.readlink("/proc/self/fd/1")
        except OSError:
            info["fd1_target"] = "?"
        try:
            info["fd0_target"] = os.readlink("/proc/self/fd/0")
        except OSError:
            info["fd0_target"] = "?"
        # A raw write to fd 1 must succeed (lands in the log).
        try:
            os.write(1, b"probe-fd1\\n")
            info["write_fd1"] = "ok"
        except OSError as e:
            info["write_fd1"] = "err:%s" % e.errno
        with open(result_path, "w") as fh:
            json.dump(info, fh)
        os._exit(0)

    daemon._run_daemon = _probe
    pid = daemon.start(stub=True)
    # Parent: wait for the child to finish writing the result, then exit.
    try:
        os.waitpid(pid, 0)
    except OSError:
        pass
    """
)


def _run_detach_probe(tmp_path: Path) -> dict:
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    result_path = tmp_path / "result.json"
    driver = tmp_path / "driver.py"
    driver.write_text(DRIVER)
    repo_src = str(Path(__file__).resolve().parents[1] / "src")
    env = dict(os.environ)
    env["PYTHONPATH"] = repo_src + os.pathsep + env.get("PYTHONPATH", "")
    subprocess.run(
        [sys.executable, str(driver), str(config_dir), str(result_path)],
        env=env,
        timeout=30,
        check=True,
        capture_output=True,
    )
    import json

    assert result_path.exists(), "detached child never wrote its probe result"
    return json.loads(result_path.read_text())


def test_detach_keeps_fds_012_open_and_redirected(tmp_path):
    info = _run_detach_probe(tmp_path)
    # The bug: closing the old streams freed fds 0/1/2 and left them closed.
    assert info["0"] == "open", f"fd 0 was not redirected: {info}"
    assert info["1"] == "open", f"fd 1 was not redirected: {info}"
    assert info["2"] == "open", f"fd 2 was not redirected: {info}"
    # fd 1 must point at the daemon log; fd 0 at devnull.
    assert "daemon.log" in info["fd1_target"], info
    assert "null" in info["fd0_target"], info
    # A raw fd-1 write (e.g. from a C extension) must not fail.
    assert info["write_fd1"] == "ok", info


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


# === #58: TOCTOU re-read guard ===

def test_start_survives_pid_file_vanishing_after_is_running(monkeypatch):
    """is_running() says True, but the PID file is gone by the re-read
    (concurrent stop/daemon-exit). Pre-fix: int(read_text()) raised
    FileNotFoundError. Post-fix: start() falls through and forks a fresh
    daemon (we stub fork to the parent path) instead of crashing."""
    monkeypatch.setattr(daemon, "is_running", lambda: True)
    # PID file does NOT exist -> read_text() raises FileNotFoundError(OSError).
    assert not daemon._pid_file().exists()
    # Stub the fork so we take the parent branch and never spawn a child.
    monkeypatch.setattr(os, "fork", lambda: 4321)
    monkeypatch.setattr(daemon.time, "sleep", lambda *_a, **_k: None)

    # Must NOT raise; falls through to the (stubbed) fork and returns its pid.
    pid = daemon.start(stub=True)
    assert pid == 4321


def test_start_survives_malformed_pid_file_after_is_running(monkeypatch):
    """is_running() True but the PID file is truncated/garbage at re-read
    time -> int() would raise ValueError pre-fix. Post-fix: guarded."""
    monkeypatch.setattr(daemon, "is_running", lambda: True)
    pf = daemon._pid_file()
    pf.parent.mkdir(parents=True, exist_ok=True)
    pf.write_text("not-a-pid")
    monkeypatch.setattr(os, "fork", lambda: 8765)
    monkeypatch.setattr(daemon.time, "sleep", lambda *_a, **_k: None)

    pid = daemon.start(stub=True)
    assert pid == 8765


def test_start_returns_existing_pid_when_running_and_readable(monkeypatch):
    """Happy path preserved: running daemon with a valid PID file returns
    that pid without forking."""
    monkeypatch.setattr(daemon, "is_running", lambda: True)
    pf = daemon._pid_file()
    pf.parent.mkdir(parents=True, exist_ok=True)
    pf.write_text("12345")

    def _no_fork():
        raise AssertionError("start() must NOT fork when daemon already running")

    monkeypatch.setattr(os, "fork", _no_fork)
    assert daemon.start(stub=True) == 12345


# === #59: inherited stdio streams closed in the daemon child ===

def test_child_closes_inherited_stdio_streams(tmp_path, monkeypatch):
    """Drive start()'s child branch (fork -> 0) far enough to exercise the
    redirect + close loop, then assert the inherited terminal streams were
    closed. Pre-fix they leaked open."""
    monkeypatch.setattr(daemon, "is_running", lambda: False)
    monkeypatch.setattr(os, "fork", lambda: 0)  # child branch
    monkeypatch.setattr(os, "setsid", lambda: None)
    # The child opens the log file before mkdir-ing CONFIG_DIR; in production
    # the dir already exists. Ensure it does here.
    config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    old_in = io.StringIO()
    old_out = io.StringIO()
    old_err = io.StringIO()
    monkeypatch.setattr(daemon.sys, "stdin", old_in)
    monkeypatch.setattr(daemon.sys, "stdout", old_out)
    monkeypatch.setattr(daemon.sys, "stderr", old_err)

    # Stop the child before it actually runs the daemon loop / os._exit.
    class _StopHere(Exception):
        pass

    def _boom(**_k):
        raise _StopHere()

    monkeypatch.setattr(daemon, "_run_daemon", _boom)
    monkeypatch.setattr(os, "_exit", lambda *_a: (_ for _ in ()).throw(_StopHere()))

    with pytest.raises(_StopHere):
        daemon.start(stub=True)

    assert old_in.closed, "inherited stdin was not closed (leak)"
    assert old_out.closed, "inherited stdout was not closed (leak)"
    assert old_err.closed, "inherited stderr was not closed (leak)"


# === MEDIUM: log opened before CONFIG_DIR is created ===

def test_open_log_for_append_creates_config_dir_first(tmp_path, monkeypatch):
    """On a fresh install CONFIG_DIR does not exist. _open_log_for_append must
    mkdir it BEFORE opening _log_file(), otherwise open() raises
    FileNotFoundError in the forked daemon child while the parent has already
    reported (false) success. This is the exact ordering the daemon child
    relies on, factored out so it is testable without forking/stdio hijack.

    The suite's autouse ``isolate`` creates CONFIG_DIR (save_defaults), so
    re-point at a virgin dir to restore the fresh-install precondition."""
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path / "fresh-config")
    assert not config.CONFIG_DIR.exists(), "precondition: fresh install"

    # Before the fix this raised FileNotFoundError (mkdir came after the open).
    f = daemon._open_log_for_append()
    try:
        assert config.CONFIG_DIR.exists(), "log open must create CONFIG_DIR first"
        assert daemon._log_file().exists()
        f.write("regression-marker\n")
    finally:
        f.close()

    assert "regression-marker" in daemon._log_file().read_text(encoding="utf-8")


# === LOW: _shutdown Event not cleared at _run_daemon entry ===

def test_run_daemon_clears_stale_shutdown_flag(monkeypatch):
    """A set() _shutdown from a prior run must not make a fresh _run_daemon
    exit its loop immediately. _run_daemon should clear it on entry."""
    # Simulate a leftover shutdown flag from a previous run.
    daemon._shutdown.set()
    assert daemon._shutdown.is_set()

    # Stub everything _run_daemon touches so the loop body is inert and we can
    # observe whether the flag was cleared. We capture the flag state at the
    # moment the loop's wait() is first reached, then trip it to exit.
    seen = {}

    class _FakeHB:
        def start(self):
            pass

        def stop(self):
            pass

    monkeypatch.setattr(daemon.signal, "signal", lambda *a, **k: None)
    monkeypatch.setattr(daemon.heartbeat, "Heartbeat", lambda **kw: _FakeHB())
    monkeypatch.setattr(daemon, "_make_dispatch_callback", lambda **kw: (lambda *a, **k: ""))
    monkeypatch.setattr(daemon, "_maybe_start_telegram_listener", lambda: None)
    monkeypatch.setattr(daemon.telegram_notify, "notify_event", lambda **kw: None)
    monkeypatch.setattr(daemon.cron, "dispatch_due", lambda: [])

    class _FakePE:
        def tick(self, **kw):
            return []

    monkeypatch.setattr(daemon, "_project_execution_module", lambda: _FakePE())
    monkeypatch.setattr(daemon, "_make_project_loader", lambda **kw: (lambda c: None))
    monkeypatch.setattr(daemon, "_make_runners_for", lambda **kw: (lambda p: {}))

    real_wait = daemon._shutdown.wait

    def _wait(timeout=None):
        # On the first loop iteration, record whether the stale flag survived,
        # then set it so the loop terminates on the next is_set() check.
        seen["was_set_at_loop_entry"] = daemon._shutdown.is_set()
        daemon._shutdown.set()
        return real_wait(0)

    monkeypatch.setattr(daemon._shutdown, "wait", _wait)

    daemon._run_daemon(stub=True)

    # If the clear() were missing, the while-loop guard would have seen the
    # stale set() flag and never entered the body, so _wait would not run.
    assert "was_set_at_loop_entry" in seen, "loop body never ran — stale flag not cleared"
    assert seen["was_set_at_loop_entry"] is False, "_shutdown should be cleared on entry"

    # Leave the global in a clean state for other tests.
    daemon._shutdown.clear()
