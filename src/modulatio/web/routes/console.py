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

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from modulatio import vault
from modulatio.web.events import get_bus

router = APIRouter(prefix="/api")

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


@router.get("/{project}/events")
async def event_stream(project: str) -> StreamingResponse:
    code = valid_project(project)
    bus = get_bus(code)

    async def stream():
        q = bus.subscribe()
        try:
            yield _sse("hello", {"project": code})
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
