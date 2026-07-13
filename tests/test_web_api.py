# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Cowboy Claude and Clifton Knox.
"""WebOS backend — the FastAPI app factory, entry-point guard, and
the projects endpoint.

Every route is a thin binding over an existing engine seam; these tests
exercise the binding against the real vault (isolated per-test by the
autouse conftest fixture), never a mock of the engine.
"""

from __future__ import annotations

import pytest

# The WebOS backend is the opt-in `[web]` extra; skip its suite cleanly
# when FastAPI isn't installed rather than erroring on a lean install.
pytest.importorskip("fastapi")

from modulatio import config, vault  # noqa: E402 — after the extra guard


@pytest.fixture()
def client():
    """A TestClient over a freshly-created app (isolated tmp vault)."""
    from fastapi.testclient import TestClient

    from modulatio.web.app import create_app

    return TestClient(create_app(), base_url="http://localhost")


# ── entry-point guard ─────────────────────────────────────────────────


def test_run_prints_install_hint_when_web_deps_missing(monkeypatch, capsys):
    """`modulatio-api` with the [web] extra absent must print the install
    hint and exit nonzero — never an ImportError traceback at launch."""
    from modulatio.web import server

    monkeypatch.setattr(server, "is_installed", lambda: False)
    with pytest.raises(SystemExit) as exc:
        server.run([])
    assert exc.value.code != 0
    err = capsys.readouterr().err
    assert 'pip install "modulatio[web]"' in err


def test_run_loads_vault_env_before_serving(monkeypatch):
    """`modulatio-api` must load the install-root + vault `.env` before
    serving — the same env-load contract cli.py gives every other entry
    point. Without it the server runs with an empty key environment and
    the Leader's chat runner 500s (live #6, 2026-07-07)."""
    import uvicorn

    from modulatio import config as config_mod
    from modulatio.web import server

    calls: list[str] = []
    monkeypatch.setattr(
        config_mod, "load_modulatio_env", lambda: calls.append("env")
    )
    monkeypatch.setattr(uvicorn, "run", lambda *a, **kw: calls.append("serve"))

    server.run([])

    assert calls == ["env", "serve"]


def test_run_default_port_is_8787(monkeypatch):
    from modulatio.web import server

    monkeypatch.setattr(server, "is_installed", lambda: True)
    monkeypatch.setattr(server.config, "load_modulatio_env", lambda: None)
    captured: dict = {}
    monkeypatch.setattr(
        "uvicorn.run", lambda app, **k: captured.update(k))
    server.run([])
    assert captured["port"] == 8787


def test_run_port_from_env_override_knob(monkeypatch):
    """The MODULATIO_WEB_PORT settings knob changes the default port so the
    operator can move the WebOS off an occupied port."""
    from modulatio.web import server

    monkeypatch.setattr(server, "is_installed", lambda: True)
    monkeypatch.setattr(server.config, "load_modulatio_env", lambda: None)
    monkeypatch.setenv("MODULATIO_WEB_PORT", "9001")
    captured: dict = {}
    monkeypatch.setattr("uvicorn.run", lambda app, **k: captured.update(k))
    server.run([])
    assert captured["port"] == 9001


def test_run_explicit_port_flag_wins_over_env(monkeypatch):
    from modulatio.web import server

    monkeypatch.setattr(server, "is_installed", lambda: True)
    monkeypatch.setattr(server.config, "load_modulatio_env", lambda: None)
    monkeypatch.setenv("MODULATIO_WEB_PORT", "9001")
    captured: dict = {}
    monkeypatch.setattr("uvicorn.run", lambda app, **k: captured.update(k))
    server.run(["--port", "8080"])
    assert captured["port"] == 8080


# ── /api/projects ─────────────────────────────────────────────────────


def test_projects_lists_real_projects_with_default(client):
    vault.init_project("alpha", "Alpha", "o")
    vault.init_project("beta", "Beta", "o")
    config.set_default_project_code("beta")

    resp = client.get("/api/projects")

    assert resp.status_code == 200
    body = resp.json()
    assert body["projects"] == ["alpha", "beta"]
    assert body["default"] == "beta"


def test_projects_empty_vault(client):
    resp = client.get("/api/projects")
    assert resp.status_code == 200
    assert resp.json() == {"projects": [], "default": None}


# ── bearer-token gate (the LAN red-line: no token, no non-loopback) ──


def test_api_requires_bearer_token_when_configured():
    from fastapi.testclient import TestClient

    from modulatio.web.app import create_app

    client = TestClient(create_app(bearer_token="s3cret"), base_url="http://localhost")

    assert client.get("/api/projects").status_code == 401
    ok = client.get("/api/projects", headers={"Authorization": "Bearer s3cret"})
    assert ok.status_code == 200


def test_static_shell_not_token_gated():
    """The SPA shell loads without auth (the pairing prompt lives in it);
    only /api and /events are gated."""
    from fastapi.testclient import TestClient

    from modulatio.web.app import create_app

    client = TestClient(create_app(bearer_token="s3cret"), base_url="http://localhost")
    assert client.get("/").status_code == 200


# ── CSRF guard (state-changing requests need the SPA's custom header) ──


def test_state_changing_request_requires_webos_header():
    """A bodyless cross-origin POST is a CORS 'simple request' (no preflight),
    so the JSON-content-type defense doesn't cover it. State-changing /api
    requests must carry the SPA's custom header; a page that can't set it (no
    preflight → blocked, no CORS middleware) can't drive the API."""
    from fastapi.testclient import TestClient

    from modulatio.web.app import create_app

    client = TestClient(create_app(), base_url="http://localhost")
    # Reads are unaffected.
    assert client.get("/api/projects").status_code == 200
    # A mutating request without the header is refused before routing.
    assert client.post("/api/web/cron/j/enable").status_code == 403
    assert client.delete("/api/web/tickets/T-1").status_code == 403
    # With the header it reaches routing (404 = no such job, not a CSRF block).
    ok = client.post("/api/web/cron/j/enable", headers={"X-Modulatio-WebOS": "1"})
    assert ok.status_code != 403


# ── DNS-rebinding guard ───────────────────────────────────────────────


def test_unknown_host_header_rejected():
    """A rebound hostname resolving to 127.0.0.1 must not reach the API:
    only loopback names (and an explicitly allowed host) are served."""
    from fastapi.testclient import TestClient

    from modulatio.web.app import create_app

    client = TestClient(create_app(), base_url="http://localhost")
    bad = client.get("/api/projects", headers={"Host": "evil.example.com"})
    assert bad.status_code == 400

    ok = client.get("/api/projects", headers={"Host": "localhost:8787"})
    assert ok.status_code == 200


def test_extra_allowed_host_is_served():
    from fastapi.testclient import TestClient

    from modulatio.web.app import create_app

    client = TestClient(create_app(allowed_hosts=["nautilus.lan"]))
    ok = client.get("/api/projects", headers={"Host": "nautilus.lan:8787"})
    assert ok.status_code == 200


# ── static SPA serving ────────────────────────────────────────────────


def test_index_served_at_root(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "MODULATIO" in resp.text


def test_shell_assets_serve(client):
    """The SPA shell's css/js modules ship and serve — both themes in the
    token sheet, the app module as ES module JS."""
    themes = client.get("/css/themes.css")
    assert themes.status_code == 200
    assert 'data-theme="atelier"' in themes.text
    assert 'data-theme="vellum"' in themes.text

    base = client.get("/css/base.css")
    assert base.status_code == 200

    app_js = client.get("/js/app.js")
    assert app_js.status_code == 200
    assert "javascript" in app_js.headers["content-type"]


def test_statics_forbid_heuristic_caching(client):
    """Every static response carries Cache-Control: no-cache — without it a
    browser's heuristic cache pins stale SPA modules for hours (surviving a
    full browser restart), so a shipped fix never reaches the operator. The
    ETag keeps revalidation a cheap 304."""
    for path in ("/", "/js/app.js", "/js/pages/config.js", "/css/base.css"):
        resp = client.get(path)
        assert resp.status_code == 200
        assert resp.headers.get("cache-control") == "no-cache", path
    assert "etag" in client.get("/js/app.js").headers  # revalidation stays cheap
