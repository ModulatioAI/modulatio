# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Cowboy Claude and Clifton Knox.
"""WebOS CONFIG routes — the read/write configuration surface (Feature 2).
Each write binds the SAME engine seam the TUI Config screens use; secret
values are write-only and never cross the boundary out.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from modulatio import config, vault  # noqa: E402 — after the extra guard

pytestmark = pytest.mark.usefixtures("fresh_web_registries")


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    from modulatio.web.app import create_app

    vault.init_project("web", "Web", "o")
    return TestClient(create_app(stub=True), base_url="http://localhost",
                      headers={"X-Modulatio-WebOS": "1"})


# ── SETTINGS ──────────────────────────────────────────────────────────


def test_settings_list_knobs_with_value_and_source(client):
    resp = client.get("/api/settings")
    assert resp.status_code == 200
    knobs = resp.json()["knobs"]
    by_key = {k["key"]: k for k in knobs}
    assert "MODULATIO_TASK_MAX_RETRIES" in by_key
    row = by_key["MODULATIO_TASK_MAX_RETRIES"]
    assert row["label"] and row["default"] == "3" and row["hint"]
    assert row["source"] in ("default", "settings", "shell/.env")


def test_settings_set_and_clear(client):
    r = client.post("/api/settings/MODULATIO_TASK_MAX_RETRIES", json={"value": "2"})
    assert r.status_code == 200
    assert config._load_defaults()["env_overrides"]["MODULATIO_TASK_MAX_RETRIES"] == "2"

    d = client.delete("/api/settings/MODULATIO_TASK_MAX_RETRIES")
    assert d.status_code == 200
    assert "MODULATIO_TASK_MAX_RETRIES" not in (
        config._load_defaults().get("env_overrides") or {})


def test_settings_set_out_of_range_422(client):
    r = client.post("/api/settings/MODULATIO_TASK_MAX_RETRIES", json={"value": "99"})
    assert r.status_code == 422


def test_settings_set_unknown_knob_404(client):
    r = client.post("/api/settings/MODULATIO_NOPE", json={"value": "1"})
    assert r.status_code == 404


def test_settings_shell_owned_knob_refused(client, monkeypatch):
    monkeypatch.setenv("MODULATIO_QC_FIXER", "0")
    r = client.post("/api/settings/MODULATIO_QC_FIXER", json={"value": "1"})
    assert r.status_code == 409  # read-only, owned outside the app


# ── FOLDERS (management view — the operator's own registry) ────────────


def test_folders_add_list_and_remove(client, tmp_path):
    d = tmp_path / "contracts"
    d.mkdir()
    add = client.post("/api/config/folders",
                      json={"name": "contracts", "path": str(d), "mode": "rw"})
    assert add.status_code == 200
    rows = client.get("/api/config/folders").json()["folders"]
    row = next(r for r in rows if r["name"] == "contracts")
    assert row["mode"] == "rw" and row["path"] == str(d)

    rm = client.delete("/api/config/folders/contracts")
    assert rm.status_code == 200
    assert not any(r["name"] == "contracts"
                   for r in client.get("/api/config/folders").json()["folders"])


def test_folders_add_refuses_bad_path(client, tmp_path):
    # non-absolute
    assert client.post("/api/config/folders",
                       json={"name": "x", "path": "relative/dir", "mode": "rw"}
                       ).status_code == 422
    # unreachable
    assert client.post("/api/config/folders",
                       json={"name": "y", "path": str(tmp_path / "nope"), "mode": "rw"}
                       ).status_code == 422
    # a system root
    assert client.post("/api/config/folders",
                       json={"name": "z", "path": "/etc", "mode": "rw"}
                       ).status_code == 422


def test_folders_add_refuses_duplicate_name(client, tmp_path):
    a = tmp_path / "a"
    a.mkdir()
    b = tmp_path / "b"
    b.mkdir()
    client.post("/api/config/folders",
                json={"name": "dup", "path": str(a), "mode": "rw"})
    r = client.post("/api/config/folders",
                    json={"name": "dup", "path": str(b), "mode": "rw"})
    assert r.status_code == 422


def test_folders_set_output_only_output_mode(client, tmp_path):
    from modulatio import config

    out = tmp_path / "deliver"
    out.mkdir()
    client.post("/api/config/folders",
                json={"name": "deliver", "path": str(out), "mode": "output"})
    r = client.post("/api/config/folders/deliver/output")
    assert r.status_code == 200
    assert config.get_job_output_folder() == "deliver"


# ── PROJECTS (create / switch / delete with guards) ───────────────────


def test_project_create_switch_delete(client):
    from modulatio import config, vault

    assert client.post("/api/projects",
                       json={"code": "beta", "objective": "test"}).status_code == 200
    assert "beta" in vault.list_projects()
    assert client.post("/api/projects/beta/switch").status_code == 200
    assert config.get_default_project_code() == "beta"
    # switch away so beta isn't active, then delete
    client.post("/api/projects/web/switch")
    assert client.delete("/api/projects/beta").status_code == 200
    assert "beta" not in vault.list_projects()


def test_project_delete_refuses_active(client):
    from modulatio import config, vault

    vault.init_project("gamma", "G", "o")
    config.set_default_project_code("gamma")
    r = client.delete("/api/projects/gamma")
    assert r.status_code == 409  # active — switch away first
    assert "gamma" in vault.list_projects()


def test_project_delete_refuses_while_in_flight(client, monkeypatch):
    from modulatio import config, vault
    from modulatio.web.actors import get_actor

    vault.init_project("delta", "D", "o")
    config.set_default_project_code("web")  # delta not active
    monkeypatch.setattr(get_actor("delta", stub=True), "kickoff_active", lambda: True)
    r = client.delete("/api/projects/delta")
    assert r.status_code == 409
    assert "delta" in vault.list_projects()
