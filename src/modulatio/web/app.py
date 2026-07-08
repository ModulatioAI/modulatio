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

#: A custom header the SPA sends on every request. State-changing methods
#: require it: a cross-origin page cannot set a custom header on a CORS
#: "simple request", so demanding one forces a preflight that (no CORS
#: middleware exists) fails — closing CSRF on the token-free loopback bind,
#: including the bodyless POSTs that a JSON content-type wouldn't catch.
_CSRF_HEADER = "x-modulatio-webos"
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


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

    @app.middleware("http")
    async def _csrf_guard(request: Request, call_next):
        # State-changing /api requests must carry the SPA's custom header.
        # Reads (GET/HEAD) and preflight (OPTIONS) pass; static assets pass.
        if (request.method not in _SAFE_METHODS
                and request.url.path.startswith(("/api", "/events"))
                and _CSRF_HEADER not in request.headers):
            return JSONResponse(
                {"detail": "missing WebOS request header"}, status_code=403)
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

    from modulatio.web.routes import actions, config as config_routes, console, data, projects

    app.include_router(projects.router)
    app.include_router(console.router)
    app.include_router(data.router)
    app.include_router(actions.router)
    app.include_router(config_routes.router)

    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app
