"""Regression tests for the round-2 cli.py audit fixes (2026-06-13 ledger).

Uniquely-named so concurrent per-file audit agents never collide.

Covered findings:

- kickoff ``--attach`` of a DIRECTORY (or unreadable file) now surfaces a clean
  message instead of an uncaught IsADirectoryError/PermissionError stack trace.
- ``models add --env-var`` no longer force-uppercases the env var name (POSIX
  env var names are case-sensitive — uppercasing silently mis-points a
  lowercase var).
- ``models edit`` now also catches ValueError from update_preset (a corrupt /
  hand-edited preset whose merged state fails revalidation) instead of crashing.
- ``project runs`` reads objective.md defensively (errors="replace") so a
  non-utf8 objective.md doesn't crash the listing with UnicodeDecodeError.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from modulatio import auth_alerts, config, model_presets, vault
from modulatio.cli import app

runner = CliRunner()


@pytest.fixture
def isolated(tmp_path: Path, monkeypatch):
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


# === kickoff --attach a directory → clean message, no stack trace ===

def test_kickoff_attach_directory_is_clean_error(isolated):
    a_dir = isolated / "some-folder"
    a_dir.mkdir()
    result = runner.invoke(app, [
        "kickoff",
        "--code", "PRJ",
        "--objective", "improve this",
        "--stub",
        "--attach", str(a_dir),
    ])
    assert result.exit_code != 0
    # Clean message, not a bare traceback.
    assert "--attach: cannot attach" in result.output
    assert "Traceback" not in result.output
    # No orphan project / run created by the rejected attach.
    assert not vault.project_dir("PRJ").exists()
    assert vault.list_runs("PRJ") == []


# === models add --env-var preserves case (no silent uppercasing) ===

def test_models_add_env_var_preserves_case(isolated):
    result = runner.invoke(app, [
        "models", "add", "lowerentry",
        "--label", "Lower Entry",
        "--base-url", "https://example.invalid",
        "--auth-type", "api_key",
        "--env-var", "my_lower_key",
        "--model", "some-model",
    ])
    assert result.exit_code == 0, result.output
    entry = model_presets.get_preset("lowerentry")
    assert entry is not None
    # Stored EXACTLY as passed — not "MY_LOWER_KEY".
    assert entry["auth_config"]["env_var"] == "my_lower_key"


# === models edit catches ValueError (corrupt merged state) ===

def test_models_edit_corrupt_preset_value_error_is_clean(isolated):
    # Hand-write a preset with an invalid api_format on disk (the kind of
    # corruption add_preset would reject but a hand-edit could introduce).
    config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    model_presets.PRESETS_FILE.write_text(json.dumps({
        "broken": {
            "label": "Broken",
            "base_url": "https://example.invalid",
            "api_format": "NOT_A_REAL_FORMAT",
            "auth_type": "none",
            "auth_config": {},
            "model": "m",
        }
    }))
    result = runner.invoke(app, [
        "models", "edit", "broken", "--label", "New Label",
    ])
    # update_preset re-validates the merged state → ValueError; the CLI must
    # surface it as a clean exit, not crash with a traceback.
    assert result.exit_code != 0
    assert "Traceback" not in result.output
    assert "api_format" in result.output


# === project runs reads non-utf8 objective.md without crashing ===

def test_project_runs_non_utf8_objective_does_not_crash(isolated):
    vault.init_project("PRJ", "Test", "objective", exist_ok=True)
    run_id = "20260613T100000Z-aaaa"
    vault.init_run("PRJ", run_id, "run one")
    obj = vault.run_dir("PRJ", run_id) / "objective.md"
    # Non-utf8 bytes (latin-1 'é' = 0xe9 without continuation) — read_text
    # with strict utf-8 would raise UnicodeDecodeError here.
    obj.write_bytes(b"# header\nimprove the caf\xe9 menu\n")
    result = runner.invoke(app, ["project", "runs", "--code", "PRJ"])
    assert result.exit_code == 0, result.output
    assert run_id in result.output
    assert "Traceback" not in result.output
