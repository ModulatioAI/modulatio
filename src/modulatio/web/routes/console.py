# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Cowboy Claude and Clifton Knox.
"""Console routes — the event stream (and, next, converse/kickoff/stop).

The stream is Server-Sent Events over plain HTTP. The SPA consumes it
via fetch-streaming (not ``EventSource``, which can't carry the bearer
header), so auth stays uniform with the rest of ``/api``.
"""

from __future__ import annotations

import asyncio
import json
import queue
from dataclasses import replace
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, field_validator, model_validator

from modulatio import vault
from modulatio.web.actors import KickoffBusy, get_actor
from modulatio.web.events import get_bus

router = APIRouter(prefix="/api")

#: History turns returned per request — the durable file keeps the full
#: thread; the browser windows like the TUI's prompt does.
_HISTORY_LIMIT = 500

#: Seconds between keepalive comments when no frames flow — lets a dead
#: client be detected and keeps proxies from timing the stream out.
_KEEPALIVE_S = 15.0


def valid_project(code: str) -> str:
    """The web trust boundary for path-supplied project codes: invalid →
    404, never a traceback (and never a filesystem touch)."""
    try:
        return vault.validate_project_code(code)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _sse(name: str, data: dict) -> str:
    return f"event: {name}\ndata: {json.dumps(data)}\n\n"


def _actor(request: Request, code: str):
    return get_actor(code, stub=bool(request.app.state.stub))


class ConverseBody(BaseModel):
    text: str = ""
    #: Handles from ``converse/upload``, claimed once as this turn is sent.
    uploads: "list[str]" = []

    @model_validator(mode="after")
    def _says_something(self) -> "ConverseBody":
        # A turn carrying a file says something without words — showing a
        # screenshot and asking nothing in particular is an ordinary way to
        # start. An empty turn carrying neither is the one with nothing in it.
        if not self.text.strip() and not self.uploads:
            raise ValueError("text must be non-blank")
        return self


class KickoffBody(BaseModel):
    objective: str

    @field_validator("objective")
    @classmethod
    def _non_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("objective must be non-blank")
        return v


class ApprovalBody(BaseModel):
    #: The operator's chosen scope — the TUI modal's exact vocabulary. The
    #: route validates the words; the broker clamps to the request's own
    #: available_scopes (fail-closed) before the gate ever sees it.
    scope: Literal["once", "session", "always", "deny"]


@router.get("/{project}/conversation")
def conversation(project: str) -> dict:
    code = valid_project(project)
    path = vault.project_dir(code) / "leader_conversation.jsonl"
    if not path.exists():
        return {"turns": []}
    turns: list[dict] = []
    # Same defensive read as the engine's _load_conversation: the file is
    # durable + hand-editable; a bad line degrades, never 500s.
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {"turns": []}
    for line in raw.splitlines():
        try:
            turns.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return {"turns": turns[-_HISTORY_LIMIT:]}


@router.post("/{project}/converse/upload")
async def converse_upload(project: str, request: Request) -> dict:
    """Take bytes from the composer and return a handle naming nothing.

    A browser has no filesystem the engine can reach, so the path a disk load
    names does not exist here and the bytes travel in the request instead. The
    reply is an opaque token rather than the location it was written to: a
    client that can name a server-side file is the one choosing what gets
    read, and every check after that answers a question the caller picked.
    """
    from modulatio import uploads

    code = valid_project(project)
    data = await request.body()
    name = request.headers.get("x-modulatio-filename") or "upload"
    try:
        handle, shown = uploads.stage_upload(
            data, display_name=name, project=code)
    except uploads.UploadRefused as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    return {"handle": handle, "name": shown, "size": len(data)}


@router.post("/{project}/converse")
def converse(project: str, body: ConverseBody, request: Request) -> dict:
    from modulatio import uploads
    from modulatio.attachments import build_attachment, looks_like_image

    code = valid_project(project)
    attachments = []
    claimed: "list" = []
    try:
        for handle in body.uploads:
            staged, shown = uploads.consume(handle, project=code)
            claimed.append(staged)
            # The same constructor a disk load goes through, so an upload
            # meets one policy rather than a second one written for it: the
            # byte cap, the digest, the regular-inode check, and the decode
            # that refuses a binary posing as a document.
            kind = "image" if looks_like_image(staged) else "document"
            item = build_attachment(staged, kind=kind)
            attachments.append(replace(item, name=shown))
    except uploads.UploadRefused as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (ValueError, OSError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=415, detail=str(exc)) from exc
    finally:
        # The upload's own copy has served its purpose either way: the
        # attachment carries an engine-held snapshot of its own, and a refused
        # one must not outlive the refusal.
        for path in claimed:
            path.unlink(missing_ok=True)
    reply = _actor(request, code).converse(
        body.text, attachments=attachments or None)
    return {"reply": reply}


@router.get("/{project}/mode")
def session_mode(project: str, request: Request) -> dict:
    """The converse Leader's autonomy mode — the console pill's mount-time
    read (live changes ride the `mode` SSE frame)."""
    code = valid_project(project)
    return {"mode": _actor(request, code).session_mode()}


@router.post("/{project}/conversation/reset")
def reset_conversation(project: str, request: Request) -> dict:
    code = valid_project(project)
    archived = _actor(request, code).reset_thread()
    return {"archived": str(archived) if archived else None}


@router.post("/{project}/console/clear")
def console_clear(project: str) -> dict:
    """Operator 'clear screen' — drop the run's replay buffer so a tab-return
    or reconnect doesn't repaint what was deliberately cleared. Telemetry
    survives (the rail is not part of the log)."""
    from modulatio.web.events import get_bus

    code = valid_project(project)
    get_bus(code).clear_replay()
    return {"cleared": True}


@router.post("/{project}/kickoff")
def kickoff(project: str, body: KickoffBody, request: Request) -> dict:
    code = valid_project(project)
    try:
        run_id = _actor(request, code).kickoff(body.objective)
    except KickoffBusy as busy:
        raise HTTPException(
            status_code=409,
            detail={"run_id": busy.run_id, "message": str(busy)},
        ) from busy
    return {"run_id": run_id}


@router.post("/{project}/stop")
def stop(project: str, request: Request) -> dict:
    code = valid_project(project)
    return {"stopped": _actor(request, code).stop()}


@router.post("/{project}/converse/interrupt")
def interrupt_converse(project: str, request: Request) -> dict:
    code = valid_project(project)
    return {"interrupted": _actor(request, code).interrupt_converse()}


@router.post("/{project}/approvals/{rid}")
def approval_decision(
    project: str, rid: str, body: ApprovalBody, request: Request
) -> dict:
    code = valid_project(project)
    if not _actor(request, code).broker.resolve(rid, body.scope):
        raise HTTPException(status_code=404, detail="unknown approval id")
    return {"resolved": True}


@router.get("/{project}/events")
async def event_stream(project: str) -> StreamingResponse:
    code = valid_project(project)
    bus = get_bus(code)

    async def stream():
        q = bus.subscribe()
        try:
            # The hello carries the version stamp: `engine` is this process's
            # import-time version, `disk` is what a reinstall may have landed
            # since. Read per connection, so the very reconnect that follows a
            # reinstall is the one that reports the skew.
            from modulatio import __version__, installed_version
            disk = installed_version()
            yield _sse("hello", {
                "project": code, "engine": __version__, "disk": disk,
                "stale": bool(disk and disk != __version__),
            })
            while True:
                try:
                    frame = await asyncio.to_thread(q.get, True, _KEEPALIVE_S)
                except queue.Empty:
                    yield ": keepalive\n\n"
                    continue
                yield _sse(frame.get("type", "event"), frame.get("data", {}))
        finally:
            bus.unsubscribe(q)

    return StreamingResponse(stream(), media_type="text/event-stream")
