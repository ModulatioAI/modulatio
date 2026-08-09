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


def test_an_upload_handle_names_nothing_and_works_once(tmp_path, monkeypatch):
    """A client that can name a server-side file is the one choosing what gets
    read, so the reply is a token this store alone resolves. Claiming it twice
    finds nothing: bytes sent in one turn cannot be replayed into a later one."""
    import pytest

    from modulatio import config, uploads

    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path / "cfg")
    handle, shown = uploads.stage_upload(
        b"body bytes", display_name="../../etc/passwd", project="WEB")

    # The display name is a label, never a location.
    assert "/" not in shown and ".." not in shown

    staged, name = uploads.consume(handle, project="WEB")
    assert staged.read_bytes() == b"body bytes"
    assert name == shown
    # Single use.
    with pytest.raises(uploads.UploadRefused):
        uploads.consume(handle, project="WEB")


def test_a_handle_does_not_cross_into_another_project(tmp_path, monkeypatch):
    """A token is unguessable, but a leaked one must not reach a project its
    holder is not already working in. The refusal reads the same as an unknown
    handle — saying which case it was confirms the other handle exists."""
    import pytest

    from modulatio import config, uploads

    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path / "cfg")
    handle, _ = uploads.stage_upload(b"x", display_name="n.txt", project="WEB")

    with pytest.raises(uploads.UploadRefused) as wrong:
        uploads.consume(handle, project="OTHER")
    with pytest.raises(uploads.UploadRefused) as unknown:
        uploads.consume("not-a-real-handle", project="WEB")
    assert str(wrong.value) == str(unknown.value)

    # Refusing another project's claim leaves the real owner's upload intact.
    staged, _ = uploads.consume(handle, project="WEB")
    assert staged.read_bytes() == b"x"


def test_an_expired_upload_is_gone_from_disk_not_just_from_the_index(
        tmp_path, monkeypatch):
    """Bytes nobody claimed must not sit in the staging directory waiting to
    be. Dropping only the entry would leave the file with no owner to remove
    it."""
    import pytest

    from modulatio import config, uploads

    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path / "cfg")
    handle, _ = uploads.stage_upload(
        b"x", display_name="n.txt", project="WEB", ttl_s=-1)
    staged = tmp_path / "cfg" / "uploads"

    with pytest.raises(uploads.UploadRefused):
        uploads.consume(handle, project="WEB")
    assert not [p for p in staged.glob("*") if p.is_file()]


def test_uploads_waiting_for_one_project_are_bounded(tmp_path, monkeypatch):
    """A size cap alone bounds nothing: a client can hold unlimited staging
    space in pieces that each pass it."""
    import pytest

    from modulatio import config, uploads

    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path / "cfg")
    for i in range(uploads.DEFAULT_MAX_PENDING):
        uploads.stage_upload(b"x", display_name=f"{i}.txt", project="WEB")

    with pytest.raises(uploads.UploadRefused) as caught:
        uploads.stage_upload(b"x", display_name="one-too-many", project="WEB")
    assert "already waiting" in str(caught.value)

    # A different project is unaffected by another's backlog.
    uploads.stage_upload(b"x", display_name="fine.txt", project="OTHER")


def test_a_sent_turn_carries_the_uploaded_bytes_and_frees_them(
        client, tmp_path, monkeypatch):
    """The composer's bytes reach the turn through the same constructor a disk
    load uses, so an upload meets one policy rather than a second written for
    it. The upload's own copy does not outlive the turn that claimed it."""
    from modulatio import config, uploads
    from modulatio.web.routes import console as _console

    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path / "cfg")
    seen = {}

    class _Actor:
        def converse(self, text, *, attachments=None):
            seen["text"] = text
            seen["attachments"] = attachments or []
            return "ack"

    monkeypatch.setattr(_console, "_actor", lambda *a, **k: _Actor())
    handle, _ = uploads.stage_upload(
        b"the uploaded body\n", display_name="notes.md", project="web")

    resp = client.post("/api/web/converse",
                       json={"text": "look at this", "uploads": [handle]})
    assert resp.status_code == 200, resp.text

    att = seen["attachments"]
    assert [a.name for a in att] == ["notes.md"]
    assert att[0].content == "the uploaded body\n"
    assert att[0].sha256.startswith("sha256:")
    # The staged upload is released; the attachment's own snapshot is not.
    assert not [p for p in (tmp_path / "cfg" / "uploads").glob("*") if p.is_file()]
    assert att[0].staged_path.exists()


def test_a_turn_naming_an_unknown_upload_is_refused_not_sent_bare(
        client, tmp_path, monkeypatch):
    """Dropping a handle that will not resolve would send the turn without the
    file: the operator believes it went and the model answers as though nothing
    was offered, with neither able to tell."""
    from modulatio import config
    from modulatio.web.routes import console as _console

    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path / "cfg")
    called = {"n": 0}

    class _Actor:
        def converse(self, text, *, attachments=None):
            called["n"] += 1
            return "ack"

    monkeypatch.setattr(_console, "_actor", lambda *a, **k: _Actor())

    resp = client.post("/api/web/converse",
                       json={"text": "hi", "uploads": ["nope"]})
    assert resp.status_code == 404
    assert called["n"] == 0


def test_a_turn_carrying_only_a_file_is_accepted_but_an_empty_one_is_not(
        client, tmp_path, monkeypatch):
    """Showing a screenshot and asking nothing in particular is an ordinary
    way to start a turn, so words are not what makes one worth sending. A turn
    carrying neither words nor a file is the one with nothing in it."""
    from modulatio import config, uploads
    from modulatio.web.routes import console as _console

    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path / "cfg")
    seen = {}

    class _Actor:
        def converse(self, text, *, attachments=None):
            seen["text"] = text
            return "ack"

    monkeypatch.setattr(_console, "_actor", lambda *a, **k: _Actor())
    handle, _ = uploads.stage_upload(
        b"just this\n", display_name="n.md", project="web")

    assert client.post("/api/web/converse",
                       json={"text": "", "uploads": [handle]}).status_code == 200
    assert seen["text"] == ""
    assert client.post("/api/web/converse", json={"text": "  "}).status_code == 422


def test_an_uploads_modality_is_read_from_its_bytes(tmp_path):
    """A browser's declared type is the client's claim about bytes the engine
    is already holding, and reading them answers the same question without
    trusting it."""
    from modulatio.attachments import looks_like_image as _looks_like_image

    png = tmp_path / "a.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8)
    assert _looks_like_image(png)

    webp = tmp_path / "b.webp"
    webp.write_bytes(b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"\x00" * 4)
    assert _looks_like_image(webp)

    # Text named like an image is text; the name never decides.
    liar = tmp_path / "c.png"
    liar.write_bytes(b"just words in a file\n")
    assert not _looks_like_image(liar)
