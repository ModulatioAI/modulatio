# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""SVG-renderer check step.

Visual QC review renders an SVG artifact to PNG so a vision-capable judge
sees the picture, not just the markup (``multimodal.render_svg_to_png``).
The render needs ``rsvg-convert`` (librsvg) on PATH. This step detects it
and, if missing, offers a best-effort auto-install / manual panel / skip —
the same shape as the pandoc + clipboard steps. Without it, SVG artifacts
are reviewed as text only (raster images still get visual review).
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
    "Linux (apt)": "sudo apt install librsvg2-bin",
    "Linux (dnf)": "sudo dnf install librsvg2-tools",
    "Linux (pacman)": "sudo pacman -S librsvg",
    "macOS (brew)": "brew install librsvg",
    "Windows (choco)": "choco install rsvg-convert",
}


def is_installed() -> bool:
    """True when ``rsvg-convert`` is reachable on PATH — the same probe
    ``multimodal.render_svg_to_png`` uses at review time."""
    return shutil.which("rsvg-convert") is not None


def render_install_panel() -> None:
    print()
    print(theme.color("  Install the SVG renderer (librsvg):", "primary", bold=True))
    print()
    width = max(len(label) for label in INSTALL_COMMANDS) + 2
    for label, cmd in INSTALL_COMMANDS.items():
        print(f"    {theme.color(label.ljust(width), 'highlight')}{cmd}")
    print()


def try_auto_install() -> bool:
    """Best-effort install of librsvg's ``rsvg-convert``. sudo prompts appear
    naturally. Returns True on success."""
    system = platform.system()
    candidates: list[list[str]] = []
    if system == "Linux":
        if shutil.which("apt"):
            candidates.append(["sudo", "apt", "install", "-y", "librsvg2-bin"])
        if shutil.which("dnf"):
            candidates.append(["sudo", "dnf", "install", "-y", "librsvg2-tools"])
        if shutil.which("pacman"):
            candidates.append(["sudo", "pacman", "-S", "--noconfirm", "librsvg"])
    elif system == "Darwin":
        if shutil.which("brew"):
            candidates.append(["brew", "install", "librsvg"])
    elif system == "Windows":
        if shutil.which("choco"):
            candidates.append(["choco", "install", "-y", "rsvg-convert"])
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
            theme.success("SVG renderer installed successfully.")
            return True
    theme.error("Automatic install attempts failed. Use the manual install panel.")
    return False


def run(state: dict) -> Any:
    """Detect the SVG renderer; if missing, offer install/skip."""
    if is_installed():
        theme.success("SVG renderer is available — a vision-capable QC seat "
                      "reviews SVG artifacts as rendered images.")
        state["svg_renderer_installed"] = True
        state["svg_renderer_skipped"] = False
        try:
            input(theme.prompt_color("\n  Press Enter to continue...", "muted"))
        except (EOFError, KeyboardInterrupt):
            return steps.QUIT
        return "installed"

    theme.warn("No SVG renderer found (rsvg-convert). Vision-capable QC seats "
               "review raster images either way, but SVG artifacts stay "
               "text-only reviews without it.")
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
                state["svg_renderer_installed"] = True
                state["svg_renderer_skipped"] = False
                return "installed"
            choice = "m"  # fall through to manual panel
        if choice == "m":
            render_install_panel()
            try:
                input(theme.prompt_color("\n  After running an install command, press Enter to recheck...", "muted"))
            except (EOFError, KeyboardInterrupt):
                return steps.QUIT
            if is_installed():
                theme.success("SVG renderer detected. Continuing.")
                state["svg_renderer_installed"] = True
                state["svg_renderer_skipped"] = False
                return "installed"
            theme.warn("Still not detected. Skip for now or try again.")
            continue
        if choice == "s":
            theme.warn(
                "Skipped the SVG renderer. Visual QC still reviews raster "
                "images; SVG artifacts are reviewed as markup text until "
                "rsvg-convert is installed."
            )
            state["svg_renderer_installed"] = False
            state["svg_renderer_skipped"] = True
            return "skipped"
        theme.error(f"Unknown choice: {choice}")


__all__ = ["is_installed", "render_install_panel", "try_auto_install", "run", "INSTALL_COMMANDS"]
