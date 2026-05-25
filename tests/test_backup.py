"""Slice 8: backup/restore round-trip tests."""

from __future__ import annotations

import json

import pytest

from modulatio import backup, config, preferences, setup_state, telegram_notify, vault


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "config"
    monkeypatch.setattr(config, "CONFIG_DIR", cfg_dir)
    monkeypatch.setattr(config, "DEFAULTS_FILE", cfg_dir / "defaults.json")
    monkeypatch.setattr(preferences, "PREFS_FILE", cfg_dir / "preferences.json")
    monkeypatch.setattr(telegram_notify, "CONFIG_FILE", cfg_dir / "telegram-config.json")
    monkeypatch.setattr(setup_state, "SETUP_STATE_FILE", cfg_dir / "setup-state.json")
    config.save_defaults({"vault_root": str(tmp_path / "vault")})
    config.reload()
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path / "vault")
    yield


def _seed_state(tmp_path):
    """Populate config + a sample project for export."""
    config.save_defaults({
        "vault_root": str(tmp_path / "vault"),
        "default_models": {"leader": "anthropic/claude-opus-4-7"},
    })
    preferences.save_prefs({"backup_dir": "/tmp/some-backups"})
    telegram_notify.save_config({
        "enabled": True, "bot_token": "secret-token", "chat_id": "123",
    })
    # A small project with a real file
    proj = tmp_path / "vault" / "STA"
    proj.mkdir(parents=True)
    (proj / "index.md").write_text("# STA\n\nObjective: do work.")
    (proj / "agents").mkdir()
    (proj / "agents" / "leader.md").write_text("---\nname: leader\n---\n\nleader body")


def test_export_creates_file_with_all_sections(tmp_path):
    _seed_state(tmp_path)
    out = tmp_path / "backup.modulatio"
    backup.export_backup(out)
    assert out.exists()
    data = json.loads(out.read_text())
    assert data["version"] == backup.BACKUP_FORMAT_VERSION
    assert data["defaults"]["default_models"]["leader"] == "anthropic/claude-opus-4-7"
    assert data["preferences"]["backup_dir"] == "/tmp/some-backups"
    assert "STA" in data["vaults"]
    assert "index.md" in data["vaults"]["STA"]["files"]


def test_export_default_strips_secrets(tmp_path):
    """Default behavior must strip secrets — share-safe by default.

    Regression for audit Wave 2 finding F3 (Wild Bill review):
    docs claimed strip-by-default but the implementation defaulted to
    include. A user following docs could have shared a backup
    containing their .env + Telegram bot token. This test pins the
    contract: ``export_backup(path)`` with no kwargs MUST produce a
    stripped output."""
    _seed_state(tmp_path)
    env_path = tmp_path / "vault" / ".env"
    env_path.write_text("API_KEY=very-secret\n")
    out = tmp_path / "backup.modulatio"
    backup.export_backup(out)  # NO strip_secrets kwarg — pin default
    data = json.loads(out.read_text())
    assert data["stripped"] is True
    assert data["telegram_config"]["bot_token"] == ""
    assert data["vault_env"] == ""
    # And the secret bytes themselves don't leak via any other field.
    assert "very-secret" not in out.read_text()


def test_export_with_strip_omits_secrets(tmp_path):
    """Explicit ``strip_secrets=True`` matches the new default —
    test kept to lock the named-arg contract for callers that pass
    explicitly."""
    _seed_state(tmp_path)
    env_path = tmp_path / "vault" / ".env"
    env_path.write_text("API_KEY=very-secret\n")
    out = tmp_path / "backup.modulatio"
    backup.export_backup(out, strip_secrets=True)
    data = json.loads(out.read_text())
    assert data["stripped"] is True
    assert data["telegram_config"]["bot_token"] == ""
    assert data["vault_env"] == ""


def test_export_with_secrets_includes_env(tmp_path):
    """Explicit ``strip_secrets=False`` includes the .env contents.
    This is the opt-in self-contained-backup path; CLI's
    ``--include-secrets`` flag drives this and the CLI prints a
    warning when it does. Pin the underlying contract here."""
    _seed_state(tmp_path)
    env_path = tmp_path / "vault" / ".env"
    env_path.write_text("API_KEY=very-secret\n")
    out = tmp_path / "backup.modulatio"
    backup.export_backup(out, strip_secrets=False)
    data = json.loads(out.read_text())
    assert data["stripped"] is False
    assert "API_KEY=very-secret" in data["vault_env"]


def test_export_filters_by_project_code(tmp_path):
    _seed_state(tmp_path)
    # Add a second project
    (tmp_path / "vault" / "OTHER").mkdir(parents=True)
    (tmp_path / "vault" / "OTHER" / "index.md").write_text("other")
    out = tmp_path / "backup.modulatio"
    backup.export_backup(out, project_codes=["STA"])
    data = json.loads(out.read_text())
    assert "STA" in data["vaults"]
    assert "OTHER" not in data["vaults"]


def test_export_skips_non_project_dirs_under_vault_root(tmp_path):
    """Slice 8 fix: smoke test surfaced that ancillary dirs under
    vault_root (heartbeat-output/, test leftovers) were being treated as
    project vaults. Filter now requires at least one SEED_FILE marker."""
    _seed_state(tmp_path)
    # Add a non-project dir (heartbeat output, no project markers)
    (tmp_path / "vault" / "heartbeat-output").mkdir(parents=True)
    (tmp_path / "vault" / "heartbeat-output" / "task_2026.md").write_text("output")
    out = tmp_path / "backup.modulatio"
    backup.export_backup(out)
    data = json.loads(out.read_text())
    assert "STA" in data["vaults"]  # has index.md
    assert "heartbeat-output" not in data["vaults"]  # no markers


def test_discover_project_codes_only_returns_project_dirs(tmp_path):
    from modulatio.backup import _discover_project_codes
    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    # Real project — has index.md
    (vault_root / "real").mkdir()
    (vault_root / "real" / "index.md").write_text("# real")
    # Ancillary dir — no markers
    (vault_root / "heartbeat-output").mkdir()
    # Hidden dir — should always skip
    (vault_root / ".cache").mkdir()
    (vault_root / ".cache" / "index.md").write_text("hidden but markered")
    codes = _discover_project_codes(vault_root)
    assert codes == ["real"]


# === Import ===

def test_import_round_trip_restores_config(tmp_path):
    _seed_state(tmp_path)
    out = tmp_path / "backup.modulatio"
    backup.export_backup(out)

    # Wipe state — different cfg_dir
    new_cfg = tmp_path / "config2"
    config.CONFIG_DIR = new_cfg
    config.DEFAULTS_FILE = new_cfg / "defaults.json"
    preferences.PREFS_FILE = new_cfg / "preferences.json"
    telegram_notify.CONFIG_FILE = new_cfg / "telegram-config.json"
    setup_state.SETUP_STATE_FILE = new_cfg / "setup-state.json"
    config.reload()
    new_vault = tmp_path / "new-vault"
    backup.export_backup  # noqa — keep import context
    # Override vault_root in the backup's defaults so it lands in new_vault
    data = json.loads(out.read_text())
    data["defaults"]["vault_root"] = str(new_vault)
    out.write_text(json.dumps(data))

    summary = backup.import_backup(out)
    assert "defaults.json" in summary["config_files"]
    assert "preferences.json" in summary["config_files"]
    assert summary["vault_files_written"] >= 2  # index.md + leader.md at minimum

    # Verify config was restored
    config.reload()
    restored = config.get_default_models()
    assert restored.get("leader") == "anthropic/claude-opus-4-7"
    # Verify vault file made it
    assert (new_vault / "STA" / "index.md").exists()


def test_import_skips_existing_vault_files_without_overwrite(tmp_path):
    _seed_state(tmp_path)
    out = tmp_path / "backup.modulatio"
    backup.export_backup(out)
    # Pre-create a different version of the file
    target = tmp_path / "vault" / "STA" / "index.md"
    target.write_text("DO NOT OVERWRITE")
    summary = backup.import_backup(out)
    assert summary["vault_files_skipped"] >= 1
    assert "DO NOT OVERWRITE" in target.read_text()


def test_import_overwrites_with_overwrite_flag(tmp_path):
    _seed_state(tmp_path)
    out = tmp_path / "backup.modulatio"
    backup.export_backup(out)
    target = tmp_path / "vault" / "STA" / "index.md"
    target.write_text("DO NOT OVERWRITE")
    backup.import_backup(out, overwrite=True)
    # The exported version had "Objective: do work" — should be restored
    assert "Objective" in target.read_text()


def test_import_unknown_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        backup.import_backup(tmp_path / "nope.modulatio")


def test_import_malformed_file_raises(tmp_path):
    bad = tmp_path / "bad.modulatio"
    bad.write_text("{{{")
    with pytest.raises(ValueError, match="Could not parse"):
        backup.import_backup(bad)


# === SEC-005 zip-slip-equivalent path-traversal guards ===

def _make_minimal_backup(tmp_path, vaults_dict):
    """Build a minimal valid .modulatio JSON with a custom vaults section
    so we can probe the per-project / per-file path validation without
    going through export.
    """
    config.save_defaults({
        "vault_root": str(tmp_path / "vault"),
        "default_models": {"leader": "anthropic/claude-opus-4-7"},
    })
    out = tmp_path / "evil.modulatio"
    out.write_text(json.dumps({
        "version": backup.BACKUP_FORMAT_VERSION,
        "exported_at": "2026-04-29T00:00:00Z",
        "defaults": {
            "vault_root": str(tmp_path / "vault"),
            "default_models": {"leader": "anthropic/claude-opus-4-7"},
        },
        "preferences": {},
        "telegram_config": {},
        "vault_env": None,
        "vaults": vaults_dict,
    }))
    return out


def test_import_rejects_traversal_in_project_code(tmp_path):
    """SEC-005: a backup whose outer key is `../../etc` would write the
    inner files outside the vault root."""
    out = _make_minimal_backup(tmp_path, {
        "../../etc": {"files": {"index.md": "pwn"}},
    })
    with pytest.raises(ValueError, match="invalid project code"):
        backup.import_backup(out)


def test_import_rejects_traversal_in_rel_path(tmp_path):
    """SEC-005: a backup whose inner rel_path is `../../.ssh/...` would
    write outside the project dir."""
    out = _make_minimal_backup(tmp_path, {
        "good": {"files": {"../../.ssh/authorized_keys": "pwn"}},
    })
    with pytest.raises(ValueError, match="unsafe path"):
        backup.import_backup(out)


def test_import_rejects_absolute_rel_path(tmp_path):
    out = _make_minimal_backup(tmp_path, {
        "good": {"files": {"/etc/cron.d/evil": "pwn"}},
    })
    with pytest.raises(ValueError, match="unsafe path"):
        backup.import_backup(out)


def test_import_rejects_dotfile_rel_path(tmp_path):
    out = _make_minimal_backup(tmp_path, {
        "good": {"files": {".bashrc": "pwn"}},
    })
    with pytest.raises(ValueError, match="unsafe path"):
        backup.import_backup(out)


def test_import_rejects_symlink_escape(tmp_path):
    """SEC-005 belt-and-suspenders: even if rel_path passes shape
    validation, a pre-existing symlink at project_dir/<dir> pointing
    outside the project should refuse the write.
    """
    out = _make_minimal_backup(tmp_path, {
        "good": {"files": {"sub/inner.md": "content"}},
    })
    proj = tmp_path / "vault" / "good"
    proj.mkdir(parents=True)
    escape_target = tmp_path / "outside"
    escape_target.mkdir()
    (proj / "sub").symlink_to(escape_target)
    with pytest.raises(ValueError, match="resolves outside project"):
        backup.import_backup(out)
