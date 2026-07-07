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

from modulatio import config, vault


@pytest.fixture()
def client():
    """A TestClient over a freshly-created app (isolated tmp vault)."""
    from fastapi.testclient import TestClient

    from modulatio.web.app import create_app

    return TestClient(create_app())


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

    client = TestClient(create_app(bearer_token="s3cret"))

    assert client.get("/api/projects").status_code == 401
    ok = client.get("/api/projects", headers={"Authorization": "Bearer s3cret"})
    assert ok.status_code == 200


def test_static_shell_not_token_gated():
    """The SPA shell loads without auth (the pairing prompt lives in it);
    only /api and /events are gated."""
    from fastapi.testclient import TestClient

    from modulatio.web.app import create_app

    client = TestClient(create_app(bearer_token="s3cret"))
    assert client.get("/").status_code == 200


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
