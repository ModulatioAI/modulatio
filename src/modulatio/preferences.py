# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""User preferences — small JSON file outside the install dir so it survives
reinstalls. Currently just stores backup_dir; extend as needed.

Paths use ``config.CONFIG_DIR`` for consistency with the rest of the app.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from modulatio import config

PREFS_FILE = config.CONFIG_DIR / "preferences.json"
DEFAULT_BACKUP_DIR = str(Path.home() / "modulatio-backups")


def load_prefs() -> dict:
    """Read preferences.json. Empty dict if missing or malformed."""
    if not PREFS_FILE.exists():
        return {}
    try:
        return json.loads(PREFS_FILE.read_text(encoding="utf-8", errors="replace")) or {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_prefs(prefs: dict) -> None:
    """Persist preferences to disk."""
    config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    PREFS_FILE.write_text(json.dumps(prefs, indent=2), encoding="utf-8")


def get_backup_dir() -> str:
    """Where ``modulatio export`` backups land. Defaults to ~/modulatio-backups."""
    prefs = load_prefs()
    return os.path.expanduser(prefs.get("backup_dir") or DEFAULT_BACKUP_DIR)


def set_backup_dir(path: str) -> None:
    prefs = load_prefs()
    prefs["backup_dir"] = path
    save_prefs(prefs)


def get_theme(default: str = "feng-amber") -> str:
    """The Feng-Tui variant the TUI last used, so it reloads where you left it.

    Returns the stored variant name (e.g. ``feng-green``) or ``default`` if none
    is saved. The caller validates membership against the registered variants —
    a stale/garbage value should fall back, never crash boot."""
    prefs = load_prefs()
    val = prefs.get("theme")
    return val if isinstance(val, str) and val else default


def set_theme(name: str) -> None:
    """Remember the active Feng-Tui variant for the next launch."""
    prefs = load_prefs()
    prefs["theme"] = name
    save_prefs(prefs)
