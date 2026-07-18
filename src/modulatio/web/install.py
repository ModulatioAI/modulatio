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

import getpass
import importlib.metadata as _md
import os
import shutil
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


#: The user-level unit name for the WebOS server.
_SERVICE = "modulatio-api.service"


def _unit_dir() -> Path:
    """The systemd *user* unit directory (no sudo required to write it)."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "systemd" / "user"


def _resolve_api_script() -> str | None:
    """Absolute path of the installed ``modulatio-api`` entry point — from the
    running argv when we ARE that script, else from PATH. ``None`` when it
    can't be resolved (e.g. launched as ``python -m``)."""
    argv0 = sys.argv[0] if sys.argv else ""
    if Path(argv0).name == "modulatio-api":
        return str(Path(argv0).resolve())
    return shutil.which("modulatio-api")


def _unit_text(script: str, host: str | None, port: int | None) -> str:
    exec_start = script
    if host:
        exec_start += f" --host {host}"
    if port:
        exec_start += f" --port {port}"
    return (
        "[Unit]\n"
        "Description=Modulatio WebOS API (modulatio-api)\n"
        "After=network.target\n"
        "\n"
        "[Service]\n"
        f"ExecStart={exec_start}\n"
        "Restart=always\n"
        "RestartSec=3\n"
        "\n"
        "[Install]\n"
        # User units hang off the user manager's default.target.
        "WantedBy=default.target\n"
    )


def install_service(host: str | None = None, port: int | None = None) -> tuple[bool, str]:
    """Install + enable a user-level systemd unit so the WebOS survives
    reboots. No sudo: the unit lives in the user manager. Never raises —
    ``(ok, message)``; ``ok=False`` carries the reason and the manual path."""
    if not sys.platform.startswith("linux"):
        return False, "Service install needs Linux with systemd (user units)."
    systemctl = shutil.which("systemctl")
    if not systemctl:
        return False, "systemctl not found — service install needs systemd."
    script = _resolve_api_script()
    if not script:
        return False, (
            "Couldn't resolve the installed modulatio-api script; "
            "make sure `modulatio-api` is on PATH and retry."
        )

    unit = _unit_dir() / _SERVICE
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text(_unit_text(script, host, port))

    for cmd in (
        [systemctl, "--user", "daemon-reload"],
        [systemctl, "--user", "enable", "--now", _SERVICE],
    ):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        except (subprocess.SubprocessError, OSError) as exc:
            return False, f"`{' '.join(cmd[1:])}` failed ({type(exc).__name__})."
        if r.returncode != 0:
            return False, f"`{' '.join(cmd[1:])}` failed: {r.stderr.strip() or r.returncode}"

    # Linger keeps the user manager (and this service) running from boot,
    # without a login. Best-effort: some systems gate it behind polkit.
    msg = f"WebOS service installed and running ({_SERVICE}, user unit)."
    loginctl = shutil.which("loginctl")
    lingered = False
    if loginctl:
        try:
            r = subprocess.run(
                [loginctl, "enable-linger", getpass.getuser()],
                capture_output=True, text=True, timeout=30,
            )
            lingered = r.returncode == 0
        except (subprocess.SubprocessError, OSError):
            lingered = False
    if not lingered:
        msg += (
            " Note: couldn't enable linger — the service starts at login, not "
            "boot. For boot-start run: sudo loginctl enable-linger $USER"
        )
    return True, msg


def uninstall_service() -> tuple[bool, str]:
    """Disable and remove the user-level WebOS unit. Clean no-op when it was
    never installed."""
    unit = _unit_dir() / _SERVICE
    if not unit.exists():
        return True, "No service installed — nothing to remove."
    systemctl = shutil.which("systemctl")
    if systemctl:
        subprocess.run(
            [systemctl, "--user", "disable", "--now", _SERVICE],
            capture_output=True, text=True, timeout=30,
        )
    unit.unlink()
    if systemctl:
        subprocess.run(
            [systemctl, "--user", "daemon-reload"],
            capture_output=True, text=True, timeout=30,
        )
    return True, "WebOS service removed."


__all__ = [
    "is_installed",
    "web_requirements",
    "install_command",
    "manual_command",
    "install",
    "install_service",
    "uninstall_service",
]
