"""CLI subcommand structure tests for the flat-schema registry.

Covers ``models`` / ``auth`` / ``doctor`` subcommands + banner injection.
The old ``providers`` subcommand group is gone; the providers/models split
collapsed into a single self-contained models registry on 2026-04-26.
"""

from __future__ import annotations

import json
import re

import pytest
from typer.testing import CliRunner

from modulatio import auth_alerts, config, model_presets
from modulatio.cli import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def isolate_config(tmp_path, monkeypatch):
    """Keep CLI subcommand writes out of the real ~/.config/modulatio/.
    Stub the noisy notification channels — without this, every test that
    sets up an active alert (for banner / doctor tests) would shell out
    to ``notify-send`` and fire real desktop pop-ups."""
    cfg_dir = tmp_path / "config"
    monkeypatch.setattr(config, "CONFIG_DIR", cfg_dir)
    monkeypatch.setattr(config, "DEFAULTS_FILE", cfg_dir / "defaults.json")
    monkeypatch.setattr(config, "AUTH_ALERTS_FILE", cfg_dir / "auth_alerts.json")
    monkeypatch.setattr(model_presets, "PRESETS_FILE", cfg_dir / "model_presets.json")
    monkeypatch.setattr(auth_alerts, "_try_desktop_notification", lambda *a, **k: None)
    monkeypatch.setattr(auth_alerts, "_try_telegram_notification", lambda *a, **k: None)
    config.reload()
    yield


# === Top-level help shows all subcommands ===

def test_top_level_help_lists_subcommands():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    out = result.stdout
    for cmd in ("kickoff", "setup", "export", "models", "auth", "doctor", "telegram", "daemon"):
        assert cmd in out, f"Top-level help missing subcommand: {cmd}"


def test_providers_subcommand_no_longer_exists():
    """The standalone providers group was collapsed into models on 2026-04-26."""
    result = runner.invoke(app, ["providers", "--help"])
    assert result.exit_code != 0


def test_version_flag_prints_modulatio_version():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "Modulatio" in result.stdout


def test_v_short_flag_prints_modulatio_version():
    result = runner.invoke(app, ["-V"])
    assert result.exit_code == 0
    assert "Modulatio" in result.stdout


# === models <list|show|add|remove|edit> ===

def test_models_list_when_empty():
    result = runner.invoke(app, ["models", "list"])
    assert result.exit_code == 0
    assert "No models" in result.stdout


def test_models_add_local_no_auth():
    result = runner.invoke(app, [
        "models", "add", "local_llama",
        "--label", "Local Llama",
        "--base-url", "http://127.0.0.1:11434/v1",
        "--api-format", "openai",
        "--auth-type", "none",
        "--model", "llama3",
    ])
    assert result.exit_code == 0
    p = model_presets.get_preset("local_llama")
    assert p["base_url"] == "http://127.0.0.1:11434/v1"
    assert p["auth_type"] == "none"
    assert p["model"] == "llama3"


def test_models_add_api_key_requires_env_var():
    result = runner.invoke(app, [
        "models", "add", "x",
        "--label", "X", "--base-url", "u",
        "--auth-type", "api_key",
        "--model", "m",
    ])
    assert result.exit_code == 1


def test_models_add_api_key_with_env_var():
    result = runner.invoke(app, [
        "models", "add", "xai_grok",
        "--label", "Grok", "--base-url", "https://api.x.ai/v1",
        "--api-format", "openai",
        "--auth-type", "api_key", "--env-var", "XAI_API_KEY",
        "--model", "grok-4-2",
    ])
    assert result.exit_code == 0
    p = model_presets.get_preset("xai_grok")
    assert p["auth_config"]["env_var"] == "XAI_API_KEY"


def test_models_add_rejects_duplicate():
    runner.invoke(app, [
        "models", "add", "k",
        "--label", "K", "--base-url", "u", "--api-format", "openai",
        "--auth-type", "none", "--model", "m",
    ])
    result = runner.invoke(app, [
        "models", "add", "k",
        "--label", "K2", "--base-url", "u", "--api-format", "openai",
        "--auth-type", "none", "--model", "m",
    ])
    assert result.exit_code == 1


def test_models_show_dumps_json():
    runner.invoke(app, [
        "models", "add", "k",
        "--label", "K", "--base-url", "u", "--api-format", "openai",
        "--auth-type", "none", "--model", "m",
    ])
    result = runner.invoke(app, ["models", "show", "k"])
    assert result.exit_code == 0
    parsed = json.loads(result.stdout)
    assert parsed["model"] == "m"


def test_models_show_unknown_key_fails():
    result = runner.invoke(app, ["models", "show", "never-existed"])
    assert result.exit_code == 1


def test_models_remove():
    runner.invoke(app, [
        "models", "add", "k",
        "--label", "K", "--base-url", "u", "--api-format", "openai",
        "--auth-type", "none", "--model", "m",
    ])
    result = runner.invoke(app, ["models", "remove", "k"])
    assert result.exit_code == 0
    assert "k" not in model_presets.load_presets()


def test_models_edit_changes_label_and_url():
    runner.invoke(app, [
        "models", "add", "k",
        "--label", "Old", "--base-url", "old-url", "--api-format", "openai",
        "--auth-type", "none", "--model", "m",
    ])
    result = runner.invoke(app, ["models", "edit", "k", "--label", "New", "--base-url", "new-url"])
    assert result.exit_code == 0
    p = model_presets.get_preset("k")
    assert p["label"] == "New"
    assert p["base_url"] == "new-url"


def test_models_edit_requires_at_least_one_field():
    runner.invoke(app, [
        "models", "add", "k",
        "--label", "K", "--base-url", "u", "--api-format", "openai",
        "--auth-type", "none", "--model", "m",
    ])
    result = runner.invoke(app, ["models", "edit", "k"])
    assert result.exit_code == 1


# === auth <list|clear|clear-all> ===

def test_auth_list_when_no_alerts():
    result = runner.invoke(app, ["auth", "list"])
    assert result.exit_code == 0
    assert "No active auth alerts" in result.stdout


def test_auth_list_shows_active_alerts():
    auth_alerts.raise_alert("p1", error_message="auth failed", auth_type="api_key")
    result = runner.invoke(app, ["auth", "list"])
    assert result.exit_code == 0
    assert "p1" in result.stdout


def test_auth_clear_removes_alert():
    auth_alerts.raise_alert("p1", error_message="x", auth_type="api_key")
    result = runner.invoke(app, ["auth", "clear", "p1"])
    assert result.exit_code == 0
    assert "p1" not in auth_alerts.load_alerts()


def test_auth_clear_all_removes_every_alert():
    auth_alerts.raise_alert("p1", error_message="x", auth_type="api_key")
    auth_alerts.raise_alert("p2", error_message="y", auth_type="api_key")
    result = runner.invoke(app, ["auth", "clear-all"])
    assert result.exit_code == 0
    assert "Cleared 2 alert" in result.stdout
    assert auth_alerts.load_alerts() == {}


# === doctor ===

def test_doctor_runs_with_no_config():
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    assert "Models (0)" in result.stdout


def test_doctor_lists_configured_models():
    runner.invoke(app, [
        "models", "add", "k",
        "--label", "K", "--base-url", "u", "--api-format", "openai",
        "--auth-type", "none", "--model", "m",
    ])
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    assert "Models (1)" in result.stdout
    assert "k" in result.stdout


def test_doctor_oauth_caveat_shown_when_oauth_model_configured():
    runner.invoke(app, [
        "models", "add", "anth",
        "--label", "A", "--base-url", "https://api.anthropic.com",
        "--api-format", "anthropic",
        "--auth-type", "oauth_openai", "--model", "claude-sonnet-4-6",
    ])
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    assert "OAuth-backed models configured" in result.stdout


def test_doctor_no_oauth_caveat_when_no_oauth_model():
    runner.invoke(app, [
        "models", "add", "k",
        "--label", "K", "--base-url", "u", "--api-format", "openai",
        "--auth-type", "none", "--model", "m",
    ])
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    assert "OAuth-backed models configured" not in result.stdout


def test_doctor_surfaces_active_alerts():
    auth_alerts.raise_alert("p1", error_message="oops", auth_type="api_key")
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    assert "Active auth alerts (1)" in result.stdout


# === Alpha (W2-lite): engine calibration banner ===


def test_doctor_surfaces_engine_calibration():
    """Alpha (W2-lite): doctor must surface what the engine is
    calibrated for — single-phase deliverables, Python full symbol map,
    other languages filename-only, multi-phase work not yet supported.
    Sets correct user expectations on first contact."""
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    assert "Engine calibration" in result.stdout
    assert "v0.1.0 Beta" in result.stdout
    # Must name what works today
    assert "Single-phase" in result.stdout
    assert "Python" in result.stdout
    # Must name the roadmap-territory limitations explicitly
    assert "Multi-phase" in result.stdout or "multi-phase" in result.stdout
    assert "v0.1.0" in result.stdout


def test_doctor_calibration_warns_about_multi_language():
    """Don't let users sleepwalk into thinking the symbol-aware
    code map covers JS/TS/Ruby — it doesn't until """
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    out = result.stdout
    # Mentions at least one of the unsupported languages by name so
    # users grep'ing for "is JS supported?" hit the right answer.
    assert any(lang in out for lang in ("JS", "TS", "Ruby", "Go", "Rust"))
    # Frames it as filename-only, not full symbol awareness.
    assert "filename" in out.lower()


# === Stubs / other subcommand groups still resolve ===

def test_setup_command_invokes_wizard():
    result = runner.invoke(app, ["setup"])
    assert result.exit_code == 1
    assert not isinstance(result.exception, NotImplementedError)


def test_export_command_active():
    result = runner.invoke(app, ["export"])
    assert result.exit_code != 0
    assert not isinstance(result.exception, NotImplementedError)


def test_export_help_documents_strip_default(tmp_path):
    """`modulatio export --help` must document that secrets are
    stripped by default (audit Wave 2, F3). The flag is
    `--include-secrets`, not `--strip` — the old `--strip` opt-in
    was the bug."""
    result = runner.invoke(app, ["export", "--help"])
    assert result.exit_code == 0
    # Under CI, Rich renders help with ANSI styling + width-wrapping, which
    # splits the literal flag string. Strip ANSI and collapse whitespace so
    # the assertion is render-independent (color/width agnostic).
    plain = re.sub(r"\x1b\[[0-9;]*m", "", result.stdout)
    compact = re.sub(r"\s+", "", plain)
    assert "--include-secrets" in compact
    # The old, dangerous-by-default `--strip` flag must be gone.
    assert "--strip" not in compact


def test_export_include_secrets_emits_warning(tmp_path, monkeypatch):
    """When the user explicitly opts into a non-share-safe backup,
    the CLI must print a stderr warning so the file isn't accidentally
    shared (audit Wave 2, F3)."""
    # Stub the actual backup write to avoid touching real config dirs;
    # this test is about the CLI surface, not the backup module.
    from modulatio import backup as backup_mod

    captured: dict = {}

    def fake_export(path, *, strip_secrets, project_codes=None):
        captured["strip_secrets"] = strip_secrets
        captured["path"] = path
        # Touch the file so the CLI's success print doesn't crash on a
        # missing path display. Real file content irrelevant for the
        # surface test.
        from pathlib import Path as _P
        _P(path).write_text("{}")
        return _P(path)

    monkeypatch.setattr(backup_mod, "export_backup", fake_export)
    out_path = tmp_path / "out.modulatio"
    result = runner.invoke(app, ["export", str(out_path), "--include-secrets"])
    assert result.exit_code == 0
    assert captured["strip_secrets"] is False
    # Warning text must appear (mixed stdout+stderr in newer
    # Click/Typer's default CliRunner — split-mode kwargs vary across
    # versions, so we assert presence in the combined output).
    combined = result.output
    assert "WARNING" in combined
    assert "include-secrets" in combined.lower()
    assert "with secrets" in combined


def test_export_default_passes_strip_true(tmp_path, monkeypatch):
    """Default invocation (no flag) must propagate strip_secrets=True
    to the backup module — the contract that makes the share-safe
    default real (audit Wave 2, F3)."""
    from modulatio import backup as backup_mod

    captured: dict = {}

    def fake_export(path, *, strip_secrets, project_codes=None):
        captured["strip_secrets"] = strip_secrets
        from pathlib import Path as _P
        _P(path).write_text("{}")
        return _P(path)

    monkeypatch.setattr(backup_mod, "export_backup", fake_export)
    out_path = tmp_path / "out.modulatio"
    result = runner.invoke(app, ["export", str(out_path)])
    assert result.exit_code == 0
    assert captured["strip_secrets"] is True


def test_telegram_command_active():
    result = runner.invoke(app, ["telegram", "--help"])
    assert result.exit_code == 0


def test_daemon_command_active():
    result = runner.invoke(app, ["daemon", "--help"])
    assert result.exit_code == 0


# === Banner injection ===

def test_no_banner_when_no_alerts():
    r2 = CliRunner()  # Click 8.2+ separates stderr by default (mix_stderr removed)
    result = r2.invoke(app, ["--help"])
    assert "AUTH ALERT" not in (result.stdout + (result.stderr or ""))


def test_banner_appears_when_alert_active():
    auth_alerts.raise_alert("p1", error_message="auth failed", auth_type="api_key")
    r2 = CliRunner()  # Click 8.2+ separates stderr by default (mix_stderr removed)
    result = r2.invoke(app, ["doctor"])
    assert result.exit_code == 0
    assert "AUTH ALERT" in (result.stderr or "")


def test_banner_suppressed_by_env_var(monkeypatch):
    auth_alerts.raise_alert("p1", error_message="x", auth_type="api_key")
    monkeypatch.setenv("MODULATIO_NO_AUTH_BANNER", "1")
    r2 = CliRunner()  # Click 8.2+ separates stderr by default (mix_stderr removed)
    result = r2.invoke(app, ["doctor"])
    assert "AUTH ALERT" not in (result.stderr or "")


def test_bare_launch_enables_splash(monkeypatch):
    """Bare ``modulatio`` (no subcommand) must launch the TUI with the Feng-Tui
    splash enabled — the fresh-install bug was the splash never appearing
    because this launch path dropped ``splash=True``."""
    import modulatio.tui.app as tui_app
    from modulatio import setup_state

    monkeypatch.setattr(setup_state, "setup_completed", lambda: True)
    monkeypatch.setattr("modulatio.cli._ensure_launch_project_code", lambda: ("TEST", False))

    captured: dict = {}

    class _FakeApp:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def run(self):
            return None

    monkeypatch.setattr(tui_app, "ModulatioApp", _FakeApp)
    monkeypatch.setattr(tui_app, "_relaunch_if_restart", lambda app: None)

    result = CliRunner().invoke(app, [])
    assert result.exit_code == 0
    assert captured.get("splash") is True
