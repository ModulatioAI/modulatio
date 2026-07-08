# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Cowboy Claude and Clifton Knox.
"""WebOS CONFIG routes — the read/write configuration surface (Feature 2).

The browser mirror of the TUI's CONFIG tab: Settings, Folders, Projects,
Agents, Models, Services. Every handler is a thin call into the SAME engine
seam the matching TUI screen uses, and — the load-bearing rule — reproduces
that screen's GUARDS explicitly (shell/.env read-only, range checks, the triad
floor, project triple-guard, folder-root refusal). Secret VALUES are
write-only: they go IN and are never returned; a key view reports only whether
a slot is set.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/api")


# ── SETTINGS (install-wide engine knobs) ──────────────────────────────


class KnobValue(BaseModel):
    value: str


@router.get("/settings")
def settings_list() -> dict:
    from modulatio import settings_knobs

    return {"knobs": [
        {
            "key": k.key, "label": k.label, "default": k.default, "hint": k.hint,
            "value": settings_knobs.knob_value(k.key),
            "source": settings_knobs.knob_source(k.key),
        }
        for k in settings_knobs.KNOBS
    ]}


@router.post("/settings/{key}")
def settings_set(key: str, body: KnobValue) -> dict:
    from modulatio import settings_knobs

    if key not in settings_knobs.BY_KEY:
        raise HTTPException(status_code=404, detail=f"unknown setting {key}")
    ok, reason = settings_knobs.set_knob(key, body.value)
    if not ok:
        # A shell/.env-owned knob is a conflict (it wins, read-only here); a
        # bad value is unprocessable.
        status = 409 if "read-only" in reason else 422
        raise HTTPException(status_code=status, detail=reason)
    return {"saved": True, "source": settings_knobs.knob_source(key)}


@router.delete("/settings/{key}")
def settings_clear(key: str) -> dict:
    from modulatio import settings_knobs

    if key not in settings_knobs.BY_KEY:
        raise HTTPException(status_code=404, detail=f"unknown setting {key}")
    settings_knobs.clear_knob(key)
    return {"cleared": True, "source": settings_knobs.knob_source(key)}
