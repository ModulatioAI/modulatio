# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Cowboy Claude and Clifton Knox.
"""Console routes — conversation history, converse, kickoff/stop,
approval decisions. All through the stub actor (real engine, stub
runners)."""

from __future__ import annotations

import threading

import pytest

# The WebOS backend is the opt-in `[web]` extra; skip its suite cleanly
# when FastAPI isn't installed rather than erroring on a lean install.
pytest.importorskip("fastapi")

from modulatio import vault  # noqa: E402 — after the extra guard


pytestmark = pytest.mark.usefixtures("fresh_web_registries")


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    from modulatio.web.app import create_app

    vault.init_project("web", "Web", "o")
    return TestClient(create_app(stub=True), base_url="http://localhost")


def test_converse_replies_and_history_shows_the_turns(client):
    resp = client.post("/api/web/converse", json={"text": "howdy leader"})
    assert resp.status_code == 200
    assert resp.json()["reply"]

    hist = client.get("/api/web/conversation")
    assert hist.status_code == 200
    turns = hist.json()["turns"]
    assert any(t["role"] == "operator" and "howdy leader" in t["content"]
               for t in turns)
    assert any(t["role"] == "leader" for t in turns)


def test_conversation_empty_when_no_thread(client):
    resp = client.get("/api/web/conversation")
    assert resp.status_code == 200
    assert resp.json() == {"turns": []}


def test_conversation_reset_archives_thread(client):
    client.post("/api/web/converse", json={"text": "note this"})
    resp = client.post("/api/web/conversation/reset")
    assert resp.status_code == 200
    assert resp.json()["archived"]
    assert client.get("/api/web/conversation").json() == {"turns": []}


def test_converse_empty_text_rejected(client):
    resp = client.post("/api/web/converse", json={"text": "   "})
    assert resp.status_code == 422


def test_kickoff_returns_run_id_and_second_is_409(client, monkeypatch):
    from modulatio.web import actors as actors_mod

    release = threading.Event()
    started = threading.Event()

    def slow_kickoff(self, objective, **kw):
        started.set()
        release.wait(timeout=30)

    monkeypatch.setattr(
        actors_mod.Orchestrator, "kickoff", slow_kickoff, raising=True
    )
    resp = client.post("/api/web/kickoff", json={"objective": "big job"})
    assert resp.status_code == 200
    run_id = resp.json()["run_id"]
    assert started.wait(timeout=10)

    busy = client.post("/api/web/kickoff", json={"objective": "again"})
    assert busy.status_code == 409
    assert busy.json()["detail"]["run_id"] == run_id

    stop = client.post("/api/web/stop")
    assert stop.status_code == 200
    assert stop.json() == {"stopped": True}
    release.set()

    from modulatio.web.actors import get_actor

    get_actor("web", stub=True).join_kickoff(timeout=10)


def test_stop_when_idle_reports_false(client):
    resp = client.post("/api/web/stop")
    assert resp.status_code == 200
    assert resp.json() == {"stopped": False}


def test_approval_decision_roundtrip(client):
    from modulatio.web.actors import get_actor

    broker = get_actor("web", stub=True).broker
    results: list[bool] = []
    t = threading.Thread(
        target=lambda: results.append(broker.request("run_shell", {"cmd": "ls"}))
    )
    t.start()
    # The frame carries the id; here we grab it from the broker's pending set.
    for _ in range(100):
        with broker._lock:
            pending = list(broker._pending)
        if pending:
            break
        threading.Event().wait(0.01)
    rid = pending[0]

    resp = client.post(f"/api/web/approvals/{rid}", json={"approve": True})
    assert resp.status_code == 200
    assert resp.json() == {"resolved": True}
    t.join(timeout=5)
    assert results == [True]


def test_approval_unknown_id_404(client):
    resp = client.post("/api/web/approvals/deadbeef", json={"approve": True})
    assert resp.status_code == 404


def test_routes_validate_project_code(client):
    assert client.post(
        "/api/..evil/converse", json={"text": "x"}
    ).status_code == 404
    assert client.get("/api/..evil/conversation").status_code == 404
