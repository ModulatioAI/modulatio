# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Setup state — tracks whether the wizard has been run + first-install detection.

Stored at ``~/.config/modulatio/setup-state.json``. Separate from
``defaults.json`` (paths + models) and ``preferences.json`` (user prefs)
to keep concerns isolated.

Schema:

.. code-block:: python

    {
        "wizard_completed_at": "2026-04-24T18:30:00Z",  # ISO timestamp
        "wizard_version": "2.0.0",                       # Modulatio version that ran wizard
        "skipped_steps": ["pandoc", "embedded_llm"],     # what user skipped
    }

Used by:
- TUI app on launch — if no setup-state, push wizard before tabs (slice 5)
- ``modulatio kickoff`` — if no setup-state, suggest running ``modulatio setup`` first
- ``modulatio setup`` itself — re-invocation pre-populates from existing state
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from modulatio import config

SETUP_STATE_FILE = config.CONFIG_DIR / "setup-state.json"


def setup_completed() -> bool:
    """First-install detection: True if the wizard has been run at least once."""
    return SETUP_STATE_FILE.exists()


def load() -> dict[str, Any]:
    """Read the setup state. Empty dict if file missing or malformed."""
    if not SETUP_STATE_FILE.exists():
        return {}
    try:
        return json.loads(SETUP_STATE_FILE.read_text()) or {}
    except (OSError, json.JSONDecodeError):
        return {}


def save(state: dict[str, Any]) -> None:
    """Persist setup state."""
    config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    SETUP_STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True))


def mark_completed(*, version: str, skipped_steps: list[str] | None = None) -> dict[str, Any]:
    """Mark the wizard as completed. Called by setup_wizard.finalize."""
    state = load()
    state["wizard_completed_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    state["wizard_version"] = version
    state["skipped_steps"] = list(skipped_steps or [])
    save(state)
    return state


def reset() -> bool:
    """Delete setup state — forces wizard to run again next launch.

    Returns True if state was removed, False if it didn't exist.
    """
    if SETUP_STATE_FILE.exists():
        SETUP_STATE_FILE.unlink()
        return True
    return False
