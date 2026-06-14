"""Round-2 audit regressions for backup.py.

Covers two ledger findings:
  - MEDIUM: backup silently drops binary + >1MB vault files with no
    skipped-count surfaced.
  - LOW: import_backup accepts any backup version silently
    (version-mismatch was a no-op pass).
"""

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


def _seed_project(tmp_path):
    config.save_defaults({"vault_root": str(tmp_path / "vault")})
    config.reload()
    proj = tmp_path / "vault" / "STA"
    proj.mkdir(parents=True)
    (proj / "index.md").write_text("# STA\n\nObjective: do work.")
    return proj


# === MEDIUM: lossy-snapshot visibility ===

def test_walk_vault_reports_skipped_binary_file(tmp_path):
    """A binary (non-utf-8) file is skipped AND surfaced in the skip list."""
    proj = _seed_project(tmp_path)
    (proj / "art.png").write_bytes(b"\x89PNG\r\n\x1a\n\xff\xfe binary blob")
    files, skipped = backup._walk_vault(tmp_path / "vault", "STA")
    assert "index.md" in files
    assert "art.png" not in files
    assert "art.png" in skipped


def test_walk_vault_reports_skipped_oversized_file(tmp_path):
    """A file over the per-file cap is skipped AND surfaced."""
    proj = _seed_project(tmp_path)
    big = proj / "huge.txt"
    big.write_text("a" * (backup._MAX_VAULT_FILE_BYTES + 1))
    files, skipped = backup._walk_vault(tmp_path / "vault", "STA")
    assert "huge.txt" not in files
    assert "huge.txt" in skipped


def test_walk_vault_no_skips_returns_empty_list(tmp_path):
    _seed_project(tmp_path)
    files, skipped = backup._walk_vault(tmp_path / "vault", "STA")
    assert skipped == []
    assert "index.md" in files


def test_export_surfaces_skipped_count_and_warns(tmp_path, caplog):
    """export_backup records a skipped_files count in the file and logs a
    warning so the text-only lossiness is visible, not silent."""
    proj = _seed_project(tmp_path)
    (proj / "art.png").write_bytes(b"\xff\xfe\x00 binary")
    out = tmp_path / "backup.modulatio"
    with caplog.at_level("WARNING", logger="modulatio.backup"):
        backup.export_backup(out)
    data = json.loads(out.read_text())
    assert data["skipped_files"] == 1
    assert data["vaults"]["STA"]["skipped"] == ["art.png"]
    assert any("NOT captured" in r.message for r in caplog.records)


def test_export_skipped_count_zero_when_all_text(tmp_path):
    _seed_project(tmp_path)
    out = tmp_path / "backup.modulatio"
    backup.export_backup(out)
    data = json.loads(out.read_text())
    assert data["skipped_files"] == 0
    assert "skipped" not in data["vaults"]["STA"]


# === LOW: version-mismatch handling ===

def _write_backup(tmp_path, version):
    out = tmp_path / "vbackup.modulatio"
    out.write_text(json.dumps({
        "version": version,
        "exported_at": "2026-04-29T00:00:00Z",
        "defaults": {"vault_root": str(tmp_path / "vault")},
        "preferences": {},
        "telegram_config": {},
        "vault_env": None,
        "vaults": {},
    }))
    return out


def test_import_refuses_major_newer_version(tmp_path):
    """A backup whose MAJOR format version exceeds ours is refused
    fail-closed rather than silently writing config from an unknown
    schema."""
    out = _write_backup(tmp_path, "3.0.0")
    with pytest.raises(ValueError, match="newer than this Modulatio"):
        backup.import_backup(out)


def test_import_refuses_unrecognized_version(tmp_path):
    out = _write_backup(tmp_path, "not-a-version")
    with pytest.raises(ValueError, match="unrecognized format version"):
        backup.import_backup(out)


def test_import_tolerates_older_minor_version_with_warning(tmp_path, caplog):
    """A same-or-older MAJOR (e.g. an older minor) still imports, but
    warns instead of silently passing."""
    _seed_project(tmp_path)
    out = _write_backup(tmp_path, "2.0.0-rc1")  # same major, differs
    with caplog.at_level("WARNING", logger="modulatio.backup"):
        summary = backup.import_backup(out)
    assert isinstance(summary, dict)
    assert any("differs from current" in r.message for r in caplog.records)


def test_import_refuses_non_object_root(tmp_path):
    out = tmp_path / "list.modulatio"
    out.write_text(json.dumps([1, 2, 3]))
    with pytest.raises(ValueError, match="not a JSON object"):
        backup.import_backup(out)


def test_import_current_version_still_works(tmp_path):
    """Sanity: the current-version path is unaffected by the new guard."""
    out = _write_backup(tmp_path, backup.BACKUP_FORMAT_VERSION)
    summary = backup.import_backup(out)
    assert isinstance(summary, dict)
