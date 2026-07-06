"""The FOLDERS registry: named operator folders for job runs.

Slice 1 — registry accessors (list/save + the job-output pick).
Slice 2 — grant classification (rw vs read partition, safety floor,
reachability probe). The registry lives in defaults.json; every accessor
drops malformed entries fail-closed so a hand-edited file can't inject a
bad root downstream.
"""

from __future__ import annotations

import time

import pytest

from modulatio import config


@pytest.fixture(autouse=True)
def isolate_config(tmp_path, monkeypatch):
    """Redirect CONFIG_DIR / DEFAULTS_FILE to a tmp dir and reset the cache."""
    cfg_dir = tmp_path / "config"
    monkeypatch.setattr(config, "CONFIG_DIR", cfg_dir)
    monkeypatch.setattr(config, "DEFAULTS_FILE", cfg_dir / "defaults.json")
    config.reload()
    yield
    config.reload()


def _folder(tmp_path, name="docs", mode="ro", **over):
    d = tmp_path / name
    d.mkdir(exist_ok=True)
    rec = {"name": name, "path": str(d), "mode": mode, "kind": "path"}
    rec.update(over)
    return rec


# ── Slice 1: registry accessors ─────────────────────────────────────────────


def test_list_folders_empty_by_default():
    assert config.list_folders() == []


def test_save_and_list_folders_roundtrip(tmp_path):
    folders = [_folder(tmp_path, "docs", "ro"), _folder(tmp_path, "drop", "output")]
    config.save_folders(folders)
    assert config.list_folders() == folders


def test_list_folders_skips_malformed_and_unknown_kind(tmp_path):
    good = _folder(tmp_path, "docs", "ro")
    config.save_folders([
        good,
        {"name": "", "path": str(tmp_path), "mode": "ro", "kind": "path"},   # empty name
        {"name": "x", "path": str(tmp_path), "mode": "chaos", "kind": "path"},  # bad mode
        {"name": "y", "path": str(tmp_path), "mode": "ro", "kind": "smb"},   # future kind
        {"name": "z", "mode": "ro", "kind": "path"},                          # no path
        "not-a-dict",
    ])
    assert config.list_folders() == [good]


def test_list_folders_skips_relative_paths(tmp_path):
    config.save_folders([
        _folder(tmp_path, "docs", "ro"),
        {"name": "rel", "path": "relative/dir", "mode": "ro", "kind": "path"},
    ])
    assert [f["name"] for f in config.list_folders()] == ["docs"]


def test_job_output_folder_roundtrip_and_clear(tmp_path):
    config.save_folders([_folder(tmp_path, "drop", "output")])
    config.set_job_output_folder("drop")
    assert config.get_job_output_folder() == "drop"
    config.set_job_output_folder(None)
    assert config.get_job_output_folder() is None


def test_get_job_output_folder_none_when_name_unregistered(tmp_path):
    config.set_job_output_folder("ghost")
    assert config.get_job_output_folder() is None


def test_get_job_output_folder_none_when_mode_not_output(tmp_path):
    config.save_folders([_folder(tmp_path, "docs", "ro")])
    config.set_job_output_folder("docs")
    assert config.get_job_output_folder() is None


# ── Slice 2: grant classification + safety floor + probe ────────────────────


def test_probe_folder_true_for_dir_false_for_missing(tmp_path):
    assert config.probe_folder(str(tmp_path)) is True
    assert config.probe_folder(str(tmp_path / "gone")) is False


def test_probe_folder_times_out_instead_of_hanging(monkeypatch):
    import pathlib

    real_is_dir = pathlib.Path.is_dir

    def _slow_is_dir(self):
        time.sleep(5)
        return real_is_dir(self)

    monkeypatch.setattr(pathlib.Path, "is_dir", _slow_is_dir)
    t0 = time.monotonic()
    assert config.probe_folder("/somewhere", timeout_s=0.2) is False
    assert time.monotonic() - t0 < 2.0  # returned at the timeout, not after 5s


def test_folder_grant_roots_partitions_rw_vs_read(tmp_path):
    config.save_folders([
        _folder(tmp_path, "live", "rw"),
        _folder(tmp_path, "docs", "ro"),
        _folder(tmp_path, "drop", "output"),
    ])
    rw, read = config.folder_grant_roots()
    assert rw == (str(tmp_path / "live"),)
    assert set(read) == {str(tmp_path / "docs"), str(tmp_path / "drop")}


def test_folder_grant_roots_skips_missing_dirs(tmp_path):
    rec = _folder(tmp_path, "docs", "ro")
    config.save_folders([rec])
    (tmp_path / "docs").rmdir()
    rw, read = config.folder_grant_roots()
    assert rw == () and read == ()


def test_folder_grant_roots_refuses_dotdir_root(tmp_path):
    """Wild Bill BLOCK: a registered dot-directory (e.g. /.../.ssh) must never
    become a grant — the secret floor extends to the ROOT itself, not just
    dotfiles below it (read_file's floor only checks components below the root)."""
    d = tmp_path / ".ssh"
    d.mkdir()
    (d / "id_rsa").write_text("PRIVATE", encoding="utf-8")
    config.save_folders(
        [{"name": "ssh", "path": str(d), "mode": "ro", "kind": "path"}])
    rw, read = config.folder_grant_roots()
    assert rw == () and read == ()


def test_folder_root_refusal_is_the_shared_floor(tmp_path, monkeypatch):
    """One floor for all three sites (tab ADD, grant USE, output pick): a
    dotfile path component, a broad/system root, or a vault/delivery overlap
    is refused; a plain reachable dir passes."""
    from modulatio import vault

    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path / "vault")
    (tmp_path / ".ssh").mkdir()
    assert config.folder_root_refusal(str(tmp_path / ".ssh")) is not None
    assert config.folder_root_refusal("/etc") is not None
    ok = tmp_path / "docs"
    ok.mkdir()
    assert config.folder_root_refusal(str(ok)) is None


def test_folder_grant_roots_refuses_broad_and_vault_roots(tmp_path, monkeypatch):
    """USE-time re-validation: a hand-edited defaults.json can't inject a
    system root, $HOME itself, or a path inside the vault/delivery trees."""
    import os

    from modulatio import vault

    vroot = tmp_path / "vault"
    (vroot / "proj").mkdir(parents=True)
    monkeypatch.setattr(vault, "VAULT_ROOT", vroot)
    home = os.path.expanduser("~")
    config.save_folders([
        {"name": "etc", "path": "/etc", "mode": "rw", "kind": "path"},
        {"name": "home", "path": home, "mode": "ro", "kind": "path"},
        {"name": "invault", "path": str(vroot / "proj"), "mode": "ro", "kind": "path"},
        _folder(tmp_path, "docs", "ro"),
    ])
    rw, read = config.folder_grant_roots()
    assert rw == ()
    assert read == (str(tmp_path / "docs"),)
