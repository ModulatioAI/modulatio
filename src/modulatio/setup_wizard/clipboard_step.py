# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Clipboard-backend check step.

The TUI's copy/paste (Ctrl+C / Ctrl+V) reaches the OS clipboard via pyperclip,
which on Linux needs a system backend (``xclip`` / ``wl-clipboard``). macOS and
Windows have a native backend. This step detects it and, if missing on Linux,
offers a best-effort auto-install / manual panel / skip — the same shape as the
pandoc step. Without a backend the TUI still copies via OSC 52 (terminal-
dependent); paste needs the backend.
"""
from __future__ import annotations

import platform
import shutil
import subprocess
from typing import Any

from modulatio import theme
from modulatio.setup_wizard import steps

# Cross-OS install commands, listed for the user to copy/paste.
INSTALL_COMMANDS: dict[str, str] = {
    "Linux (apt)": "sudo apt install xclip",
    "Linux (dnf)": "sudo dnf install xclip",
    "Linux (pacman)": "sudo pacman -S xclip",
    "Wayland (apt)": "sudo apt install wl-clipboard",
    "Wayland (dnf)": "sudo dnf install wl-clipboard",
}


def is_installed() -> bool:
    """True if the OS clipboard is usable — a Linux backend on PATH, or a native
    backend on macOS/Windows."""
    from modulatio import clipboard
    return clipboard.is_backend_installed()


def render_install_panel() -> None:
    print()
    print(theme.color("  Install a clipboard backend (Linux):", "primary", bold=True))
    print()
    width = max(len(label) for label in INSTALL_COMMANDS) + 2
    for label, cmd in INSTALL_COMMANDS.items():
        print(f"    {theme.color(label.ljust(width), 'highlight')}{cmd}")
    print()


def try_auto_install() -> bool:
    """Best-effort install of a clipboard backend (Linux only — macOS/Windows
    are native). sudo prompts appear naturally. Returns True on success."""
    system = platform.system()
    candidates: list[list[str]] = []
    if system == "Linux":
        if shutil.which("apt"):
            candidates.append(["sudo", "apt", "install", "-y", "xclip"])
        if shutil.which("dnf"):
            candidates.append(["sudo", "dnf", "install", "-y", "xclip"])
        if shutil.which("pacman"):
            candidates.append(["sudo", "pacman", "-S", "--noconfirm", "xclip"])
    if not candidates:
        theme.warn(
            f"No supported package manager detected for {system}. "
            "Use the manual install panel.")
        return False
    for cmd in candidates:
        theme.info(f"Running: {' '.join(cmd)}")
        try:
            subprocess.run(cmd, check=True)
        except subprocess.CalledProcessError as e:
            theme.error(
                f"Install command failed (exit {e.returncode}). "
                "Trying next candidate or fall back to manual.")
            continue
        except FileNotFoundError:
            continue
        if is_installed():
            theme.success("Clipboard backend installed successfully.")
            return True
    theme.error("Automatic install attempts failed. Use the manual install panel.")
    return False


def run(state: dict) -> Any:
    """Detect the clipboard backend; if missing on Linux, offer install/skip."""
    if is_installed():
        theme.success("Clipboard backend is available — TUI copy/paste will use "
                      "the OS clipboard.")
        state["clipboard_backend_installed"] = True
        state["clipboard_skipped"] = False
        try:
            input(theme.prompt_color("\n  Press Enter to continue...", "muted"))
        except (EOFError, KeyboardInterrupt):
            return steps.QUIT
        return "installed"

    theme.warn("No clipboard backend found (xclip / wl-clipboard). The TUI's "
               "Ctrl+C still copies via OSC 52 (terminal-dependent), but Ctrl+V "
               "paste from the OS clipboard needs a backend.")
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
            if try_auto_install():
                state["clipboard_backend_installed"] = True
                state["clipboard_skipped"] = False
                return "installed"
            choice = "m"  # fall through to manual panel
        if choice == "m":
            render_install_panel()
            try:
                input(theme.prompt_color("\n  After running an install command, press Enter to recheck...", "muted"))
            except (EOFError, KeyboardInterrupt):
                return steps.QUIT
            if is_installed():
                theme.success("Clipboard backend detected. Continuing.")
                state["clipboard_backend_installed"] = True
                state["clipboard_skipped"] = False
                return "installed"
            theme.warn("Still not detected. Skip for now or try again.")
            continue
        if choice == "s":
            theme.muted("Skipped. OS-clipboard paste will be unavailable until a "
                        "backend is installed (copy still uses OSC 52).")
            state["clipboard_backend_installed"] = False
            state["clipboard_skipped"] = True
            return "skipped"
        theme.error(f"Unknown choice: {choice}")


__all__ = ["is_installed", "render_install_panel", "try_auto_install", "run",
           "INSTALL_COMMANDS"]
