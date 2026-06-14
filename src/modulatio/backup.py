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

To produce a self-contained backup that re-imports without re-auth
(zero-setup restore), pass ``strip_secrets=False`` (or
``--include-secrets`` from the CLI). The CLI prints a visible
warning to stderr when ``--include-secrets`` is used.

Default flipped 2026-05-04 in audit-wave2 after the security audit's
review found backups defaulting to include `.env` + token while the
docs claimed exclusion. Per ``feedback_no_github_push.md`` and
``SECURITY.md``: secrets-by-default is a leak vector; explicit opt-in
is the safer contract.
"""

from __future__ import annotations

import json
import logging
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


def _is_project_dir(d: Path) -> bool:
    """True when ``d`` looks like a Modulatio project vault (has at least
    one of the seed-file markers). Excludes ancillary dirs like
    ``heartbeat-output/`` or test artifacts left under vault_root."""
    if not d.is_dir():
        return False
    return any((d / marker).exists() for marker in _PROJECT_MARKERS)


def _discover_project_codes(vault_root: Path) -> list[str]:
    """Return the names of every project-shaped directory under vault_root."""
    if not vault_root.exists():
        return []
    return sorted(
        p.name for p in vault_root.iterdir()
        if not p.name.startswith(".") and _is_project_dir(p)
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
    files: dict[str, str] = {}
    skipped: list[str] = []
    for f in project.rglob("*"):
        if not f.is_file():
            continue
        rel = str(f.relative_to(project))
        # re-sweep (F1): scope the cache-dir skip to the PROJECT tree, not
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
    include them — the resulting backup re-imports without re-auth
    but is no longer share-safe (the CLI emits a warning when this
    path is taken).

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

    vault_env = ""
    env_path = vault_root / ".env" if vault_root else None
    if not strip_secrets and env_path and env_path.exists():
        try:
            vault_env = env_path.read_text(encoding="utf-8")
        except OSError:
            vault_env = ""

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
        # Backup contains vault_env + telegram bot token + provider
        # OAuth state. write_secret_file gives 0600 throughout, no
        # world-readable window.
        config.write_secret_file(out, payload)
    return out


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
        env_path = vault_root / ".env"
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
            # re-sweep (F2): a corrupt/hand-crafted backup may carry a
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
