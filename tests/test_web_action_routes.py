# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Cowboy Claude and Clifton Knox.
"""WebOS action routes — the mutating page verbs (cron toggle/run/remove,
ticket + job + skill + artifact delete, skill/memory create, JT kickoff/
schedule, log send/delete, docs update, folder-targeted export). Each binds
an existing engine seam; destructive ones are exercised against the real
isolated vault, never a mock of the engine.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from modulatio import vault  # noqa: E402 — after the extra guard

pytestmark = pytest.mark.usefixtures("fresh_web_registries")


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    from modulatio.web.app import create_app

    vault.init_project("web", "Web", "o")
    return TestClient(create_app(stub=True), base_url="http://localhost")


# ── cron ──────────────────────────────────────────────────────────────


def _add_cron() -> str:
    from modulatio import cron

    job = cron.add(name="nightly", schedule="6h", project_code="web",
                   objective="tidy up")
    return job["id"]


def test_cron_disable_then_enable(client):
    from modulatio import cron

    jid = _add_cron()
    assert client.post(f"/api/web/cron/{jid}/disable").status_code == 200
    assert cron.get(jid)["enabled"] is False
    assert client.post(f"/api/web/cron/{jid}/enable").status_code == 200
    assert cron.get(jid)["enabled"] is True


def test_cron_run_now(client):
    jid = _add_cron()
    resp = client.post(f"/api/web/cron/{jid}/run-now")
    assert resp.status_code == 200


def test_cron_remove(client):
    from modulatio import cron

    jid = _add_cron()
    assert client.delete(f"/api/web/cron/{jid}").status_code == 200
    assert cron.get(jid) is None


def test_cron_unknown_job_404(client):
    assert client.post("/api/web/cron/nope/enable").status_code == 404
    assert client.delete("/api/web/cron/nope").status_code == 404


# ── tickets ───────────────────────────────────────────────────────────


def test_ticket_delete(client):
    from uuid import uuid4

    from modulatio import store
    from modulatio.types import TicketPriority

    t = store.create_ticket(
        project_id=uuid4(), project_code="web",
        priority=TicketPriority.MINOR, title="ship it?", body="?")
    assert client.delete(f"/api/web/tickets/{t.id}").status_code == 200
    assert store.get_ticket("web", t.id) is None


def test_ticket_delete_unknown_404(client):
    assert client.delete("/api/web/tickets/T-999").status_code == 404


def test_ticket_delete_validates_id(client):
    # A traversal / slash-bearing id is refused before it can reach the
    # delete seam (Starlette 405 on the collapsed path; the registry-name
    # validator is the belt-and-suspenders for anything that routes through).
    assert client.delete("/api/web/tickets/..%2Fetc").status_code >= 400
    assert client.delete("/api/web/tickets/bad%2Fname").status_code >= 400


# ── jobs ──────────────────────────────────────────────────────────────


@pytest.fixture()
def finished_run(client) -> str:
    from modulatio.web.actors import get_actor

    actor = get_actor("web", stub=True)
    run_id = actor.kickoff("produce a small artifact")
    actor.join_kickoff(timeout=60)
    return run_id


def test_job_delete(client, finished_run):
    assert client.delete(f"/api/web/runs/{finished_run}").status_code == 200
    assert not vault.run_dir("web", finished_run).exists()


def test_job_delete_unknown_404(client):
    assert client.delete("/api/web/runs/20260101T000000Z-deadbe").status_code == 404


def test_job_delete_validates_run_id(client):
    assert client.delete("/api/web/runs/..%2F..%2Fetc").status_code >= 400
    assert client.delete("/api/web/runs/bad%2Fid").status_code >= 400


# ── docs ──────────────────────────────────────────────────────────────


def test_docs_update(client, monkeypatch):
    from modulatio import docs

    monkeypatch.setattr(docs, "update_docs", lambda *a, **k: "Docs up to date.")
    resp = client.post("/api/docs/update")
    assert resp.status_code == 200
    assert resp.json()["status"] == "Docs up to date."


# ── logs ──────────────────────────────────────────────────────────────


def _write_log() -> str:
    from modulatio import logstore

    logstore.write_error_log("web boom", context={"surface": "test"})
    return logstore.list_logs()[0].id  # newest first


def test_log_delete(client):
    from modulatio import logstore

    lid = _write_log()
    assert client.delete(f"/api/logs/{lid}").status_code == 200
    assert logstore.find_log(lid) is None


def test_log_delete_unknown_404(client):
    assert client.delete("/api/logs/nope").status_code == 404


def test_log_send_returns_issue_url_and_marks_sent(client):
    from modulatio import logstore

    lid = _write_log()
    resp = client.post(f"/api/logs/{lid}/send")
    assert resp.status_code == 200
    url = resp.json()["url"]
    assert url.startswith("https://github.com/") and "issues/new" in url
    assert logstore.find_log(lid).sent


def test_log_send_unknown_404(client):
    assert client.post("/api/logs/nope/send").status_code == 404


# ── skills ────────────────────────────────────────────────────────────


def test_skill_create_then_delete(client):
    from modulatio import skills

    resp = client.post("/api/web/skills", json={
        "name": "web-made-skill",
        "description": "made from the browser",
        "prompt_template": "Do the thing about {topic}.",
    })
    assert resp.status_code == 200
    assert "web-made-skill" in skills.list_skills("web")

    assert client.delete("/api/web/skills/web-made-skill").status_code == 200
    assert "web-made-skill" not in skills.list_skills("web")


def test_skill_create_rejects_blank_name(client):
    resp = client.post("/api/web/skills", json={
        "name": "", "description": "x", "prompt_template": "y"})
    assert resp.status_code == 422


def test_skill_delete_validates_name(client):
    assert client.delete("/api/web/skills/bad%2Fname").status_code >= 400


# ── memory ────────────────────────────────────────────────────────────


def _seed_agent(agent_id: str = "scout") -> None:
    from modulatio import roster

    roster.add_agent(
        project_code="web", agent_id=agent_id, name=agent_id.title(),
        identity="researcher", skills=["research"])


def test_memory_add_semantic(client):
    from modulatio.memory import agent_memory

    _seed_agent()
    resp = client.post("/api/web/memory/agent/scout",
                       json={"content": "always verify sources"})
    assert resp.status_code == 200
    got = agent_memory.get_semantic("scout", project_code="web", limit=10)
    assert any(e.content == "always verify sources" for e in got)


def test_memory_edit_and_delete_agent_entry(client):
    from modulatio.memory import agent_memory

    _seed_agent()
    e = agent_memory.add_semantic("scout", "draft note", project_code="web")
    # edit
    r = client.put(f"/api/web/memory/agent/scout/{e.id}",
                   json={"layer": "semantic", "content": "revised note"})
    assert r.status_code == 200
    got = agent_memory.get_semantic("scout", project_code="web", limit=10)
    assert any(x.content == "revised note" for x in got)
    # delete
    d = client.request(
        "DELETE", f"/api/web/memory/agent/scout/{e.id}",
        params={"layer": "semantic"})
    assert d.status_code == 200
    got = agent_memory.get_semantic("scout", project_code="web", limit=10)
    assert not any(x.id == e.id for x in got)


def test_memory_propose_then_approve(client):
    from modulatio.memory import team_memory

    r = client.post("/api/web/memory/propose",
                    json={"body": "cite the field floor, not the lab limit"})
    assert r.status_code == 200
    props = team_memory.list_proposals("web")
    assert len(props) == 1
    pid = props[0].proposal_id
    a = client.post(f"/api/web/memory/proposals/{pid}/approve")
    assert a.status_code == 200
    assert any("field floor" in e.body for e in team_memory.list_entries("web"))


def test_memory_reject_proposal(client):
    from modulatio.memory import team_memory

    client.post("/api/web/memory/propose", json={"body": "throwaway"})
    pid = team_memory.list_proposals("web")[0].proposal_id
    r = client.post(f"/api/web/memory/proposals/{pid}/reject")
    assert r.status_code == 200
    assert not team_memory.list_proposals("web")


def test_memory_add_blank_rejected(client):
    _seed_agent()
    assert client.post("/api/web/memory/agent/scout",
                       json={"content": "  "}).status_code == 422


# ── JT library ────────────────────────────────────────────────────────


def _seed_jt(name: str = "weekly-report") -> None:
    from modulatio import job_templates as jt

    jt.save(jt.JobTemplate(
        name=name, description="a weekly report",
        interview_body="# Interview\nGather the numbers."), project_code="web")


def test_jt_schedule_creates_bound_cron(client):
    from modulatio import cron

    _seed_jt()
    resp = client.post("/api/web/jts/weekly-report/schedule",
                       json={"schedule": "7d"})
    assert resp.status_code == 200
    jobs = cron.list_jobs(project_code="web")
    assert any(j.get("jt_id") == "weekly-report" for j in jobs)


def test_jt_schedule_unknown_template_400(client):
    resp = client.post("/api/web/jts/nope/schedule", json={"schedule": "7d"})
    assert resp.status_code == 400


def test_jt_kickoff_unknown_template_404(client):
    assert client.post("/api/web/jts/nope/kickoff").status_code == 404


def test_jt_kickoff_launches_run(client):
    from modulatio.web.actors import get_actor

    _seed_jt()
    resp = client.post("/api/web/jts/weekly-report/kickoff")
    assert resp.status_code == 200
    assert resp.json()["run_id"]
    get_actor("web", stub=True).join_kickoff(timeout=60)


# ── artifacts ─────────────────────────────────────────────────────────


def test_artifact_delete(client):
    art = vault.project_dir("web") / "artifacts" / "scratch.md"
    art.write_text("throwaway\n", encoding="utf-8")
    resp = client.request("DELETE", "/api/web/artifacts",
                          params={"path": "artifacts/scratch.md"})
    assert resp.status_code == 200
    assert not art.exists()


def test_artifact_delete_traversal_refused(client):
    resp = client.request("DELETE", "/api/web/artifacts",
                          params={"path": "../../etc/passwd"})
    assert resp.status_code == 404


def test_artifact_export_to_registered_folder(client, tmp_path):
    from modulatio import config

    art = vault.project_dir("web") / "artifacts" / "report.md"
    art.write_text("# Report\n\nBody.\n", encoding="utf-8")
    dest = tmp_path / "share"
    dest.mkdir()
    config.save_folders([
        {"name": "share", "path": str(dest), "mode": "rw", "kind": "path"}])

    resp = client.post("/api/web/artifacts/export", json={
        "path": "artifacts/report.md", "format": "markdown", "folder": "share"})
    assert resp.status_code == 200
    assert (dest / "report.md").exists()


def test_artifact_export_rejects_unregistered_folder(client):
    art = vault.project_dir("web") / "artifacts" / "report.md"
    art.write_text("# Report\n", encoding="utf-8")
    resp = client.post("/api/web/artifacts/export", json={
        "path": "artifacts/report.md", "format": "markdown", "folder": "ghost"})
    assert resp.status_code == 404


# ── jobs reveal ───────────────────────────────────────────────────────


def test_job_reveal_returns_path(client, finished_run, monkeypatch):
    import modulatio.web.routes.actions as actions_mod

    monkeypatch.setattr(actions_mod.subprocess, "Popen", lambda *a, **k: None)
    resp = client.post(f"/api/web/runs/{finished_run}/reveal")
    assert resp.status_code == 200
    assert finished_run in resp.json()["path"]
