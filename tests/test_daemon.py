"""Slice 8: daemon — lifecycle + dispatch_callback assembly tests.

Doesn't actually fork/spawn the daemon (cross-platform/CI brittleness).
Verifies the building blocks: PID file management, status reporting,
callback construction, telegram-listener gating.
"""

from __future__ import annotations

import os

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


def test_make_dispatch_callback_real_mode_requires_roster_leader_model():
    """With no Leader model in the project roster (the single source of a seat's
    model), real-mode dispatch must fail loudly rather than silently downgrade."""
    cb = daemon._make_dispatch_callback(stub=False)
    with pytest.raises(RuntimeError, match="Leader model"):
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
    # The roster is the single source of every seat's model — a Leader agent with
    # a model must exist for real-mode dispatch (build_role_runners reads it).
    roster.save(
        roster.Agent(id="leader", name="Leader", tier="leader", model="L"),
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
