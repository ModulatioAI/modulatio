# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Cowboy Claude and Clifton Knox.
"""WebOS action routes — the mutating page verbs.

Every handler is a thin binding over the SAME engine seam the matching TUI
screen's key-action calls; the web surface adds no new authority. Destructive
verbs validate their id at the boundary (never a raw filesystem touch) and the
frontend guards them behind a confirm dialog. Read-only listing stays in
``data.py``; this module is where the buttons act.
"""

from __future__ import annotations

import subprocess

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, field_validator

from modulatio import cron, store, vault
from modulatio.web.actors import KickoffBusy, get_actor
from modulatio.web.routes.console import valid_project

router = APIRouter(prefix="/api")


def _valid_run(code: str, run_id: str) -> str:
    try:
        vault.validate_run_id(run_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if not vault.run_dir(code, run_id).exists():
        raise HTTPException(status_code=404, detail=f"unknown run {run_id}")
    return run_id


# ── cron ──────────────────────────────────────────────────────────────


def _require_job(code: str, job_id: str) -> dict:
    """Fetch a cron job, refusing one that belongs to a DIFFERENT project
    (WB-2): the job id is global, but a verb reached under ``/api/{project}``
    must only touch that project's own schedule. Cron stores project_code
    uppercased (``cron.add``), so compare against ``code.upper()``."""
    job = cron.get(job_id)
    if job is None or job.get("project_code") != code.upper():
        raise HTTPException(status_code=404, detail=f"unknown cron job {job_id}")
    return job


@router.post("/{project}/cron/{job_id}/enable")
def cron_enable(project: str, job_id: str) -> dict:
    code = valid_project(project)
    _require_job(code, job_id)
    return {"enabled": cron.enable(job_id)}


@router.post("/{project}/cron/{job_id}/disable")
def cron_disable(project: str, job_id: str) -> dict:
    code = valid_project(project)
    _require_job(code, job_id)
    return {"disabled": cron.disable(job_id)}


@router.post("/{project}/cron/{job_id}/run-now")
def cron_run_now(project: str, job_id: str) -> dict:
    code = valid_project(project)
    _require_job(code, job_id)
    job = cron.run_now(job_id)
    return {"queued": job is not None}


@router.delete("/{project}/cron/{job_id}")
def cron_remove(project: str, job_id: str) -> dict:
    code = valid_project(project)
    _require_job(code, job_id)
    return {"removed": cron.remove(job_id)}


# ── tickets ───────────────────────────────────────────────────────────


@router.delete("/{project}/tickets/{ticket_id}")
def ticket_delete(project: str, ticket_id: str) -> dict:
    code = valid_project(project)
    try:
        vault.validate_registry_name(ticket_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if store.get_ticket(code, ticket_id) is None:
        raise HTTPException(status_code=404, detail=f"unknown ticket {ticket_id}")
    return {"deleted": store.delete_ticket(code, ticket_id)}


# ── jobs ──────────────────────────────────────────────────────────────


@router.delete("/{project}/runs/{run_id}")
def run_delete(project: str, run_id: str, request: Request) -> dict:
    code = valid_project(project)
    # WB-1: never delete a run folder while the engine is writing one — the
    # TUI Jobs screen refuses deletion while any job is in flight. Mirror that:
    # a live kickoff on this project's actor blocks deletion (409).
    if get_actor(code, stub=bool(request.app.state.stub)).kickoff_active():
        raise HTTPException(
            status_code=409,
            detail="a job is in flight — finish or stop it before deleting runs")
    _valid_run(code, run_id)
    return {"deleted": vault.delete_run(code, run_id)}


# ── docs (global, like the DOCS tab) ──────────────────────────────────


@router.post("/docs/update")
def docs_update() -> dict:
    from modulatio import docs

    return {"status": docs.update_docs()}


# ── logs (global store) ───────────────────────────────────────────────


def _require_log(log_id: str):
    from modulatio import logstore

    entry = logstore.find_log(log_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"unknown log {log_id}")
    return entry


@router.delete("/logs/{log_id}")
def log_delete(log_id: str) -> dict:
    from modulatio import logstore

    entry = _require_log(log_id)
    if not entry.deletable:
        raise HTTPException(
            status_code=409, detail=f"a {entry.label} can't be deleted here")
    return {"deleted": logstore.delete_log(entry)}


@router.post("/logs/{log_id}/send")
def log_send(log_id: str) -> dict:
    """Open a prefilled issue for this log — the web analog of the TUI's send
    modal. Returns the GitHub new-issue URL (title + re-scrubbed body) for the
    browser to open, and marks the log sent, exactly like the terminal."""
    from modulatio import bug_report, logstore

    entry = _require_log(log_id)
    title, body = logstore.compose_issue(entry)
    url = bug_report.prefilled_issue_url(title, body)
    logstore.mark_sent(entry.path, url)
    return {"url": url}


# ── skills ────────────────────────────────────────────────────────────


class SkillBody(BaseModel):
    name: str
    description: str
    prompt_template: str

    @field_validator("name", "description", "prompt_template")
    @classmethod
    def _non_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must be non-blank")
        return v


@router.post("/{project}/skills")
def skill_create(project: str, body: SkillBody) -> dict:
    from modulatio import skills

    code = valid_project(project)
    try:
        vault.validate_registry_name(body.name)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    skills.create_skill(
        name=body.name, description=body.description,
        prompt_template=body.prompt_template, project_code=code)
    return {"created": body.name}


@router.delete("/{project}/skills/{name}")
def skill_delete(project: str, name: str) -> dict:
    from modulatio import skills

    code = valid_project(project)
    try:
        vault.validate_registry_name(name)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"deleted": skills.delete_skill(name, project_code=code)}


# ── memory ────────────────────────────────────────────────────────────
#
# The MEMORY page's verbs mirror the TUI tab exactly: an operator adds/edits/
# deletes an AGENT's own memory in place, while TEAM memory is QC-curated —
# edits go through propose→approve, never a direct write.


class MemoryContent(BaseModel):
    content: str

    @field_validator("content")
    @classmethod
    def _non_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("content must be non-blank")
        return v


class MemoryEdit(BaseModel):
    layer: str
    content: str

    @field_validator("content")
    @classmethod
    def _non_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("content must be non-blank")
        return v


class MemoryProposal(BaseModel):
    body: str

    @field_validator("body")
    @classmethod
    def _non_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("body must be non-blank")
        return v


def _valid_agent(agent_id: str) -> str:
    try:
        return vault.validate_registry_name(agent_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/{project}/memory/agent/{agent_id}")
def memory_add(project: str, agent_id: str, body: MemoryContent) -> dict:
    from modulatio.memory import agent_memory

    code = valid_project(project)
    _valid_agent(agent_id)
    entry = agent_memory.add_semantic(agent_id, body.content, project_code=code)
    return {"id": entry.id}


@router.put("/{project}/memory/agent/{agent_id}/{entry_id}")
def memory_edit(project: str, agent_id: str, entry_id: str, body: MemoryEdit) -> dict:
    from modulatio.memory import agent_memory

    code = valid_project(project)
    _valid_agent(agent_id)
    updated = agent_memory.update_entry(
        agent_id, entry_id, project_code=code, layer=body.layer,
        content=body.content)
    if updated is None:
        raise HTTPException(status_code=404, detail="entry not found")
    return {"edited": True}


@router.delete("/{project}/memory/agent/{agent_id}/{entry_id}")
def memory_delete(
    project: str, agent_id: str, entry_id: str, layer: str = Query(...)
) -> dict:
    from modulatio.memory import agent_memory

    code = valid_project(project)
    _valid_agent(agent_id)
    return {"deleted": agent_memory.delete_entry(
        agent_id, entry_id, project_code=code, layer=layer)}


@router.post("/{project}/memory/propose")
def memory_propose(project: str, body: MemoryProposal) -> dict:
    from modulatio.memory import team_memory

    code = valid_project(project)
    prop = team_memory.propose(
        proposer_id="operator", body=body.body, project_code=code,
        rationale="Operator-proposed revision from the WebOS MEMORY page.")
    return {"proposal_id": prop.proposal_id}


@router.post("/{project}/memory/proposals/{proposal_id}/approve")
def memory_approve(project: str, proposal_id: str) -> dict:
    from modulatio.memory import team_memory

    code = valid_project(project)
    entry = team_memory.approve_proposal(
        proposal_id, project_code=code, approver_id="operator",
        approver_tier="leader")
    if entry is None:
        raise HTTPException(status_code=404, detail="proposal not found")
    return {"approved": True}


@router.post("/{project}/memory/proposals/{proposal_id}/reject")
def memory_reject(project: str, proposal_id: str) -> dict:
    from modulatio.memory import team_memory

    code = valid_project(project)
    if not team_memory.reject_proposal(proposal_id, project_code=code):
        raise HTTPException(status_code=404, detail="proposal not found")
    return {"rejected": True}


# ── JT library ────────────────────────────────────────────────────────


class ScheduleBody(BaseModel):
    schedule: str                 # "once" or the recurrence DSL (daily/weekly/…)
    start_at: str | None = None   # ISO datetime — the picked first run + anchor
    count: int | None = None      # run N times, then stop (None = infinite)
    until: str | None = None      # ISO end date (inclusive), or None

    @field_validator("schedule")
    @classmethod
    def _non_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("schedule must be non-blank")
        return v


def _valid_jt_name(name: str) -> str:
    try:
        return vault.validate_registry_name(name)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/{project}/jts/{name}/kickoff")
def jt_kickoff(project: str, name: str, request: Request) -> dict:
    """Kick off a Job Template as a run — the web analog of the JT Library's
    Kick off button. Pre-flighted with the same add-time check the TUI uses
    (params satisfiable from the template's own defaults), then launched
    JT-bound through the single-flight actor."""
    from modulatio.tui.screens.jt_library import kickoff_template_now

    code = valid_project(project)
    _valid_jt_name(name)
    ok, message = kickoff_template_now(name, code)
    if not ok:
        raise HTTPException(status_code=404, detail=message)
    actor = get_actor(code, stub=bool(request.app.state.stub))
    try:
        run_id = actor.kickoff(message, jt_name=name)
    except KickoffBusy as busy:
        raise HTTPException(
            status_code=409,
            detail={"run_id": busy.run_id, "message": str(busy)},
        ) from busy
    return {"run_id": run_id}


@router.post("/{project}/jts/{name}/schedule")
def jt_schedule(project: str, name: str, body: ScheduleBody) -> dict:
    """Schedule a Job Template as a recurring cron job — bound to the JT, using
    the template's own parameter defaults (the JT Library's Schedule button)."""
    from modulatio.tui.screens.jt_library import schedule_template_as_cron

    code = valid_project(project)
    _valid_jt_name(name)
    ok, message = schedule_template_as_cron(
        name, body.schedule, code,
        start_at=body.start_at, count=body.count, until=body.until)
    if not ok:
        raise HTTPException(status_code=400, detail=message)
    return {"scheduled": message}


# ── artifacts (export + delete) ───────────────────────────────────────


class ExportBody(BaseModel):
    path: str
    format: str
    folder: str


def _artifact_roots(code: str) -> list:
    root = vault.project_dir(code)
    return [root / "artifacts", root / "research"]


def _resolve_artifact(code: str, rel_path: str):
    """Resolve a project-relative artifact path INSIDE the trusted artifact
    roots (never a symlinked root, never an escaping target) — the same
    boundary the read-only preview enforces. 404 on anything outside."""
    from modulatio.tui.screens.artifacts import _is_artifact_file

    proj = vault.project_dir(code).resolve()
    target = (proj / rel_path).resolve()
    roots = []
    for d in _artifact_roots(code):
        if not d.exists() or d.is_symlink():
            continue
        rd = d.resolve()
        if rd == proj or proj in rd.parents:
            roots.append(rd)
    if not any(r == target or r in target.parents for r in roots):
        raise HTTPException(status_code=404, detail="not under an artifact root")
    if not target.exists() or not _is_artifact_file(target):
        raise HTTPException(status_code=404, detail="not an artifact")
    return target


@router.delete("/{project}/artifacts")
def artifact_delete(project: str, path: str = Query(...)) -> dict:
    code = valid_project(project)
    target = _resolve_artifact(code, path)
    target.unlink()
    return {"deleted": True}


@router.post("/{project}/artifacts/export")
def artifact_export(project: str, body: ExportBody) -> dict:
    """Render an artifact via pandoc and write it into a REGISTERED folder
    (Folders tab — including an OS-mounted network share). The destination is
    validated as a currently-registered, reachable, writable folder through
    the same ``folder_grant_roots`` seam the engine grants seats — a
    hand-edited path can't target /etc or a secrets dir."""
    from pathlib import Path

    from modulatio import config, export

    code = valid_project(project)
    source = _resolve_artifact(code, body.path)

    rw_roots, _read = config.folder_grant_roots()
    dest_dir = next(
        (r for r in rw_roots
         if any(f["path"] == r and f["name"] == body.folder
                for f in config.list_folders())),
        None,
    )
    if dest_dir is None:
        raise HTTPException(
            status_code=404,
            detail=f"'{body.folder}' is not a registered writable folder")

    ext = {"docx": ".docx", "pdf": ".pdf", "markdown": ".md"}.get(body.format)
    if ext is None:
        raise HTTPException(status_code=422, detail=f"bad format {body.format!r}")
    dest = Path(dest_dir) / (source.stem + ext)
    try:
        export.export_artifact(source, dest, body.format)
    except export.ExportError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    # WB-3: the absolute host path (folder/share name) never crosses the
    # boundary — return only the folder name asked for + the filename.
    return {"folder": body.folder, "filename": dest.name}


# ── jobs reveal ───────────────────────────────────────────────────────


@router.post("/{project}/runs/{run_id}/reveal")
def run_reveal(project: str, run_id: str) -> dict:
    """Best-effort OS reveal of a run folder on the SERVER host (meaningful for
    the default loopback bind, where the server is the operator's own machine)
    and always return the path so it's reachable on a LAN bind too."""
    code = valid_project(project)
    _valid_run(code, run_id)
    path = vault.run_dir(code, run_id)
    try:
        subprocess.Popen(
            ["xdg-open", str(path)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, ValueError):
        pass  # headless / no xdg-open — the path is still returned
    return {"path": str(path)}
