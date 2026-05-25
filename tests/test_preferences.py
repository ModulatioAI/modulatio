"""Slice 1: preferences module tests.

Drop-in carry from v1.3.1 — backup_dir get/set with sensible default.
"""

from __future__ import annotations

import pytest

from modulatio import config, preferences


@pytest.fixture(autouse=True)
def isolate_prefs(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "config"
    monkeypatch.setattr(config, "CONFIG_DIR", cfg_dir)
    monkeypatch.setattr(preferences, "PREFS_FILE", cfg_dir / "preferences.json")
    yield


def test_load_prefs_empty_when_missing():
    assert preferences.load_prefs() == {}


def test_save_and_load_prefs_roundtrip():
    preferences.save_prefs({"backup_dir": "/tmp/backups"})
    assert preferences.load_prefs() == {"backup_dir": "/tmp/backups"}


def test_get_backup_dir_default_when_unset():
    result = preferences.get_backup_dir()
    assert "modulatio-backups" in result
    assert result.startswith("/")


def test_set_backup_dir_persists():
    preferences.set_backup_dir("/tmp/my-backups")
    assert preferences.get_backup_dir() == "/tmp/my-backups"


def test_set_backup_dir_preserves_other_prefs():
    preferences.save_prefs({"backup_dir": "/old", "other_key": "kept"})
    preferences.set_backup_dir("/new")
    loaded = preferences.load_prefs()
    assert loaded["backup_dir"] == "/new"
    assert loaded["other_key"] == "kept"


def test_malformed_prefs_falls_back_to_empty():
    preferences.PREFS_FILE.parent.mkdir(parents=True, exist_ok=True)
    preferences.PREFS_FILE.write_text("not valid {{{")
    assert preferences.load_prefs() == {}
