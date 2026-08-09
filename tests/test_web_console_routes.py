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
    return TestClient(create_app(stub=True), base_url="http://localhost",
                      headers={"X-Modulatio-WebOS": "1"})


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


def test_console_clear_drops_the_replay_buffer(client):
    """The CLEAR button's server half: POST console/clear empties the bus
    replay so a tab-return doesn't repaint the cleared log."""
    from modulatio.web.events import get_bus

    bus = get_bus("web")  # bus keys are the validated (lowercase) code
    bus.publish({"type": "event", "data": {"n": 1}})
    resp = client.post("/api/web/console/clear")
    assert resp.status_code == 200 and resp.json() == {"cleared": True}
    q = bus.subscribe()
    assert q.empty()  # nothing replays after the clear


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
    from modulatio import leader_gate as lg
    from modulatio.web.actors import get_actor

    broker = get_actor("web", stub=True).broker
    req = lg.SecurityRequest(
        action="write", resource="/home/user/notes", request_class="path",
        why="the Leader wants to edit your notes")
    results: list = []
    t = threading.Thread(target=lambda: results.append(broker.prompt(req)))
    t.start()
    # The frame carries the id; here we grab it from the broker's pending set.
    for _ in range(100):
        with broker._lock:
            pending = list(broker._pending)
        if pending:
            break
        threading.Event().wait(0.01)
    rid = pending[0]

    resp = client.post(f"/api/web/approvals/{rid}", json={"scope": "session"})
    assert resp.status_code == 200
    assert resp.json() == {"resolved": True}
    t.join(timeout=5)
    assert [d.scope for d in results] == ["session"]


def test_approval_unknown_id_404(client):
    resp = client.post("/api/web/approvals/deadbeef", json={"scope": "once"})
    assert resp.status_code == 404


def test_approval_invalid_scope_string_422(client):
    """The route validates the scope vocabulary; garbage never reaches the
    broker (clamping to available_scopes happens broker-side)."""
    resp = client.post("/api/web/approvals/deadbeef", json={"scope": "yes"})
    assert resp.status_code == 422


def test_routes_validate_project_code(client):
    assert client.post(
        "/api/..evil/converse", json={"text": "x"}
    ).status_code == 404
    assert client.get("/api/..evil/conversation").status_code == 404


def test_interrupt_converse_when_idle_reports_false(client):
    resp = client.post("/api/web/converse/interrupt")
    assert resp.status_code == 200
    assert resp.json() == {"interrupted": False}


def test_the_agents_listing_carries_the_name_a_seat_is_shown_by(client):
    """Every event carries a seat ID, and the name its operator gave it lives
    with the roster — so the console reads the listing to write a seat's name
    rather than its id. A seat whose id is its role would otherwise appear as
    the role word on every line."""
    from modulatio import roster

    roster.seed_default_roster(
        "web", leader_model="stub", coordinator_model="stub",
        producer_model="stub", qc_model="stub")
    resp = client.get("/api/web/config/agents")
    assert resp.status_code == 200
    agents = resp.json()["agents"]
    assert agents, "a seeded project lists its seats"
    for a in agents:
        assert a["id"] and a["name"], a
