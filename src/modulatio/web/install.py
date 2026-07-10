# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Cowboy Claude and Clifton Knox.
"""Install the opt-in WebOS ``[web]`` extra from inside Modulatio.

Stdlib-only and import-safe without FastAPI/uvicorn present — this is the
module that *installs* them, so it can't depend on them. The setup wizard's
WebOS step and the TUI's CONFIG → SETTINGS button are its two callers.

Approach: derive the exact package specs from our OWN installed metadata (so
they can never drift from ``pyproject.toml``), pick the environment-correct
command (``pipx inject`` when Modulatio runs from a pipx venv — durable across
a later ``pipx upgrade`` — else ``<python> -m pip install``), run it, and
VERIFY by re-checking that the modules import. Any failure returns
``(False, reason)`` so the caller can show the manual command instead.
"""

from __future__ import annotations

import importlib.metadata as _md
import subprocess
import sys
from importlib.util import find_spec
from pathlib import Path

#: The manual fallback command (matches the hint ``web/server.py`` prints).
_MANUAL = 'pip install "modulatio[web]"'

#: Ceiling on the install subprocess — two small pure-Python wheels; a hang
#: past this is a stuck resolver, not slow progress.
_INSTALL_TIMEOUT_S = 600


def is_installed() -> bool:
    """True when both WebOS runtime deps import — the same check
    ``web/server.py`` gates its launch on."""
    return find_spec("fastapi") is not None and find_spec("uvicorn") is not None


def web_requirements() -> list[str]:
    """The ``[web]`` extra's package specs, read from our installed metadata.

    Single source of truth: whatever ``pyproject.toml`` declared for the extra
    of THIS installed version. Empty list means the metadata is missing/foreign
    — the caller treats that as "cannot auto-install."
    """
    out: list[str] = []
    for raw in _md.requires("modulatio") or []:
        spec, _, marker = raw.partition(";")
        # The marker quotes the extra name as 'web' or "web" depending on the
        # backend — accept either.
        if "extra ==" in marker and ("'web'" in marker or '"web"' in marker):
            spec = spec.strip()
            # A spec starting with '-' would land in the install argv as
            # a pip OPTION (e.g. a rogue --index-url from hostile/broken
            # dist-info). Only real requirement names reach the command.
            if spec and not spec.startswith("-"):
                out.append(spec)
    return out


def _is_pipx() -> bool:
    """True when Modulatio runs from a pipx-managed venv
    (``…/pipx/venvs/<name>``)."""
    parts = Path(sys.prefix).parts
    return "pipx" in parts and "venvs" in parts


def install_command() -> list[str]:
    """The env-correct argv to add the WebOS deps. ``pipx inject`` under pipx
    (so they survive a later ``pipx upgrade``), else pip into this
    interpreter. Installs only the deps, never ``modulatio[web]`` — the
    meta-spec would re-resolve Modulatio itself and could downgrade a newer
    local build."""
    reqs = web_requirements()
    if _is_pipx():
        return ["pipx", "inject", "modulatio", *reqs]
    return [sys.executable, "-m", "pip", "install", *reqs]


def manual_command() -> str:
    """The command to run by hand when the auto-install can't (offline, no
    writable env, a locked-down system)."""
    return _MANUAL


def install(*, timeout: int = _INSTALL_TIMEOUT_S) -> tuple[bool, str]:
    """Install the WebOS deps, then verify by re-checking the imports. Never
    raises — returns ``(ok, message)``; ``ok=False`` means fall back to
    :func:`manual_command`."""
    if not web_requirements():
        return False, (
            "Couldn't read the WebOS package list from Modulatio's metadata. "
            f"Install it manually: {_MANUAL}"
        )
    cmd = install_command()
    try:
        subprocess.run(cmd, check=True, timeout=timeout)
    except (subprocess.SubprocessError, OSError) as exc:
        return False, (
            f"Install command failed ({type(exc).__name__}). "
            f"Install it manually: {_MANUAL}"
        )
    if not is_installed():
        return False, (
            "Install ran but FastAPI/uvicorn still don't import. "
            f"Try manually: {_MANUAL}"
        )
    return True, "Modulatio WebOS installed — run `modulatio-api` to launch it."


__all__ = [
    "is_installed",
    "web_requirements",
    "install_command",
    "manual_command",
    "install",
]
