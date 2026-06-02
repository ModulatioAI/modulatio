# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Diagnostics bundle for bug reports — a redacted snapshot of the install.

``collect()`` returns a plain-text block safe to attach to a public bug report:
version / runtime, the configured models with their auth type + availability
(never a key or its value), the relevant opt-in toggles (set/unset, not values),
and the most recent crash-log filenames (names only). No secrets by
construction — it reads model *references* and env-var presence, never values.
"""
from __future__ import annotations

import os
import platform
import sys

#: Opt-in toggles worth knowing in a bug report — reported as set/unset only.
_TOGGLES = (
    "MODULATIO_QC_FIXER",
    "MODULATIO_LEADER_ITERATE",
    "MODULATIO_WAVE_REFLECT",
    "MODULATIO_CONCURRENT_WAVES",
    "MODULATIO_CRASH_DIR",
    "MODULATIO_GITHUB_TOKEN",
)


def _version() -> str:
    try:
        from modulatio import __version__
        return __version__
    except Exception:
        return "unknown"


def _models_block() -> list[str]:
    try:
        from modulatio import model_presets
        presets = model_presets.load_presets()
    except Exception as exc:
        return [f"models: (could not load — {type(exc).__name__})"]
    if not presets:
        return ["models: (none configured)"]
    lines = [f"models: {len(presets)} configured"]
    for key, preset in sorted(presets.items()):
        try:
            ready = model_presets.is_available(key)
        except Exception:
            ready = False
        # model id + auth TYPE only — never the key/value.
        lines.append(
            f"  - {key}: {preset.get('model', '?')} "
            f"[{preset.get('auth_type', '?')}] "
            f"{'ready' if ready else 'needs setup'}"
        )
    return lines


def _toggles_block() -> list[str]:
    lines = ["toggles:"]
    for name in _TOGGLES:
        # presence only — never the value (some, like the token, are secret).
        lines.append(f"  - {name}: {'set' if os.environ.get(name) else 'unset'}")
    return lines


def _recent_crashes() -> list[str]:
    try:
        from modulatio._crash import crash_dir
        d = crash_dir()
        if not d.exists():
            return ["recent crashes: (none)"]
        logs = sorted(d.glob("crash-*.log"), reverse=True)[:3]
        if not logs:
            return ["recent crashes: (none)"]
        # filenames only — the contents stay on disk for the user to attach.
        return ["recent crashes (names only):"] + [f"  - {p.name}" for p in logs]
    except Exception:
        return ["recent crashes: (unavailable)"]


def collect() -> str:
    """A redacted diagnostics bundle, safe to attach to a public bug report."""
    lines: list[str] = [
        "## Diagnostics",
        f"modulatio: {_version()}",
        f"python: {sys.version.split()[0]}",
        f"platform: {platform.platform()}",
        "",
    ]
    lines += _models_block()
    lines.append("")
    lines += _toggles_block()
    lines.append("")
    lines += _recent_crashes()
    return "\n".join(lines)


__all__ = ["collect"]
