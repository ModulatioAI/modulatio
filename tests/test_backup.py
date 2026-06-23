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


def test_export_excludes_dotfile_dirs_and_round_trips(tmp_path):
    """Regression (pre-ship MEDIUM): a project containing dotfile content
    (e.g. .obsidian/* — the vault IS an Obsidian vault) must not break the
    round-trip. export must NOT capture dotfile components (import would
    reject them via _is_safe_relative_file_arg and abort the WHOLE restore);
    the excluded files are counted in ``skipped`` so the loss is visible.
    """
    _seed_state(tmp_path)
    proj = tmp_path / "vault" / "STA"
    (proj / ".obsidian").mkdir()
    (proj / ".obsidian" / "app.json").write_text('{"theme":"obsidian"}')
    (proj / ".obsidian" / "workspace.json").write_text("{}")

    out = tmp_path / "backup.modulatio"
    backup.export_backup(out)
    data = json.loads(out.read_text())

    # The benign content file is still captured...
    assert "index.md" in data["vaults"]["STA"]["files"]
    # ...but no .obsidian (or any dotfile) entry leaked into the archive.
    captured = data["vaults"]["STA"]["files"]
    assert not any(
        any(part.startswith(".") for part in rel.replace("\\", "/").split("/"))
        for rel in captured
    )
    # The skip is surfaced, not silent.
    skipped = data["vaults"]["STA"].get("skipped", [])
    assert any(".obsidian" in s for s in skipped)
    assert data["skipped_files"] >= 2

    # And the backup actually round-trips (would have raised before the fix).
    new_vault = tmp_path / "new-vault"
    data["defaults"]["vault_root"] = str(new_vault)
    out.write_text(json.dumps(data))
    summary = backup.import_backup(out)
    assert summary["vault_files_written"] >= 2
    assert (new_vault / "STA" / "index.md").exists()
    assert not (new_vault / "STA" / ".obsidian").exists()


# === delete_project (backup-first, marker-guarded removal) ===


def _backup_dir(tmp_path, monkeypatch):
    """Point the backup dir at a tmp location and return it."""
    d = tmp_path / "backups"
    monkeypatch.setattr(preferences, "get_backup_dir", lambda: str(d))
    return d


def test_delete_project_backs_up_then_removes(tmp_path, monkeypatch):
    """A real project is backed up to a .modulatio file BEFORE its folder
    is removed — the backup is un-skippable (one function owns both)."""
    bdir = _backup_dir(tmp_path, monkeypatch)
    root = vault.init_project("alpha", "Alpha", "do work")
    assert root.exists()

    backup_path = backup.delete_project("alpha")

    assert not root.exists(), "project folder should be gone"
    assert backup_path.exists() and backup_path.parent == bdir
    data = json.loads(backup_path.read_text())
    assert "alpha" in data["vaults"]
    assert "index.md" in data["vaults"]["alpha"]["files"]


def test_delete_project_refuses_non_project(tmp_path, monkeypatch):
    """A stray folder with a valid-looking name but no seed markers is NOT a
    project — delete_project refuses (raises) and never removes it."""
    _backup_dir(tmp_path, monkeypatch)
    stray = vault.VAULT_ROOT / "notes"
    stray.mkdir(parents=True)
    (stray / "readme.md").write_text("not a project", encoding="utf-8")

    with pytest.raises(ValueError):
        backup.delete_project("notes")
    assert stray.exists(), "a non-project must never be removed"


def test_delete_project_repoints_default(tmp_path, monkeypatch):
    """Deleting the recorded default repoints it at a remaining project so
    the next launch doesn't recreate an empty ghost of the deleted code."""
    _backup_dir(tmp_path, monkeypatch)
    vault.init_project("alpha", "Alpha", "x")
    vault.init_project("beta", "Beta", "y")
    config.set_default_project_code("alpha")

    backup.delete_project("alpha")

    assert config.get_default_project_code() == "beta"


# === symlink hardening ===


def test_export_backup_is_symlink_closed(tmp_path, monkeypatch):
    """The vault walker must not read THROUGH a symlink out of the project
    tree — a symlinked file pointing outside would otherwise leak that file's
    contents into a (share-safe by default) backup."""
    root = vault.init_project("alpha", "Alpha", "x")
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("OUTSIDE_SECRET", encoding="utf-8")
    (root / "linked.txt").symlink_to(outside)

    out = tmp_path / "b.modulatio"
    backup.export_backup(out)
    files = json.loads(out.read_text())["vaults"]["alpha"]["files"]
    assert "linked.txt" not in files
    assert "OUTSIDE_SECRET" not in json.dumps(files)


def test_delete_project_refuses_symlinked_vault_child(tmp_path, monkeypatch):
    """A symlinked vault child (outside dir with planted markers) is refused
    BEFORE any backup is written — no leak of the outside target."""
    bdir = _backup_dir(tmp_path, monkeypatch)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "index.md").write_text("x", encoding="utf-8")
    (outside / "comptroller.md").write_text("x", encoding="utf-8")
    (outside / "secret.txt").write_text("DO_NOT_BACKUP", encoding="utf-8")
    vault.VAULT_ROOT.mkdir(parents=True, exist_ok=True)
    (vault.VAULT_ROOT / "evil").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError):
        backup.delete_project("evil")
    # no backup file written at all → nothing leaked
    leaked = list(bdir.glob("*.modulatio")) if bdir.exists() else []
    assert leaked == []
    assert outside.exists()  # the outside tree is untouched


def test_export_backup_skips_symlinked_project_root(tmp_path, monkeypatch):
    """A symlinked PROJECT ROOT must not be walked: project.resolve() would
    become the outside target and every file under it would pass the in-tree
    check. Direct export with an explicit code (bypasses _is_project_dir)
    must capture nothing from the outside tree."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "index.md").write_text("x", encoding="utf-8")
    (outside / "comptroller.md").write_text("x", encoding="utf-8")
    (outside / "secret.txt").write_text("ROOT_SYMLINK_SECRET", encoding="utf-8")
    vault.VAULT_ROOT.mkdir(parents=True, exist_ok=True)
    (vault.VAULT_ROOT / "evil").symlink_to(outside, target_is_directory=True)

    out = tmp_path / "b.modulatio"
    backup.export_backup(out, project_codes=["evil"])
    data = json.loads(out.read_text())
    assert data["vaults"].get("evil", {}).get("files", {}) == {}
    assert "ROOT_SYMLINK_SECRET" not in json.dumps(data)


def test_delete_project_toctou_root_swap_does_not_leak(tmp_path, monkeypatch):
    """If the project dir is swapped to a symlink AFTER delete's guards pass
    and just before export runs, the backup must still not capture the outside
    target — the walker refuses a symlinked root."""
    bdir = _backup_dir(tmp_path, monkeypatch)
    root = vault.init_project("alpha", "Alpha", "x")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("RACE_SECRET", encoding="utf-8")

    import shutil as _sh
    real_export = backup.export_backup

    def swap_then_export(*args, **kwargs):
        _sh.rmtree(root)
        root.symlink_to(outside, target_is_directory=True)
        return real_export(*args, **kwargs)

    monkeypatch.setattr(backup, "export_backup", swap_then_export)
    with pytest.raises(OSError):
        backup.delete_project("alpha")  # rmtree on the swapped-in symlink raises
    leaked = [
        f for f in (bdir.glob("*.modulatio") if bdir.exists() else [])
        if "RACE_SECRET" in f.read_text()
    ]
    assert leaked == []


def test_export_backup_allows_symlinked_vault_root(tmp_path, monkeypatch):
    """A symlinked VAULT ROOT with a real project dir under it is legitimate
    (e.g. the vault path itself is a symlink) — it must still back up
    normally. Only the project dir ITSELF being a symlink is refused."""
    real_vault = tmp_path / "real_vault"
    real_vault.mkdir()
    link_vault = tmp_path / "link_vault"
    link_vault.symlink_to(real_vault, target_is_directory=True)
    monkeypatch.setattr(vault, "VAULT_ROOT", link_vault)
    config.save_defaults({"vault_root": str(link_vault)})
    config.reload()
    vault.init_project("alpha", "Alpha", "x")  # real dir under the symlinked root
    (vault.project_dir("alpha") / "notes.md").write_text("REAL_CONTENT", encoding="utf-8")

    out = tmp_path / "b.modulatio"
    backup.export_backup(out, project_codes=["alpha"])
    files = json.loads(out.read_text())["vaults"]["alpha"]["files"]
    assert "notes.md" in files and "REAL_CONTENT" in json.dumps(files)
