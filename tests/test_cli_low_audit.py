"""Regression tests for the LOW-severity cli.py audit fixes.

Covers four silent-discard / silent-destroy CLI defects:

- #48 ``cron add --jt-params`` without ``--jt`` silently discarded the params.
- #49 a failed ``kickoff --attach`` left an orphan run folder + seeded roster.
- #50 ``project clean --keep-last`` with a negative value silently deleted ALL runs.
- #51 ``models add --env-var`` was silently ignored when ``--auth-type`` != api_key.

A uniquely-named file so concurrent per-file audit agents never collide.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from modulatio import auth_alerts, config, model_presets, vault
from modulatio.cli import app

runner = CliRunner()


@pytest.fixture
def isolated(tmp_path: Path, monkeypatch):
    """Redirect config + vault to a tmp path so the test owns the universe
    and never touches the real ~/.config/modulatio or the live vault."""
    cfg = tmp_path / "config"
    monkeypatch.setattr(config, "CONFIG_DIR", cfg)
    monkeypatch.setattr(config, "DEFAULTS_FILE", cfg / "defaults.json")
    monkeypatch.setattr(config, "AUTH_ALERTS_FILE", cfg / "auth_alerts.json")
    monkeypatch.setattr(model_presets, "PRESETS_FILE", cfg / "model_presets.json")
    monkeypatch.setattr(auth_alerts, "_try_desktop_notification", lambda *a, **k: None)
    monkeypatch.setattr(auth_alerts, "_try_telegram_notification", lambda *a, **k: None)
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path / "projects")
    config.reload()
    return tmp_path


# === #48 — cron add --jt-params without --jt ===

def test_cron_add_jt_params_without_jt_is_rejected(isolated):
    result = runner.invoke(app, [
        "cron", "add",
        "--name", "nightly",
        "--schedule", "daily 09:00",
        "--code", "PRJ",
        "--objective", "do the thing",
        "--jt-params", '{"topic": "AI"}',
    ])
    assert result.exit_code != 0
    assert "--jt-params requires --jt" in result.output
    # And no cron-config was written as a side effect of the rejected add.
    assert not (isolated / "projects" / "cron-config.json").exists()


# === #49 — failed --attach must not leave disk side effects ===

def test_kickoff_bad_attach_leaves_no_run_folder_or_project(isolated):
    missing = isolated / "does-not-exist.txt"
    result = runner.invoke(app, [
        "kickoff",
        "--code", "PRJ",
        "--objective", "improve this",
        "--stub",
        "--attach", str(missing),
    ])
    assert result.exit_code != 0
    assert "file not found" in result.output
    # No net-new project / seeded roster and no orphan run folder on disk.
    assert not vault.project_dir("PRJ").exists()
    assert vault.list_runs("PRJ") == []


# === #50 — project clean --keep-last negative must not nuke everything ===

def test_project_clean_negative_keep_last_is_refused(isolated):
    vault.init_project("PRJ", "Test", "objective", exist_ok=True)
    for rid in ("20260428T100000Z-aaaa", "20260428T120000Z-bbbb"):
        vault.init_run("PRJ", rid, f"run {rid}")
    pre = vault.list_runs("PRJ")
    assert len(pre) == 2

    result = runner.invoke(app, [
        "project", "clean", "--code", "PRJ", "--keep-last", "-1", "--yes",
    ])
    assert result.exit_code != 0
    assert "cannot be negative" in result.output
    # Crucially: every run survives — nothing was deleted.
    assert vault.list_runs("PRJ") == pre


# === #51 — models add --env-var ignored for non-api_key auth ===

def test_models_add_env_var_with_non_apikey_auth_is_rejected(isolated):
    result = runner.invoke(app, [
        "models", "add", "myentry",
        "--label", "My Entry",
        "--base-url", "https://example.invalid",
        "--auth-type", "none",
        "--env-var", "MY_KEY",
        "--model", "some-model",
    ])
    assert result.exit_code != 0
    assert "--env-var only applies to --auth-type=api_key" in result.output
    # The entry was not registered despite the rejected add.
    assert model_presets.get_preset("myentry") is None


def test_models_add_none_auth_without_env_var_still_works(isolated):
    """Guard: the new branch must not break the legitimate non-api_key path."""
    result = runner.invoke(app, [
        "models", "add", "okentry",
        "--label", "OK Entry",
        "--base-url", "https://example.invalid",
        "--auth-type", "none",
        "--model", "some-model",
    ])
    assert result.exit_code == 0, result.output
    assert model_presets.get_preset("okentry") is not None
