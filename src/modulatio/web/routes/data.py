# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Cowboy Claude and Clifton Knox.
"""Read-only MasterDetail data routes — one endpoint per page, each a
direct engine-seam call. Nothing here mutates; the interactive verbs
(ticket approve/deny, cron toggle, exports) arrive in later phases with
their own review.

Red-lines held here: filesystem paths never cross the boundary except
as project-relative artifact paths; previews are extension-filtered,
size-capped and resolved inside the project root; registry names and
run ids validate before any disk touch.
"""

from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, HTTPException, Query

from modulatio import cron, docs, logstore, store, vault
from modulatio.web.routes.console import valid_project
from modulatio.web.serialize import json_safe

router = APIRouter(prefix="/api")

#: Preview cap — same 2000-char truncation the TUI's Artifacts pane uses.
_PREVIEW_CHARS = 2000


def _valid_run(code: str, run_id: str) -> str:
    try:
        vault.validate_run_id(run_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if not vault.run_dir(code, run_id).exists():
        raise HTTPException(status_code=404, detail=f"unknown run {run_id}")
    return run_id


# ── runs / jobs ───────────────────────────────────────────────────────


@router.get("/{project}/runs")
def list_runs(project: str) -> dict:
    code = valid_project(project)
    runs = []
    for run_id in reversed(vault.list_runs(code)):  # newest first, JOBS order
        size = vault.run_size(vault.run_dir(code, run_id))
        runs.append({
            "run_id": run_id, "size": size, "size_human": vault.human_size(size),
        })
    return {"runs": runs}


@router.get("/{project}/runs/{run_id}")
def run_detail(project: str, run_id: str) -> dict:
    code = valid_project(project)
    _valid_run(code, run_id)
    run_path = vault.run_dir(code, run_id)
    objective = ""
    obj_file = run_path / "objective.md"
    if obj_file.exists():
        objective = obj_file.read_text(encoding="utf-8", errors="replace")
    counts = {
        sub: sum(1 for p in (run_path / sub).rglob("*") if p.is_file())
        if (run_path / sub).exists() else 0
        for sub in vault.RUN_SUBDIRS
    }
    return {
        "run_id": run_id,
        "objective": json_safe(objective),
        "counts": counts,
        "size_human": vault.human_size(vault.run_size(run_path)),
    }


@router.get("/{project}/runs/{run_id}/tasks")
def run_tasks(project: str, run_id: str) -> dict:
    code = valid_project(project)
    _valid_run(code, run_id)
    tasks = store.list_tasks(code, run_id=run_id)
    return {"tasks": [t.model_dump(mode="json") for t in tasks]}


@router.get("/{project}/runs/{run_id}/goals")
def run_goals(project: str, run_id: str) -> dict:
    code = valid_project(project)
    _valid_run(code, run_id)
    goals = store.list_goals(code, run_id=run_id)
    return {"goals": [g.model_dump(mode="json") for g in goals]}


# ── tickets ───────────────────────────────────────────────────────────


@router.get("/{project}/tickets")
def list_tickets(project: str) -> dict:
    code = valid_project(project)
    tickets = store.list_tickets(code)
    return {"tickets": [t.model_dump(mode="json") for t in tickets]}


# ── jt library ────────────────────────────────────────────────────────


@router.get("/{project}/jts")
def list_jts(project: str) -> dict:
    from modulatio import job_template_library

    code = valid_project(project)
    return {"jts": [asdict(e) for e in job_template_library.build_index(code)]}


@router.get("/{project}/jts/{name}")
def jt_detail(project: str, name: str) -> dict:
    from modulatio import job_template_library

    code = valid_project(project)
    try:
        vault.validate_registry_name(name)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    jt = job_template_library.checkout(name, code)
    if not jt.name:
        raise HTTPException(status_code=404, detail=f"unknown job template {name}")
    return json_safe(asdict(jt))


# ── skills ────────────────────────────────────────────────────────────


@router.get("/{project}/skills")
def list_skills(project: str) -> dict:
    from modulatio import skills

    code = valid_project(project)
    return {"skills": skills.list_skills(code)}


@router.get("/{project}/skills/{name}")
def skill_detail(project: str, name: str) -> dict:
    from modulatio import skills

    code = valid_project(project)
    try:
        vault.validate_registry_name(name)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if name not in skills.list_skills(code):
        raise HTTPException(status_code=404, detail=f"unknown skill {name}")
    return {"name": name, "body": json_safe(skills.load(name, code))}


# ── memory ────────────────────────────────────────────────────────────


@router.get("/{project}/memory")
def memory(project: str) -> dict:
    from modulatio.memory import team_memory

    code = valid_project(project)
    return {
        "entries": [json_safe(asdict(e)) for e in team_memory.list_entries(code)],
        "proposals": [
            json_safe(asdict(p)) for p in team_memory.list_proposals(code)
        ],
    }


# ── cron ──────────────────────────────────────────────────────────────


@router.get("/{project}/cron")
def cron_jobs(project: str) -> dict:
    code = valid_project(project)
    return {"jobs": json_safe(cron.list_jobs(project_code=code))}


# ── logs (global store, like the LOGS tab today) ──────────────────────


@router.get("/logs")
def list_logs() -> dict:
    entries = []
    for e in logstore.list_logs():
        entries.append({
            # Deliberately NOT the path: the id is the stable handle and
            # the local filesystem layout never crosses the web boundary.
            "id": e.id,
            "kind": e.kind,
            "label": e.label,
            "timestamp": logstore.format_timestamp(e.timestamp),
            "summary": json_safe(e.summary),
            "sent": e.sent,
            "size": e.size,
        })
    return {"logs": entries}


# ── docs ──────────────────────────────────────────────────────────────


@router.get("/docs")
def list_docs() -> dict:
    return {"docs": [{"slug": s, "title": t} for s, t in docs.list_docs()]}


@router.get("/docs/{slug}")
def read_doc(slug: str) -> dict:
    try:
        markdown = docs.read_doc(slug)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if not markdown:
        raise HTTPException(status_code=404, detail=f"unknown doc {slug}")
    return {"slug": slug, "markdown": markdown}


# ── artifacts ─────────────────────────────────────────────────────────


def _artifact_roots(code: str) -> list:
    """The TUI Artifacts tab's rooting: artifacts + research are durable
    (project root); reports are per-run and surface via the run pages."""
    root = vault.project_dir(code)
    return [root / "artifacts", root / "research"]


@router.get("/{project}/artifacts")
def list_artifacts(project: str) -> dict:
    from modulatio import families
    from modulatio.tui.screens.artifacts import _FAMILY_GLYPH, _is_artifact_file

    code = valid_project(project)
    root = vault.project_dir(code)
    files = []
    for d in _artifact_roots(code):
        if not d.exists():
            continue
        for p in sorted(d.rglob("*")):
            if not _is_artifact_file(p):
                continue
            family = families.infer_artifact_family_from_path(p)
            files.append({
                "path": str(p.relative_to(root)),
                "size": p.stat().st_size,
                "family": family,
                "family_glyph": _FAMILY_GLYPH.get(family, "·"),
            })
    return {"files": files}


@router.get("/{project}/artifacts/preview")
def artifact_preview(project: str, path: str = Query(...)) -> dict:
    from modulatio.tui.screens.artifacts import _is_artifact_file

    code = valid_project(project)
    root = vault.project_dir(code)
    target = (root / path).resolve()
    # The authorization boundary is the ARTIFACT ROOTS, never the project
    # root (WB-1): a project-root file like permissions.json can pass the
    # extension filter but is not an artifact. Resolve the roots too so a
    # symlink inside artifacts/ can't point back out to a non-artifact.
    roots = [d.resolve() for d in _artifact_roots(code) if d.exists()]
    if not any(r == target or r in target.parents for r in roots):
        raise HTTPException(status_code=404, detail="not under an artifact root")
    if not target.exists() or not _is_artifact_file(target):
        raise HTTPException(status_code=404, detail="not a previewable artifact")
    text = target.read_text(encoding="utf-8", errors="replace")
    if len(text) > _PREVIEW_CHARS:
        text = text[:_PREVIEW_CHARS] + "\n…"
    return {"path": path, "text": json_safe(text)}
