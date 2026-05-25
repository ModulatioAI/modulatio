"""Tests for the multi-channel auth alert system."""

from __future__ import annotations

import json
import time

import pytest

from modulatio import auth_alerts, config


@pytest.fixture(autouse=True)
def isolate_alerts(tmp_path, monkeypatch):
    cfg = tmp_path / "config"
    monkeypatch.setattr(config, "CONFIG_DIR", cfg)
    monkeypatch.setattr(config, "AUTH_ALERTS_FILE", cfg / "auth_alerts.json")


# Real implementation captured at import time so the
# disable_external_channels autouse stub can be bypassed by tests
# that need to exercise the function directly (e.g. shell-injection
# regression).
_REAL_TRY_DESKTOP_NOTIFICATION = auth_alerts._try_desktop_notification


@pytest.fixture(autouse=True)
def disable_external_channels(monkeypatch):
    """Stub the desktop-notify + telegram channels by default so the test
    suite doesn't hit real subprocess/HTTP. Tests that verify those
    specifically can override per-test."""
    monkeypatch.setattr(auth_alerts, "_try_desktop_notification", lambda *a, **k: None)
    monkeypatch.setattr(auth_alerts, "_try_telegram_notification", lambda *a, **k: None)


# === Persistence ===

def test_load_alerts_returns_empty_when_no_file():
    assert auth_alerts.load_alerts() == {}


def test_raise_alert_persists_to_disk():
    auth_alerts.raise_alert("p1", error_message="auth failed", auth_type="api_key")
    on_disk = json.loads(config.AUTH_ALERTS_FILE.read_text())
    assert "p1" in on_disk
    assert on_disk["p1"]["error_message"] == "auth failed"
    assert on_disk["p1"]["auth_type"] == "api_key"
    assert "raised_at" in on_disk["p1"]


def test_raise_alert_chmod_600():
    auth_alerts.raise_alert("p1", error_message="x", auth_type="api_key")
    mode = config.AUTH_ALERTS_FILE.stat().st_mode & 0o777
    assert mode == 0o600


def test_clear_alert_removes_entry():
    auth_alerts.raise_alert("p1", error_message="x", auth_type="api_key")
    assert auth_alerts.clear_alert("p1") is True
    assert auth_alerts.load_alerts() == {}


def test_clear_alert_returns_false_when_absent():
    assert auth_alerts.clear_alert("never-existed") is False


def test_clear_all_removes_every_alert():
    auth_alerts.raise_alert("p1", error_message="x", auth_type="api_key")
    auth_alerts.raise_alert("p2", error_message="y", auth_type="oauth_anthropic")
    n = auth_alerts.clear_all()
    assert n == 2
    assert auth_alerts.load_alerts() == {}


# === Suggested fixes ===

def test_suggested_fix_anthropic_mentions_claude_login():
    fix = auth_alerts.suggested_fix("oauth_anthropic")
    assert "claude login" in fix


def test_suggested_fix_openai_mentions_codex_login():
    fix = auth_alerts.suggested_fix("oauth_openai")
    assert "codex login" in fix


def test_suggested_fix_api_key_mentions_env_var():
    fix = auth_alerts.suggested_fix("api_key")
    assert "env var" in fix.lower()


def test_suggested_fix_none_mentions_endpoint():
    fix = auth_alerts.suggested_fix("none")
    assert "endpoint" in fix.lower() or "ollama" in fix.lower()


# === CLI banner ===

def test_render_banner_returns_none_when_no_alerts():
    assert auth_alerts.render_cli_banner() is None


def test_render_banner_includes_provider_id_and_fix():
    auth_alerts.raise_alert("p1", error_message="bad token", auth_type="oauth_anthropic")
    banner = auth_alerts.render_cli_banner()
    assert banner is not None
    assert "p1" in banner
    assert "AUTH ALERT" in banner
    assert "claude login" in banner


def test_render_banner_pluralizes_for_multiple_alerts():
    auth_alerts.raise_alert("p1", error_message="x", auth_type="api_key")
    auth_alerts.raise_alert("p2", error_message="y", auth_type="api_key")
    banner = auth_alerts.render_cli_banner()
    assert "AUTH ALERTS" in banner


def test_render_banner_suppressed_by_env_var(monkeypatch):
    auth_alerts.raise_alert("p1", error_message="x", auth_type="api_key")
    monkeypatch.setenv("MODULATIO_NO_AUTH_BANNER", "1")
    assert auth_alerts.render_cli_banner() is None


# === Rate-limit (Telegram + desktop notify gated together) ===

def test_re_raise_within_rate_limit_does_not_re_notify(monkeypatch):
    """Two raise_alert calls within TELEGRAM_RATE_LIMIT_SEC should call
    the Telegram channel only once."""
    call_count = {"n": 0}

    def fake_telegram(*a, **k):
        call_count["n"] += 1

    monkeypatch.setattr(auth_alerts, "_try_telegram_notification", fake_telegram)

    auth_alerts.raise_alert("p1", error_message="first", auth_type="api_key")
    auth_alerts.raise_alert("p1", error_message="second", auth_type="api_key")
    assert call_count["n"] == 1


def test_re_raise_after_rate_limit_re_notifies(monkeypatch):
    """Re-firing past the rate-limit window does ping again."""
    call_count = {"n": 0}

    def fake_telegram(*a, **k):
        call_count["n"] += 1

    monkeypatch.setattr(auth_alerts, "_try_telegram_notification", fake_telegram)
    auth_alerts.raise_alert("p1", error_message="first", auth_type="api_key")
    # Manually back-date the last_notified_at
    alerts = auth_alerts.load_alerts()
    alerts["p1"]["last_notified_at"] = int(time.time()) - auth_alerts.TELEGRAM_RATE_LIMIT_SEC - 1
    auth_alerts.save_alerts(alerts)
    auth_alerts.raise_alert("p1", error_message="second", auth_type="api_key")
    assert call_count["n"] == 2


# === has_active_alerts ===

def test_has_active_alerts_false_when_file_absent():
    assert auth_alerts.has_active_alerts() is False


def test_has_active_alerts_true_when_alerts_present():
    auth_alerts.raise_alert("p1", error_message="x", auth_type="api_key")
    assert auth_alerts.has_active_alerts() is True


def test_has_active_alerts_false_after_clear_all():
    auth_alerts.raise_alert("p1", error_message="x", auth_type="api_key")
    auth_alerts.clear_all()
    assert auth_alerts.has_active_alerts() is False


# === Desktop notification: shell-injection regression ===

def test_desktop_notification_windows_does_not_interpolate_provider_data(monkeypatch):
    """A malicious provider response containing PS metacharacters must
    not end up in the PowerShell command string. Provider-controlled
    title/body flow via env vars (`$env:MODULATIO_NOTIF_TITLE` /
    `$env:MODULATIO_NOTIF_BODY`); the script body itself is constant
    and contains no provider data.
    """
    monkeypatch.setattr(auth_alerts.platform, "system", lambda: "Windows")
    monkeypatch.setattr(
        auth_alerts.shutil, "which",
        lambda name: "C:\\powershell.exe" if name == "powershell" else None,
    )
    captured: dict = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        captured["env"] = kwargs.get("env", {})

    monkeypatch.setattr(auth_alerts.subprocess, "run", fake_run)

    payload = '"; Stop-Computer; #'
    _REAL_TRY_DESKTOP_NOTIFICATION("evil-provider", payload, "rotate the key")

    assert captured, "subprocess.run should have been invoked"
    # The PS script string is the 4th positional ("-Command", <ps>).
    args = captured["args"]
    assert args[0] == "powershell"
    ps_script = args[args.index("-Command") + 1]
    # Provider-controlled bytes must NOT appear anywhere in the script
    # body — they flow through env vars instead.
    assert payload not in ps_script
    assert "evil-provider" not in ps_script
    assert "Stop-Computer" not in ps_script
    # And the env did receive them.
    env = captured["env"]
    assert payload in env["MODULATIO_NOTIF_BODY"]
    assert "evil-provider" in env["MODULATIO_NOTIF_TITLE"]
