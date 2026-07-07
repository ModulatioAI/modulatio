# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Cowboy Claude and Clifton Knox.
"""Projects — bound to ``vault.list_projects`` + the default-project seam."""

from __future__ import annotations

from fastapi import APIRouter

from modulatio import config, vault

router = APIRouter(prefix="/api")


@router.get("/projects")
def list_projects() -> dict:
    return {
        "projects": vault.list_projects(),
        "default": config.get_default_project_code(),
    }
