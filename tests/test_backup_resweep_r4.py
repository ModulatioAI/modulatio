# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""0.9.0 pre-ship re-sweep (round 4) regressions for modulatio.backup.

F1: _walk_vault cache-dir exclusion must scope to the PROJECT tree, not
    the absolute path — a vault root mounted under a dir named .cache/
    _proposals/lance.db must NOT cause every file to be silently dropped.
F2: import_backup must fail closed with a clean ValueError (not a raw
    TypeError) when a corrupt backup carries non-string file content.
"""

from __future__ import annotations

import json

import pytest

from modulatio import backup, config
from modulatio.backup import _walk_vault


# === F1: cache-dir exclusion is project-relative, not absolute ===

def test_walk_vault_keeps_files_when_vault_root_lives_under_cache(tmp_path):
    """A vault root whose ancestor is named `.cache` must not blank out
    the whole backup. Before the fix, `f.parts` matched the `.cache`
    ancestor for EVERY file → total silent data loss."""
    vault_root = tmp_path / ".cache" / "modulatio" / "vault"
    project = vault_root / "sta"
    project.mkdir(parents=True)
    (project / "index.md").write_text("# real content")
    (project / "notes" / "a.md").parent.mkdir()
    (project / "notes" / "a.md").write_text("more content")

    files, skipped = _walk_vault(vault_root, "sta")

    assert files.get("index.md") == "# real content"
    assert files.get("notes/a.md") == "more content"
    assert skipped == []


def test_walk_vault_still_excludes_project_relative_cache_dir(tmp_path):
    """The exclusion intent is preserved: a `.cache` dir INSIDE the
    project tree is still skipped (and, being dot-prefixed, also not
    captured)."""
    vault_root = tmp_path / "vault"
    project = vault_root / "sta"
    project.mkdir(parents=True)
    (project / "index.md").write_text("keep me")
    (project / "_proposals").mkdir()
    (project / "_proposals" / "draft.md").write_text("skip me")

    files, _ = _walk_vault(vault_root, "sta")

    assert "index.md" in files
    assert not any(rel.startswith("_proposals") for rel in files)


# === F2: non-string file content fails closed with ValueError ===

def _make_backup(tmp_path, vaults_dict):
    config.save_defaults({
        "vault_root": str(tmp_path / "vault"),
        "default_models": {"leader": "anthropic/claude-opus-4-7"},
    })
    out = tmp_path / "corrupt.modulatio"
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


@pytest.mark.parametrize("bad_content", [
    {"nested": "object"},
    12345,
    None,
    ["list", "value"],
])
def test_import_rejects_non_string_file_content(tmp_path, bad_content):
    """A hand-crafted/corrupt backup with a non-string file value must
    raise a clean ValueError (matching the other fail-closed guards),
    not a raw TypeError from write_text."""
    out = _make_backup(tmp_path, {
        "good": {"files": {"index.md": bad_content}},
    })
    with pytest.raises(ValueError, match="non-text content"):
        backup.import_backup(out)


def test_import_still_writes_string_file_content(tmp_path):
    """The guard must not regress the happy path: a normal string file
    content still imports."""
    out = _make_backup(tmp_path, {
        "good": {"files": {"index.md": "hello"}},
    })
    summary = backup.import_backup(out)
    assert summary["vault_files_written"] >= 1
    assert (tmp_path / "vault" / "good" / "index.md").read_text() == "hello"
