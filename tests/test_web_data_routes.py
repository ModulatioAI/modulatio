# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Cowboy Claude and Clifton Knox.
"""Read-only MasterDetail data routes — every endpoint a direct seam
binding, exercised against the real vault (populated by a stub kickoff
where run-scoped data is needed)."""

from __future__ import annotations

import pytest

from modulatio import vault


@pytest.fixture(autouse=True)
def _fresh_web_registries():
    from modulatio.web import actors, events

    actors._actors.clear()
    events._buses.clear()
    yield
    actors._actors.clear()
    events._buses.clear()


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    from modulatio.web.app import create_app

    vault.init_project("web", "Web", "o")
    return TestClient(create_app(stub=True))


@pytest.fixture()
def finished_run(client) -> str:
    """A completed stub run — real runs/tasks/goals on disk."""
    from modulatio.web.actors import get_actor

    actor = get_actor("web", stub=True)
    run_id = actor.kickoff("produce a small artifact")
    actor.join_kickoff(timeout=60)
    return run_id


# ── runs / jobs ───────────────────────────────────────────────────────


def test_runs_list_newest_first_with_sizes(client, finished_run):
    resp = client.get("/api/web/runs")
    assert resp.status_code == 200
    runs = resp.json()["runs"]
    assert runs[0]["run_id"] == finished_run
    assert runs[0]["size"] > 0
    assert runs[0]["size_human"]


def test_run_detail_objective_and_counts(client, finished_run):
    resp = client.get(f"/api/web/runs/{finished_run}")
    assert resp.status_code == 200
    body = resp.json()
    assert "produce a small artifact" in body["objective"]
    assert set(vault.RUN_SUBDIRS) <= set(body["counts"])


def test_run_tasks_and_goals_serialize(client, finished_run):
    tasks = client.get(f"/api/web/runs/{finished_run}/tasks")
    goals = client.get(f"/api/web/runs/{finished_run}/goals")
    assert tasks.status_code == 200 and goals.status_code == 200
    assert isinstance(tasks.json()["tasks"], list)
    assert goals.json()["goals"], "a stub kickoff decomposes at least one goal"
    g = goals.json()["goals"][0]
    assert g["id"] and g["status"]


def test_run_detail_unknown_run_404(client):
    assert client.get("/api/web/runs/20990101T000000Z-ffffff").status_code == 404


def test_run_id_traversal_rejected(client):
    assert client.get("/api/web/runs/..%2F..%2Fetc").status_code in (404, 422)


# ── tickets ───────────────────────────────────────────────────────────


def test_tickets_list(client):
    from uuid import uuid4

    from modulatio import store
    from modulatio.types import TicketPriority

    # Create through the store seam the engine itself uses.
    store.create_ticket(
        project_id=uuid4(),
        project_code="web",
        priority=TicketPriority.MINOR,
        title="web ticket",
        body="from the web test",
    )
    resp = client.get("/api/web/tickets")
    assert resp.status_code == 200
    tickets = resp.json()["tickets"]
    assert any(t["title"] == "web ticket" for t in tickets)


# ── jt library / skills / docs ────────────────────────────────────────


def test_jts_index_and_checkout(client):
    resp = client.get("/api/web/jts")
    assert resp.status_code == 200
    jts = resp.json()["jts"]
    assert jts, "bundled seed JTs exist"
    name = jts[0]["name"]
    one = client.get(f"/api/web/jts/{name}")
    assert one.status_code == 200
    assert one.json()["name"] == name


def test_jt_unknown_404_and_bad_name_rejected(client):
    assert client.get("/api/web/jts/no-such-jt-anywhere").status_code == 404
    assert client.get("/api/web/jts/..evil").status_code in (404, 422)


def test_skills_list_and_detail(client):
    resp = client.get("/api/web/skills")
    assert resp.status_code == 200
    skills = resp.json()["skills"]
    assert skills, "bundled seed skills exist"
    one = client.get(f"/api/web/skills/{skills[0]}")
    assert one.status_code == 200
    assert one.json()["name"] == skills[0]
    assert "body" in one.json()


def test_docs_list_and_read(client):
    resp = client.get("/api/docs")
    assert resp.status_code == 200
    docs = resp.json()["docs"]
    assert docs
    slug = docs[0]["slug"]
    page = client.get(f"/api/docs/{slug}")
    assert page.status_code == 200
    assert page.json()["markdown"]


# ── memory / cron / logs ──────────────────────────────────────────────


def test_memory_entries_and_proposals_empty_ok(client):
    resp = client.get("/api/web/memory")
    assert resp.status_code == 200
    assert resp.json() == {"entries": [], "proposals": []}


def test_cron_list(client):
    from modulatio import cron

    cron.add(
        name="nightly", schedule="6h", project_code="web",
        objective="tidy up",
    )
    resp = client.get("/api/web/cron")
    assert resp.status_code == 200
    jobs = resp.json()["jobs"]
    assert any(j["name"] == "nightly" for j in jobs)


def test_logs_list_no_paths_exposed(client):
    from modulatio import logstore

    logstore.write_error_log("web test error", context={"surface": "test"})
    resp = client.get("/api/logs")
    assert resp.status_code == 200
    logs = resp.json()["logs"]
    assert logs and logs[0]["kind"]
    assert "path" not in logs[0], "filesystem layout never crosses the boundary"


# ── artifacts ─────────────────────────────────────────────────────────


def test_artifacts_walk_and_preview(client):
    target = vault.project_dir("web") / "artifacts" / "note.md"
    target.write_text("# hello from the vault\n", encoding="utf-8")

    resp = client.get("/api/web/artifacts")
    assert resp.status_code == 200
    files = resp.json()["files"]
    entry = next(f for f in files if f["path"].endswith("note.md"))
    assert entry["family_glyph"]

    prev = client.get("/api/web/artifacts/preview", params={"path": entry["path"]})
    assert prev.status_code == 200
    assert "hello from the vault" in prev.json()["text"]


def test_artifact_preview_traversal_and_dotfile_refused(client):
    assert client.get(
        "/api/web/artifacts/preview", params={"path": "../../../etc/passwd"}
    ).status_code in (400, 404)
    secret = vault.project_dir("web") / "artifacts" / ".env"
    secret.write_text("API_KEY=oops", encoding="utf-8")
    assert client.get(
        "/api/web/artifacts/preview", params={"path": "artifacts/.env"}
    ).status_code in (400, 404)
