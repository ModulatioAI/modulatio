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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from modulatio import config

BACKUP_FORMAT_VERSION = "2.0.0"


# === Helpers ===

def _read_json_or_empty(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text()) or {}
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


def _walk_vault(vault_root: Path, code: str) -> dict[str, str]:
    """Snapshot every text file under ``<vault>/<code>/`` as a relative-path → content map.

    Skips binary files (LanceDB, .pyc, anything that fails utf-8 decode).
    Caps individual file size at 1 MB to avoid bloating the backup.
    """
    # v2 convention: vault project dirs are lowercase on disk per
    # vault.project_dir. Try as-given first (matches iterdir output), then
    # fall back to lowercase (matches CLI --code STA convention).
    project = vault_root / code
    if not project.exists():
        project = vault_root / code.lower()
    if not project.exists():
        return {}
    files: dict[str, str] = {}
    for f in project.rglob("*"):
        if not f.is_file():
            continue
        # Skip cache-like dirs
        if any(part in (".cache", "_proposals", "lance.db") for part in f.parts):
            continue
        if f.stat().st_size > 1_000_000:
            continue
        try:
            files[str(f.relative_to(project))] = f.read_text()
        except (UnicodeDecodeError, OSError):
            continue
    return files


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
    if vault_root.exists():
        codes = project_codes or _discover_project_codes(vault_root)
        for code in codes:
            vaults[code] = {"files": _walk_vault(vault_root, code)}

    vault_env = ""
    env_path = vault_root / ".env" if vault_root else None
    if not strip_secrets and env_path and env_path.exists():
        try:
            vault_env = env_path.read_text()
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
    }
    payload = json.dumps(backup, indent=2, sort_keys=True)
    if strip_secrets:
        # No tokens in the file — write at default umask, easy to email
        # or commit to a sharing repo.
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(payload)
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
        backup = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise ValueError(f"Could not parse backup: {e}")

    if backup.get("version") != BACKUP_FORMAT_VERSION:
        # Future-compat: log but don't refuse — newer formats will add a guard.
        pass

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
            target.write_text(payload)
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
            target.write_text(content)
            summary["vault_files_written"] += 1

    return summary


__all__ = ["export_backup", "import_backup", "BACKUP_FORMAT_VERSION"]
