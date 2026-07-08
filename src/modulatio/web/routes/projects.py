# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Cowboy Claude and Clifton Knox.
"""Projects — bound to ``vault.list_projects`` + the create/switch/delete
seams. Delete carries the TUI's two guards, ported: never the active project,
never while a job is in flight (the in-flight guard the WebOS learned on the
verbs arc — a project can't be removed out from under a writing engine)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, field_validator

from modulatio import config, vault

router = APIRouter(prefix="/api")


class ProjectCreate(BaseModel):
    code: str
    objective: str = ""

    @field_validator("code")
    @classmethod
    def _non_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("code must be non-blank")
        return v


@router.get("/projects")
def list_projects() -> dict:
    return {
        "projects": vault.list_projects(),
        "default": config.get_default_project_code(),
    }


@router.post("/projects")
def create_project(body: ProjectCreate) -> dict:
    from modulatio import roster

    try:
        roster.create_project(body.code, body.objective)
    except ValueError as exc:  # invalid code
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"created": body.code}


@router.post("/projects/{code}/switch")
def switch_project(code: str) -> dict:
    try:
        vault.validate_project_code(code)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if code not in vault.list_projects():
        raise HTTPException(status_code=404, detail=f"unknown project {code}")
    config.set_default_project_code(code)
    return {"active": code}


@router.delete("/projects/{code}")
def delete_project(code: str, request: Request) -> dict:
    from modulatio.web.actors import get_actor

    try:
        vault.validate_project_code(code)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if code not in vault.list_projects():
        raise HTTPException(status_code=404, detail=f"unknown project {code}")
    if code == config.get_default_project_code():
        raise HTTPException(
            status_code=409,
            detail="can't delete the active project — switch away first")
    if get_actor(code, stub=bool(request.app.state.stub)).kickoff_active():
        raise HTTPException(
            status_code=409, detail="can't delete a project while a job is running")
    from modulatio import backup

    backup.delete_project(code)
    return {"deleted": code}
