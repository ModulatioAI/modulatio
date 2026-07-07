# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Pandoc check step — first wizard step.

Detects pandoc availability; if missing, presents a cross-OS install
panel (Linux apt / macOS brew / Windows choco / pandoc.org link) plus
[Try automatic install] / [I'll install manually, recheck] / [Skip] options.

Mirrors the design discussion in feedback_modulatio_pandoc_ux.md and
project_modulatio_pandoc_ux.md.
"""

from __future__ import annotations

import platform
import shutil
import subprocess
from typing import Any

from modulatio import setup_state, theme
from modulatio.setup_wizard import steps


def _print_welcome_blurb() -> None:
    """Welcome / re-config banner — folded into step 1 so the wizard has
    a single Press-Enter gate at startup instead of two."""
    if setup_state.setup_completed():
        print(f"  {theme.color('Reconfigure', 'primary', bold=True)} — pre-populated from your current setup.")
        print(theme.color("  Edit any step; press Enter at confirm to keep what's there.", "muted"))
    else:
        print(f"  {theme.color('Welcome to Modulatio', 'primary', bold=True)} — first-install setup.")
        print(theme.color("  This wizard checks pandoc, clipboard + SVG renderer, then vault path,", "muted"))
        print(theme.color("  budget, and first project, prefetches the embedded LLM, and confirms.", "muted"))
        print(theme.color("  Models + agents are configured later in the TUI Config tab. ~5 minutes.", "muted"))
    print()


# Cross-OS install commands, listed for the user to copy/paste.
INSTALL_COMMANDS: dict[str, str] = {
    "Linux (apt)": "sudo apt install pandoc",
    "Linux (dnf)": "sudo dnf install pandoc",
    "Linux (pacman)": "sudo pacman -S pandoc",
    "macOS (brew)": "brew install pandoc",
    "Windows (choco)": "choco install pandoc",
    "Windows (winget)": "winget install --id JohnMacFarlane.Pandoc",
    "All platforms": "Download installer from https://pandoc.org/installing.html",
}


def is_installed() -> bool:
    """Detect whether pandoc is reachable on PATH OR via pypandoc.

    Mirrors the detection order in ``modulatio/export.py``.
    """
    if shutil.which("pandoc"):
        return True
    try:
        import pypandoc  # noqa: F401
        return True
    except ImportError:
        return False


def render_install_panel() -> None:
    """Print the cross-OS install command panel."""
    print()
    print(theme.color("  Install pandoc on your system:", "primary", bold=True))
    print()
    width = max(len(label) for label in INSTALL_COMMANDS) + 2
    for label, cmd in INSTALL_COMMANDS.items():
        print(f"    {theme.color(label.ljust(width), 'highlight')}{cmd}")
    print()


def try_auto_install() -> bool:
    """Best-effort automatic install attempt. Detects platform + package
    manager and runs the install command. Returns True on success.

    Honors user privileges — we shell out via subprocess; sudo prompts
    appear naturally if needed. Failures fall through silently to the
    manual install panel.
    """
    system = platform.system()

    candidates: list[list[str]] = []
    if system == "Linux":
        if shutil.which("apt"):
            candidates.append(["sudo", "apt", "install", "-y", "pandoc"])
        if shutil.which("dnf"):
            candidates.append(["sudo", "dnf", "install", "-y", "pandoc"])
        if shutil.which("pacman"):
            candidates.append(["sudo", "pacman", "-S", "--noconfirm", "pandoc"])
    elif system == "Darwin":
        if shutil.which("brew"):
            candidates.append(["brew", "install", "pandoc"])
    elif system == "Windows":
        if shutil.which("choco"):
            candidates.append(["choco", "install", "-y", "pandoc"])
        if shutil.which("winget"):
            candidates.append(["winget", "install", "--id", "JohnMacFarlane.Pandoc", "--silent"])

    if not candidates:
        theme.warn(f"No supported package manager detected for {system}. Use the manual install panel.")
        return False

    for cmd in candidates:
        theme.info(f"Running: {' '.join(cmd)}")
        try:
            subprocess.run(cmd, check=True)
        except subprocess.CalledProcessError as e:
            theme.error(f"Install command failed (exit {e.returncode}). Trying next candidate or fall back to manual.")
            continue
        except FileNotFoundError:
            continue

        if is_installed():
            theme.success("pandoc installed successfully.")
            return True

    theme.error("Automatic install attempts failed. Use the manual install panel.")
    return False


def run(state: dict) -> Any:
    """Execute the pandoc check step. Mutates state with `pandoc_installed`
    flag and `pandoc_skipped` if user opted out."""
    _print_welcome_blurb()

    if is_installed():
        theme.success("pandoc is installed and reachable.")
        state["pandoc_installed"] = True
        state["pandoc_skipped"] = False
        try:
            input(theme.prompt_color("\n  Press Enter to continue...", "muted"))
        except (EOFError, KeyboardInterrupt):
            return steps.QUIT
        return "installed"

    theme.warn("pandoc is NOT installed. DOCX/PDF export will not work without it.")
    print()
    print(theme.color("  pandoc converts your project artifacts to DOCX/PDF/Markdown.", "muted"))
    print(theme.color("  You can install it now or skip and install later.", "muted"))
    print()
    print(f"    {theme.color('a', 'highlight')}) Try automatic install (best-effort, uses your system package manager)")
    print(f"    {theme.color('m', 'highlight')}) Show install commands (I'll install manually)")
    print(f"    {theme.color('s', 'highlight')}) Skip — install later")
    print(steps.nav_hint())

    while True:
        try:
            choice = input(theme.prompt_color("\n  Choice [a]: ", "highlight")).strip().lower() or "a"
        except (EOFError, KeyboardInterrupt):
            return steps.QUIT

        if choice == "q" and not steps.quit_suppressed():
            return steps.QUIT
        if choice == "b":
            return steps.BACK
        if choice == "a":
            success = try_auto_install()
            if success:
                state["pandoc_installed"] = True
                state["pandoc_skipped"] = False
                return "installed"
            # fall through to manual panel
            choice = "m"
        if choice == "m":
            render_install_panel()
            try:
                input(theme.prompt_color("\n  After running an install command, press Enter to recheck...", "muted"))
            except (EOFError, KeyboardInterrupt):
                return steps.QUIT
            if is_installed():
                theme.success("pandoc detected. Continuing.")
                state["pandoc_installed"] = True
                state["pandoc_skipped"] = False
                return "installed"
            theme.warn("Still not detected. Skip for now or try again.")
            continue
        if choice == "s":
            theme.warn(
                "Skipped pandoc. Your deliverables will stay as Markdown — "
                "without pandoc, Modulatio can't convert them to DOCX/PDF, so "
                "you'll need to handle any document-format conversions yourself "
                "(install pandoc later, or convert the Markdown by hand)."
            )
            state["pandoc_installed"] = False
            state["pandoc_skipped"] = True
            return "skipped"
        theme.error(f"Unknown choice: {choice}")


__all__ = ["is_installed", "render_install_panel", "try_auto_install", "run", "INSTALL_COMMANDS"]
