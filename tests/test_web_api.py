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

    monkeypatch.setattr(server, "find_spec", lambda name: None)
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
