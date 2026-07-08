# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Cowboy Claude and Clifton Knox.
"""The WebOS FastAPI application factory.

Thin by law: handlers call engine seams and serialize; anything a
handler wants to invent belongs in the engine. The static SPA (vanilla
ES modules, no build step) is mounted last so API routes win.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

STATIC_DIR = Path(__file__).parent / "static"


#: Host headers always served — the loopback names. A browser page from a
#: hostile origin whose DNS rebinds to 127.0.0.1 arrives with ITS hostname
#: here, so an allowlist is the rebinding fence (the token only guards
#: non-loopback binds).
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "[::1]", "::1"})


def create_app(
    *,
    bearer_token: str | None = None,
    stub: bool = False,
    allowed_hosts: list[str] | None = None,
) -> FastAPI:
    """Build the WebOS app.

    ``bearer_token`` set → every ``/api``/``/events`` request must carry
    ``Authorization: Bearer <token>`` (the non-loopback red-line). The
    static shell stays open — the pairing prompt lives in it.

    ``allowed_hosts`` extends the loopback Host allowlist (the server
    passes its ``--host`` so LAN binds serve their own name). Any other
    Host header → 400: the DNS-rebinding fence.

    ``stub`` → actors run the engine on stub runners (the test suite's
    end-to-end path; production leaves it False).
    """
    app = FastAPI(title="Modulatio WebOS", docs_url=None, redoc_url=None)
    app.state.stub = stub

    served_hosts = _LOOPBACK_HOSTS | set(allowed_hosts or ())

    @app.middleware("http")
    async def _trusted_host(request: Request, call_next):
        host = request.headers.get("host", "").rsplit(":", 1)[0]
        if host not in served_hosts and host.lower() not in served_hosts:
            return JSONResponse({"detail": "unrecognized Host"}, status_code=400)
        return await call_next(request)

    if bearer_token is not None:
        @app.middleware("http")
        async def _require_token(request: Request, call_next):
            path = request.url.path
            if path.startswith(("/api", "/events")):
                supplied = request.headers.get("authorization", "")
                if supplied != f"Bearer {bearer_token}":
                    return JSONResponse(
                        {"detail": "bearer token required"}, status_code=401
                    )
            return await call_next(request)

    from modulatio.web.routes import actions, console, data, projects

    app.include_router(projects.router)
    app.include_router(console.router)
    app.include_router(data.router)
    app.include_router(actions.router)

    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app
