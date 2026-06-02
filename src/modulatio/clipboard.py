# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""OS clipboard access for the TUI — read/write the system clipboard.

Backed by ``pyperclip`` (a dependency, so a clean install has it): native on
macOS/Windows, and on Linux it drives ``xclip`` / ``xsel`` / ``wl-clipboard`` —
the system backend the setup wizard ensures (``clipboard_step``). This is the
reliable path to the *OS* clipboard, far better than the terminal's OSC 52
escape, which depends on the terminal honoring clipboard writes.

Everything degrades gracefully: ``copy`` returns False and ``paste`` returns None
when no backend resolves (e.g. Linux pre-setup), so callers can fall back to OSC
52 (copy) or simply no-op (paste) — never a crash.
"""
from __future__ import annotations

import shutil

#: Linux clipboard backends pyperclip can drive (used for detection / the
#: doctor + wizard hints). macOS/Windows have a native backend.
_LINUX_BACKENDS = ("xclip", "xsel", "wl-copy", "pbcopy")


def copy(text: str) -> bool:
    """Write ``text`` to the OS clipboard. Returns True on success, False if no
    backend resolves."""
    if not text:
        return False
    try:
        import pyperclip
        pyperclip.copy(text)
        return True
    except Exception:
        return False


def paste() -> str | None:
    """Read the OS clipboard, or None if no backend resolves."""
    try:
        import pyperclip
        return pyperclip.paste()
    except Exception:
        return None


def detect_backend() -> str | None:
    """The Linux clipboard backend on PATH (``xclip`` / ``wl-copy`` / …), or
    None. On macOS/Windows pyperclip has a native backend — see
    :func:`is_backend_installed`."""
    for name in _LINUX_BACKENDS:
        if shutil.which(name):
            return name
    return None


def is_backend_installed() -> bool:
    """True if the OS clipboard is usable — a Linux backend is on PATH, or the
    platform has a native one (macOS/Windows)."""
    import platform
    if platform.system() in ("Darwin", "Windows"):
        return True
    return detect_backend() is not None


__all__ = ["copy", "paste", "detect_backend", "is_backend_installed"]
