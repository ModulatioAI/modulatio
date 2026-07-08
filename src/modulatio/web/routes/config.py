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

from pathlib import Path

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


# ── FOLDERS (install-wide registry — the management view) ──────────────
#
# This is the operator's own folder registry (their own paths on the loopback
# box), so — unlike the export picker's /api/folders (names+modes only) — the
# management view shows the path you registered, exactly as the TUI does. The
# ADD guards are the TUI's, ported one-for-one so a hand-crafted request can't
# register what the terminal would refuse.

_FOLDER_MODES = ("ro", "output", "rw")


class FolderAdd(BaseModel):
    name: str
    path: str
    mode: str


@router.get("/config/folders")
def folders_list() -> dict:
    from modulatio import config

    return {"folders": [
        {"name": f["name"], "mode": f["mode"], "path": f["path"]}
        for f in config.list_folders()
    ]}


@router.post("/config/folders")
def folders_add(body: FolderAdd) -> dict:
    from modulatio import config

    if body.mode not in _FOLDER_MODES:
        raise HTTPException(status_code=422, detail=f"bad mode {body.mode!r}")
    p = Path(body.path)
    if not p.is_absolute():
        raise HTTPException(
            status_code=422,
            detail="path must be absolute (a mounted share is registered by "
                   "its mount point)")
    p = p.resolve()
    if not config.probe_folder(str(p)):
        raise HTTPException(
            status_code=422,
            detail=f"{p} is not a reachable directory — mount the share / "
                   "create the folder first")
    reason = config.folder_root_refusal(str(p))
    if reason is not None:
        raise HTTPException(status_code=422, detail=reason)
    name = body.name.strip() or p.name
    folders = config.list_folders()
    if any(r["name"].lower() == name.lower() for r in folders):
        raise HTTPException(status_code=422, detail=f"a folder named '{name}' exists")
    if any(Path(r["path"]) == p for r in folders):
        raise HTTPException(status_code=422, detail=f"{p} is already registered")
    folders.append({"name": name, "path": str(p), "mode": body.mode, "kind": "path"})
    config.save_folders(folders)
    return {"name": name, "mode": body.mode}


@router.delete("/config/folders/{name}")
def folders_remove(name: str) -> dict:
    from modulatio import config

    folders = config.list_folders()
    kept = [r for r in folders if r["name"].lower() != name.lower()]
    if len(kept) == len(folders):
        raise HTTPException(status_code=404, detail=f"unknown folder {name}")
    config.save_folders(kept)
    return {"deleted": True}


@router.post("/config/folders/{name}/output")
def folders_set_output(name: str) -> dict:
    """Pick a folder as the job-output destination — only an ``output``-mode
    folder can receive the finished product (the TUI's rule)."""
    from modulatio import config

    rec = next((r for r in config.list_folders() if r["name"] == name), None)
    if rec is None:
        raise HTTPException(status_code=404, detail=f"unknown folder {name}")
    if rec["mode"] != "output":
        raise HTTPException(
            status_code=409,
            detail=f"'{name}' is {rec['mode']} — only an output-mode folder "
                   "can receive the job's finished product")
    config.set_job_output_folder(name)
    return {"output": name}
