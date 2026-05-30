# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""First project step — capture project code + objective for auto-launch.

After ``finalize.commit()``, the wizard initializes this project's vault,
seeds the roster from the wizard team template, and execs into the TUI
on it. Lets first-run users land on a usable screen instead of a shell
prompt.
"""

from __future__ import annotations

from typing import Any

from modulatio import theme, vault
from modulatio.setup_wizard import steps


def _validate_code(code: str) -> str | None:
    """Return error string if invalid, else None.

    Wraps ``vault.validate_project_code`` for the wizard's UI flow,
    which prefers an error string over an exception.
    """
    try:
        vault.validate_project_code(code)
    except ValueError:
        return (
            "Code must start with a lowercase letter, contain only "
            "lowercase letters / digits / underscores, and be 1–32 chars."
        )
    return None


def run(state: dict) -> Any:
    """Capture project code + objective. Mutates state with
    ``first_project_code`` + ``first_project_objective``."""
    print()
    print(theme.color("  Your first Modulatio project — what do you want to ship?", "primary", bold=True))
    print(theme.color("  Pick a short code (used as the project's vault dir name).", "muted"))
    print(theme.color("  Setup ends by launching the TUI on this project.", "muted"))
    print()

    current_code = state.get("first_project_code") or "modulatio1"
    while True:
        result = steps.prompt_nav(
            "Project code (e.g. ckb, my_book, q3_marketing)",
            default=current_code,
            required=True,
        )
        if result in (steps.BACK, steps.QUIT):
            return result
        err = _validate_code(result)
        if err:
            theme.error(err)
            continue
        code = result
        break

    current_obj = state.get("first_project_objective") or "Get started with Modulatio."
    obj = steps.prompt_nav(
        "Project objective (one sentence)",
        default=current_obj,
        required=True,
    )
    if obj in (steps.BACK, steps.QUIT):
        return obj

    state["first_project_code"] = code
    state["first_project_objective"] = obj
    return "captured"


__all__ = ["run", "_validate_code"]
