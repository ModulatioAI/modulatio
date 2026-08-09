# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Backup + restore for Modulatio configuration.

Carried from v1.3.1 ``setup_wizard.export_backup`` (and import flow),
extracted into a standalone module so ``modulatio export`` /
``modulatio import`` CLI subcommands can use it AND the wizard's
pre-start menu can offer "Import existing config".

Backup format (``.modulatio`` file): a JSON document with these keys:

.. code-block:: json

    {
        "version": "2.0.0",
        "exported_at": "2026-04-24T12:00:00Z",
        "modulatio_version": "<importlib.metadata>",
        "defaults": { ... },               // ~/.config/modulatio/defaults.json
        "preferences": { ... },            // ~/.config/modulatio/preferences.json
        "model_presets": { ... },          // ~/.config/modulatio/model_presets.json
        "telegram_config": { ... },        // ~/.config/modulatio/telegram-config.json (when --no-strip)
        "setup_state": { ... },            // ~/.config/modulatio/setup-state.json
        "vault_env": "...",                // <vault>/.env contents (when --no-strip)
        "vaults": {
            "<code>": {                    // per-project vault snapshot
                "files": { "<rel-path>": "<file content>", ... }
            }
        }
    }

By default backups EXCLUDE secrets (.env contents, telegram bot token)
so the resulting ``.modulatio`` is share-safe — it can be emailed,
committed, or attached to a bug report without leaking credentials.
Restore is still functional; the user re-authenticates after import.

To carry the API-key store as well, pass ``strip_secrets=False`` (or
``--include-secrets`` from the CLI). The CLI prints a visible
warning to stderr when ``--include-secrets`` is used.

That covers keys, NOT provider sign-ins. OAuth credentials and the
web bearer are deliberately left behind even then: they are bound to
this install and this machine, a backup travels, and a file that
carries a live sign-in is a worse thing to email than one that
carries none. A restore therefore signs in again, and saying it
restores "without re-auth" would promise something it does not do.

Secrets-by-default is a leak vector; explicit opt-in
is the safer contract. See ``SECURITY.md``.
"""

from __future__ import annotations

import json
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from modulatio import config

logger = logging.getLogger("modulatio.backup")

BACKUP_FORMAT_VERSION = "2.0.0"

# Per-file cap for vault snapshots. Files larger than this are skipped to
# keep the backup from bloating; the skip is counted and surfaced (not
# silent). Measured in bytes — artifact-agnostic, applies to any class.
_MAX_VAULT_FILE_BYTES = 1_000_000


# === Helpers ===

def _read_json_or_empty(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace")) or {}
    except (OSError, json.JSONDecodeError):
        return {}


def _modulatio_version() -> str:
    try:
        from importlib.metadata import version as _v
        return _v("modulatio")
    except Exception:
        return "unknown"


# Files that mark a directory as a Modulatio project vault (per
# ``vault.SEED_FILES``). At least one must be present for the backup
# scanner to treat a top-level dir under vault_root as a project.
_PROJECT_MARKERS = ("index.md", "comptroller.md", "dashboard.md", "capacity.md")


def _looks_like_project(d: Path) -> bool:
    """Loose predicate for the 'back up everything' scan: ``d`` looks
    project-shaped if it has ANY seed-file marker. Deliberately MORE generous
    than the strict ``vault._is_project_dir`` (valid code + BOTH index.md AND
    comptroller.md) used for switch/delete — a partial or corrupted project
    should still be backed up even though it won't show in the switch list or
    be deletable. Excludes ancillary dirs like ``heartbeat-output/``."""
    if not d.is_dir():
        return False
    return any((d / marker).exists() for marker in _PROJECT_MARKERS)


def _discover_project_codes(vault_root: Path) -> list[str]:
    """Return the names of every project-shaped directory under vault_root.

    Uses the loose ``_looks_like_project`` (back up generously); the switch/
    delete path uses the strict ``vault._is_project_dir`` instead.
    """
    if not vault_root.exists():
        return []
    return sorted(
        p.name for p in vault_root.iterdir()
        if not p.name.startswith(".") and _looks_like_project(p)
    )


def _walk_vault(vault_root: Path, code: str) -> tuple[dict[str, str], list[str]]:
    """Snapshot every text file under ``<vault>/<code>/`` as a relative-path → content map.

    Returns ``(files, skipped)`` where ``skipped`` is the list of
    project-relative paths that were NOT captured: binary files (LanceDB,
    .pyc, anything that fails utf-8 decode), files over
    ``_MAX_VAULT_FILE_BYTES``, and unreadable files. The backup is
    text-only by design; the skip list makes the lossiness visible to the
    caller instead of dropping files silently. Cache-like dirs are
    excluded by convention and are NOT counted as skipped.
    """
    # v2 convention: vault project dirs are lowercase on disk per
    # vault.project_dir. Try as-given first (matches iterdir output), then
    # fall back to lowercase (matches CLI --code STA convention).
    project = vault_root / code
    if not project.exists():
        project = vault_root / code.lower()
    if not project.exists():
        return {}, []
    # A symlinked project ROOT would make ``project.resolve()`` the outside
    # target, so every file under it would pass the in-tree check below and
    # leak into the backup — including via a direct ``project_codes=[...]``
    # export (which bypasses the list/delete guards) and a delete TOCTOU swap.
    # Refuse to walk a symlinked root. (A symlinked VAULT ROOT with a real
    # project dir is fine — only the project dir itself being a symlink is the
    # hazard, which ``is_symlink`` on the final component detects.)
    if project.is_symlink():
        return {}, []
    files: dict[str, str] = {}
    skipped: list[str] = []
    project_real = project.resolve()
    for f in project.rglob("*"):
        if not f.is_file():
            continue
        rel = str(f.relative_to(project))
        # Symlink-closed: never read THROUGH a symlink out of the project tree
        # (a symlinked file, or a file under a symlinked subdir, would leak the
        # outside target's contents into a share-safe backup). Recorded in
        # ``skipped`` so the lossiness stays visible.
        try:
            f.resolve().relative_to(project_real)
        except ValueError:
            skipped.append(rel)
            continue
        # Scope the cache-dir skip to the PROJECT tree, not
        # the absolute path. f.parts includes every ancestor up to "/", so
        # a vault root mounted under a dir named .cache/_proposals/lance.db
        # (e.g. ~/.cache/...) would match on the ancestor and silently drop
        # every file. Compare project-relative parts, like the dotfile check
        # below already does.
        rel_parts = f.relative_to(project).parts
        if any(p in (".cache", "_proposals", "lance.db") for p in rel_parts):
            continue
        # Skip files with a dot-prefixed path component (e.g. .obsidian/*).
        # import_backup validates every captured rel_path with
        # tools._is_safe_relative_file_arg, which REJECTS any dotfile
        # component and raises — aborting the WHOLE restore. Capturing such
        # files here would make the backup un-restorable, so we exclude them
        # at export time and count them in ``skipped`` so the lossiness is
        # visible (mirrors the import-side contract; the two halves must
        # agree). Compared against the project-relative parts so dotted
        # files anywhere in the tree are caught.
        if any(part.startswith(".") for part in f.relative_to(project).parts):
            skipped.append(rel)
            continue
        try:
            too_big = f.stat().st_size > _MAX_VAULT_FILE_BYTES
        except OSError:
            skipped.append(rel)
            continue
        if too_big:
            skipped.append(rel)
            continue
        try:
            files[rel] = f.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            # Binary or unreadable — text-only backup can't carry it.
            skipped.append(rel)
            continue
    return files, skipped


# === Export ===

def export_backup(
    out_path: str | Path,
    *,
    strip_secrets: bool = True,
    project_codes: Optional[list[str]] = None,
) -> Path:
    """Write a .modulatio backup to ``out_path``.

    Includes config files + per-project vault snapshots. By default
    (``strip_secrets=True``) omits .env contents and the Telegram bot
    token so the export is share-safe. Set ``strip_secrets=False`` to
    include the API-key store — no longer share-safe (the CLI emits a
    warning when this path is taken). Provider sign-ins are left behind
    even then, so a restore signs in again.

    ``project_codes`` filters which projects are snapshotted; defaults
    to all projects under the vault root.
    """
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    cfg_dir = config.CONFIG_DIR
    defaults_data = _read_json_or_empty(config.DEFAULTS_FILE)
    prefs_data = _read_json_or_empty(cfg_dir / "preferences.json")
    presets_data = _read_json_or_empty(cfg_dir / "model_presets.json")
    setup_state_data = _read_json_or_empty(cfg_dir / "setup-state.json")
    telegram_data = _read_json_or_empty(cfg_dir / "telegram-config.json")

    if strip_secrets and telegram_data:
        telegram_data = {**telegram_data, "bot_token": ""}

    # Vault snapshots
    vault_root = Path(defaults_data.get("vault_root", "")) if defaults_data.get("vault_root") else None
    if vault_root is None:
        from modulatio import config as _cfg
        vault_root = _cfg.get_vault_root()

    vaults: dict[str, dict] = {}
    skipped_by_code: dict[str, list[str]] = {}
    if vault_root.exists():
        codes = project_codes or _discover_project_codes(vault_root)
        for code in codes:
            files, skipped = _walk_vault(vault_root, code)
            vaults[code] = {"files": files}
            if skipped:
                vaults[code]["skipped"] = skipped
                skipped_by_code[code] = skipped

    skipped_total = sum(len(s) for s in skipped_by_code.values())
    if skipped_total:
        logger.warning(
            "export_backup: %d vault file(s) NOT captured (binary or over "
            "%d bytes) — backups are text-only; these will be missing on "
            "restore: %s",
            skipped_total,
            _MAX_VAULT_FILE_BYTES,
            ", ".join(
                f"{code}/{rel}"
                for code, rels in skipped_by_code.items()
                for rel in rels
            ),
        )

    # The secret store, wherever it currently lives. Exporting only the vault
    # copy would omit every key on an install whose secrets have moved to the
    # settings directory, so a restore-with-secrets would hand back an empty
    # store and read as a working import.
    vault_env = ""
    if not strip_secrets:
        for candidate in (config.secrets_path(),
                          (vault_root / ".env") if vault_root else None):
            if candidate is None or not candidate.exists():
                continue
            try:
                vault_env = candidate.read_text(encoding="utf-8")
            except OSError:
                continue
            break

    backup = {
        "version": BACKUP_FORMAT_VERSION,
        "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "modulatio_version": _modulatio_version(),
        "stripped": strip_secrets,
        "defaults": defaults_data,
        "preferences": prefs_data,
        "model_presets": presets_data,
        "telegram_config": telegram_data,
        "setup_state": setup_state_data,
        "vault_env": vault_env,
        # Named in the file itself, so a restore that asks for a sign-in is a
        # documented outcome rather than a surprise the operator debugs.
        "omits_provider_signins": True,
        "vaults": vaults,
        # Count of vault files dropped from this (text-only) backup so the
        # lossiness is recorded durably in the file, not just at log-time.
        "skipped_files": skipped_total,
    }
    payload = json.dumps(backup, indent=2, sort_keys=True)
    if strip_secrets:
        # No tokens in the file — write at default umask, easy to email
        # or commit to a sharing repo.
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(payload, encoding="utf-8")
    else:
        # The archive carries the API-key store and the messaging token —
        # NOT provider sign-ins, which are deliberately left behind.
        # write_secret_file gives 0600 throughout, no world-readable window.
        config.write_secret_file(out, payload)
    return out


def delete_project(code: str) -> Path:
    """Remove a project's folder, backing it up first.

    Create-side sibling: ``roster.create_project``. Project lifecycle is split
    by bundled side-effect — folder in ``vault.init_project``, create+seed in
    ``roster.create_project``, backup+remove here.

    Refuses anything the public ``vault.list_projects`` doesn't list (valid
    code + seed markers, no symlinks), so a stray folder or a path outside the
    vault can never be removed. Writes a share-safe ``.modulatio`` snapshot of the
    project to the backup dir BEFORE deleting — bundling backup+remove in one
    function makes the backup un-skippable. Then drops the folder. If the
    deleted code was the recorded default, repoints the default at a remaining
    project (or clears it) so the next launch doesn't recreate an empty ghost.
    Returns the backup path.
    """
    from modulatio import preferences, vault

    target = vault.project_dir(code)  # validates + lowercases the code
    # Guard via the public enumerator, not vault's private predicate: a real
    # project is one that lists. list_projects excludes symlinked children, so
    # this also blocks a symlinked vault child.
    if target.name not in vault.list_projects():
        raise ValueError(f"not a Modulatio project: {code!r}")
    # Defense-in-depth on a destructive op: the real target must be a
    # directory directly under the real vault root — never a
    # symlink or a path escaping the vault, even if one were swapped in after
    # the marker check.
    resolved = target.resolve()
    if not resolved.is_dir() or resolved.parent != vault.VAULT_ROOT.resolve():
        raise ValueError(f"refusing to delete a path outside the vault: {code!r}")
    code = target.name  # canonical lowercased form

    backup_path = (
        Path(preferences.get_backup_dir())
        / f"{code}-before-delete-{vault.generate_run_id()}.modulatio"
    )
    export_backup(backup_path, project_codes=[code])

    shutil.rmtree(target)

    if config.get_default_project_code() == code:
        remaining = vault.list_projects()
        config.set_default_project_code(remaining[0] if remaining else "")
    return backup_path


# === Import ===

def import_backup(
    in_path: str | Path,
    *,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Restore a .modulatio backup. Returns a summary of what was restored.

    By default, refuses to overwrite existing per-project files (to
    avoid clobbering work). Pass ``overwrite=True`` to force restore.
    Always overwrites config files (defaults.json, preferences.json,
    etc.) — those are the source of the restore.
    """
    path = Path(in_path)
    if not path.exists():
        raise FileNotFoundError(f"Backup not found: {path}")
    # Refuse implausibly large backup files before reading. A malicious
    # or corrupt .modulatio can otherwise exhaust memory on json.loads
    # (which reads the entire file into a Python str + then a parsed
    # tree). Real backups are KB to single-digit MB; 100 MiB ceiling is
    # a defense against pathological inputs, not a real-world limit.
    MAX_BACKUP_BYTES = 100 * 1024 * 1024
    file_size = path.stat().st_size
    if file_size > MAX_BACKUP_BYTES:
        raise ValueError(
            f"Backup file too large: {file_size} bytes "
            f"(limit {MAX_BACKUP_BYTES}). Refusing to import; "
            f"split or regenerate the backup."
        )
    try:
        backup = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError) as e:
        raise ValueError(f"Could not parse backup: {e}")

    if not isinstance(backup, dict):
        raise ValueError(
            f"Backup root is not a JSON object (got {type(backup).__name__}); "
            f"refusing to import."
        )

    backup_version = backup.get("version")
    if backup_version != BACKUP_FORMAT_VERSION:
        # Refuse a MAJOR-version-newer backup fail-closed: its schema may
        # have moved keys we'd write to config files, and we can't safely
        # restore a format we don't understand. Same-or-older MAJOR (e.g.
        # a future minor bump) is tolerated with a warning for
        # forward/backward compat.
        def _major(v: Any) -> Optional[int]:
            try:
                return int(str(v).split(".", 1)[0])
            except (ValueError, AttributeError):
                return None

        ours = _major(BACKUP_FORMAT_VERSION)
        theirs = _major(backup_version)
        if theirs is None:
            raise ValueError(
                f"Backup has an unrecognized format version "
                f"{backup_version!r} (expected {BACKUP_FORMAT_VERSION}); "
                f"refusing to import."
            )
        if ours is not None and theirs > ours:
            raise ValueError(
                f"Backup format version {backup_version!r} is newer than "
                f"this Modulatio supports ({BACKUP_FORMAT_VERSION}); "
                f"refusing to import. Upgrade Modulatio and retry."
            )
        logger.warning(
            "import_backup: backup format version %r differs from current "
            "%s; importing on a best-effort basis.",
            backup_version,
            BACKUP_FORMAT_VERSION,
        )

    summary: dict[str, Any] = {
        "config_files": [],
        "vault_files_written": 0,
        "vault_files_skipped": 0,
    }

    cfg_dir = config.CONFIG_DIR
    cfg_dir.mkdir(parents=True, exist_ok=True)

    # Config files — always overwrite
    for key, fname in (
        ("defaults", "defaults.json"),
        ("preferences", "preferences.json"),
        ("model_presets", "model_presets.json"),
        ("telegram_config", "telegram-config.json"),
        ("setup_state", "setup-state.json"),
    ):
        data = backup.get(key)
        if not data:
            continue
        target = cfg_dir / fname
        payload = json.dumps(data, indent=2, sort_keys=True)
        if fname == "telegram-config.json":
            # Contains the bot token — 0o600 throughout, no world-readable
            # window between write and chmod.
            config.write_secret_file(target, payload)
        else:
            target.write_text(payload, encoding="utf-8")
        summary["config_files"].append(fname)

    config.reload()  # paths in defaults may have changed

    vault_root = config.get_vault_root()
    vault_root.mkdir(parents=True, exist_ok=True)

    # vault .env (with secrets) — only when present in backup
    if backup.get("vault_env"):
        # Restored into the ONE store the engine reads, whatever location the
        # backup was taken from — an older archive carries the vault copy, and
        # putting it back there would restore keys nothing looks for.
        env_path = config.secrets_path()
        if env_path.exists() and not overwrite:
            summary["vault_files_skipped"] += 1
        else:
            config.write_secret_file(env_path, backup["vault_env"])
            summary["vault_files_written"] += 1

    # Per-project vault files
    from modulatio import tools, vault as vault_module

    for code, project_data in (backup.get("vaults") or {}).items():
        # Reject path-traversal in the project code (zip-slip on the
        # outer dir name, e.g. code = "../../etc"). We validate the
        # lowercased form for shape (regex blocks `..`, `/`, etc.) but
        # preserve the original case for the path so legacy backups
        # whose dirs were created mixed-case round-trip cleanly.
        try:
            vault_module.validate_project_code(code.lower())
        except ValueError as exc:
            raise ValueError(
                f"backup contains invalid project code {code!r}: {exc}"
            ) from None
        project_dir = vault_root / code
        project_root = project_dir.resolve()
        for rel_path, content in (project_data.get("files") or {}).items():
            # Reject path-traversal in the file path (zip-slip on the
            # inner archive entries, e.g. rel_path = "../../.ssh/...").
            if not tools._is_safe_relative_file_arg(rel_path):
                raise ValueError(
                    f"backup contains unsafe path {rel_path!r} for "
                    f"project {code!r}; refusing to write."
                )
            target = project_dir / rel_path
            # Belt-and-suspenders: after the helper passes, still verify
            # that the resolved target lands inside the project dir. A
            # symlink already on disk could otherwise escape.
            try:
                target.resolve().relative_to(project_root)
            except ValueError:
                raise ValueError(
                    f"backup path {rel_path!r} resolves outside project "
                    f"{code!r}; refusing to write."
                ) from None
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists() and not overwrite:
                summary["vault_files_skipped"] += 1
                continue
            # A corrupt/hand-crafted backup may carry a
            # non-string value for a file entry (dict, number, null).
            # write_text would raise TypeError mid-restore; fail closed with
            # a clean ValueError matching the other malformed-input guards.
            if not isinstance(content, str):
                raise ValueError(
                    f"backup file {rel_path!r} for project {code!r} has "
                    f"non-text content ({type(content).__name__}); "
                    f"refusing to import."
                )
            target.write_text(content, encoding="utf-8")
            summary["vault_files_written"] += 1

    return summary


__all__ = ["export_backup", "import_backup", "BACKUP_FORMAT_VERSION"]
