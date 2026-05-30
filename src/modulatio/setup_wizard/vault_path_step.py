# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Vault path step — picks vault root + shared resources path.

Detects ``~/Obsidian/`` presence and suggests an Obsidian-integrated
default if the user already uses Obsidian; otherwise neutral
``~/modulatio/projects/``.

Per locked principle (feedback_modulatio_no_hardcoded_paths.md): no
hardcoded paths in production code; user override always wins.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from modulatio import theme
from modulatio.setup_wizard import steps


def detect_obsidian_root() -> Path | None:
    """If ``~/Obsidian/`` exists, return the path; otherwise None."""
    candidate = Path.home() / "Obsidian"
    return candidate if candidate.exists() and candidate.is_dir() else None


def suggested_paths() -> tuple[str, str]:
    """Return (vault_root_default, shared_resources_default) for the wizard.

    If Obsidian is detected, suggests an Obsidian-integrated default.
    Otherwise neutral ``~/modulatio/`` paths.
    """
    obsidian = detect_obsidian_root()
    if obsidian:
        return (
            str(obsidian / "Modulatio" / "projects"),
            str(obsidian / "Modulatio" / "shared"),
        )
    return (
        str(Path.home() / "modulatio" / "projects"),
        str(Path.home() / "modulatio" / "shared"),
    )


def run(state: dict) -> Any:
    """Execute the vault path step. Mutates state with vault_root and
    shared_resources_path. User can override either."""
    default_vault, default_shared = suggested_paths()
    obsidian = detect_obsidian_root()

    print()
    if obsidian:
        theme.info(f"Detected Obsidian vault at {obsidian} — suggesting Obsidian-integrated paths.")
    else:
        theme.muted("No ~/Obsidian/ directory found — suggesting neutral paths.")
    print()

    # vault_root
    current = state.get("vault_root", default_vault)
    print(theme.color("  Vault root — where per-project folders live:", "primary", bold=True))
    print(steps.nav_hint())
    result = steps.prompt_nav("Vault root path", default=current, required=True)
    if result in (steps.BACK, steps.QUIT):
        return result
    state["vault_root"] = result

    # shared_resources_path
    current_shared = state.get("shared_resources_path", default_shared)
    print()
    print(theme.color("  Shared resources — where templates, skills, standards live (system-wide defaults):", "primary", bold=True))
    print(steps.nav_hint())
    result = steps.prompt_nav("Shared resources path", default=current_shared, required=True)
    if result in (steps.BACK, steps.QUIT):
        return result
    state["shared_resources_path"] = result

    return "configured"


__all__ = ["detect_obsidian_root", "suggested_paths", "run"]
