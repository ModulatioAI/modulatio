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
