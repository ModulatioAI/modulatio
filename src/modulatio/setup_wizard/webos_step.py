# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Cowboy Claude and Clifton Knox.
"""WebOS check step — offer to install the opt-in ``[web]`` extra.

Mirrors ``pandoc_step``: detect → if absent, offer a one-click install or
skip. "Install now" runs the env-correct command (:mod:`modulatio.web.install`,
pipx-aware); a failure drops to the manual command with a recheck loop. Skip is
the default — the WebOS is an opt-in add-on and can always be added later
(rerun setup, or the CONFIG → SETTINGS button).
"""

from __future__ import annotations

from typing import Any

from modulatio import theme
from modulatio.setup_wizard import steps
from modulatio.web import install
from modulatio.web.install import is_installed  # re-export for the abort snapshot


def run(state: dict) -> Any:
    """Execute the WebOS step. Sets ``webos_installed`` or ``webos_skipped``."""
    if is_installed():
        theme.success("Modulatio WebOS is installed.")
        state["webos_installed"] = True
        state["webos_skipped"] = False
        try:
            input(theme.prompt_color("\n  Press Enter to continue...", "muted"))
        except (EOFError, KeyboardInterrupt):
            return steps.QUIT
        return "installed"

    print()
    print(theme.color("  Modulatio WebOS — the TUI, in your browser (optional).", "muted"))
    print(theme.color("  Adds a small server (FastAPI + uvicorn); run `modulatio-api`.", "muted"))
    print(theme.color("  You can install it now or skip and install it later.", "muted"))
    print()
    print(f"    {theme.color('a', 'highlight')}) Install now (one click; pip / pipx)")
    print(f"    {theme.color('s', 'highlight')}) Skip — install later (rerun setup, or the Settings tab)")
    print(steps.nav_hint())

    while True:
        try:
            choice = input(theme.prompt_color("\n  Choice [s]: ", "highlight")).strip().lower() or "s"
        except (EOFError, KeyboardInterrupt):
            return steps.QUIT

        if choice == "q" and not steps.quit_suppressed():
            return steps.QUIT
        if choice == "b":
            return steps.BACK
        if choice == "a":
            theme.info("Installing Modulatio WebOS...")
            ok, message = install.install()
            if ok:
                theme.success(message)
                state["webos_installed"] = True
                state["webos_skipped"] = False
                return "installed"
            theme.error(message)
            print(theme.color(f"\n  Install by hand:  {install.manual_command()}", "highlight"))
            try:
                input(theme.prompt_color(
                    "\n  After installing, press Enter to recheck (or choose s to skip)...", "muted"))
            except (EOFError, KeyboardInterrupt):
                return steps.QUIT
            if is_installed():
                theme.success("Modulatio WebOS detected. Continuing.")
                state["webos_installed"] = True
                state["webos_skipped"] = False
                return "installed"
            theme.warn("Still not detected. Skip for now or try again.")
            continue
        if choice == "s":
            theme.muted(
                "Skipped the WebOS. Modulatio runs fully in the terminal; install "
                "the WebOS later by rerunning setup or from the CONFIG → SETTINGS tab."
            )
            state["webos_installed"] = False
            state["webos_skipped"] = True
            return "skipped"
        theme.error(f"Unknown choice: {choice}")


__all__ = ["run", "is_installed"]
