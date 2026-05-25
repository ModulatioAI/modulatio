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


def test_make_dispatch_callback_real_mode_requires_default_models():
    """Without default_models in defaults.json, real-mode dispatch must
    fail loudly rather than silently downgrade."""
    cb = daemon._make_dispatch_callback(stub=False)
    with pytest.raises(RuntimeError, match="defaults.json"):
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
