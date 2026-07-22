# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Cowboy Claude and Clifton Knox.
"""The per-project OrchestratorActor — converse, single-flight kickoff,
stop, and the fail-closed approval broker."""

from __future__ import annotations

import threading

import pytest

from modulatio import vault
from modulatio.web.events import get_bus


pytestmark = pytest.mark.usefixtures("fresh_web_registries")


@pytest.fixture()
def actor():
    from modulatio.web.actors import OrchestratorActor

    vault.init_project("web", "Web", "o")
    return OrchestratorActor("web", stub=True)


# ── converse ──────────────────────────────────────────────────────────


def test_converse_returns_reply_and_persists_thread(actor):
    reply = actor.converse("hello leader")
    assert isinstance(reply, str) and reply
    # The turn is durable — the conversation file carries operator + leader.
    convo = vault.project_dir("web") / "leader_conversation.jsonl"
    text = convo.read_text(encoding="utf-8")
    assert "hello leader" in text


# ── kickoff single-flight ─────────────────────────────────────────────


def test_kickoff_returns_run_id_and_completes(actor):
    run_id = actor.kickoff("write a haiku")
    assert run_id in vault.list_runs("web")
    actor.join_kickoff(timeout=30)
    assert not actor.kickoff_active()


def test_second_kickoff_while_running_raises_busy(actor, monkeypatch):
    from modulatio.web import actors as actors_mod
    from modulatio.web.actors import KickoffBusy

    release = threading.Event()
    started = threading.Event()

    def slow_kickoff(self, objective, **kw):
        started.set()
        release.wait(timeout=30)

    monkeypatch.setattr(
        actors_mod.Orchestrator, "kickoff", slow_kickoff, raising=True
    )
    first = actor.kickoff("long job")
    assert started.wait(timeout=10)
    with pytest.raises(KickoffBusy) as exc:
        actor.kickoff("second job")
    assert exc.value.run_id == first
    release.set()
    actor.join_kickoff(timeout=10)


def test_stop_sets_abort_on_live_kickoff(actor, monkeypatch):
    from modulatio.web import actors as actors_mod

    started = threading.Event()
    aborted = threading.Event()

    def waiting_kickoff(self, objective, **kw):
        started.set()
        if self.abort_event.wait(timeout=30):
            aborted.set()

    monkeypatch.setattr(
        actors_mod.Orchestrator, "kickoff", waiting_kickoff, raising=True
    )
    actor.kickoff("stoppable job")
    assert started.wait(timeout=10)
    assert actor.stop() is True
    assert aborted.wait(timeout=10)
    actor.join_kickoff(timeout=10)


def test_stop_with_no_live_kickoff_returns_false(actor):
    assert actor.stop() is False


# ── events reach the bus ──────────────────────────────────────────────


def test_kickoff_publishes_run_started_events_and_run_done(actor):
    q = get_bus("web").subscribe()
    try:
        run_id = actor.kickoff("emit some events")
        actor.join_kickoff(timeout=30)
        frames = []
        while not q.empty():
            frames.append(q.get_nowait())
        types = [f["type"] for f in frames]
        assert types[0] == "run_started"
        assert frames[0]["data"]["run_id"] == run_id
        assert "event" in types  # engine ActivityEvents flowed through
        assert types[-1] == "run_done"
        assert frames[-1]["data"]["run_id"] == run_id
    finally:
        get_bus("web").unsubscribe(q)


def test_kickoff_publishes_telemetry_frames(actor):
    q = get_bus("web").subscribe()
    try:
        actor.kickoff("telemetry please")
        actor.join_kickoff(timeout=30)
        frames = []
        while not q.empty():
            frames.append(q.get_nowait())
        telemetry = [f for f in frames if f["type"] == "telemetry"]
        assert telemetry, "at least one telemetry frame per run"
        data = telemetry[-1]["data"]
        for key in ("elapsed_s", "tasks_total", "tasks_done", "pct",
                    "qc_rejected", "tokens", "compressions"):
            assert key in data
    finally:
        get_bus("web").unsubscribe(q)


# ── approval broker (fail-closed) ─────────────────────────────────────


def _security_request(**over):
    from modulatio import leader_gate as lg

    base = dict(
        action="write", resource="/home/user/notes", request_class="path",
        why="the Leader wants to edit your notes",
    )
    base.update(over)
    return lg.SecurityRequest(**base)


def test_approval_prompt_carries_request_and_returns_scope():
    from modulatio.web.actors import ApprovalBroker

    bus_q = get_bus("web").subscribe()
    broker = ApprovalBroker("web", timeout_s=10)
    results: list = []

    t = threading.Thread(
        target=lambda: results.append(broker.prompt(_security_request()))
    )
    t.start()
    frame = bus_q.get(timeout=5)
    assert frame["type"] == "approval_request"
    data = frame["data"]
    # The engine-rendered request reaches the modal whole — resource, why,
    # and the scopes this class may offer (the TUI-parity contract).
    assert data["action"] == "write"
    assert data["resource"] == "/home/user/notes"
    assert data["why"]
    assert data["available_scopes"] == ["once", "session", "always", "deny"]
    assert broker.resolve(data["id"], "session") is True
    t.join(timeout=5)
    assert [d.scope for d in results] == ["session"]
    get_bus("web").unsubscribe(bus_q)


def test_approval_times_out_to_deny():
    from modulatio.web.actors import ApprovalBroker

    broker = ApprovalBroker("web", timeout_s=0.05)
    assert broker.prompt(_security_request()).scope == "deny"


def test_approval_scope_outside_available_denies():
    """A scope the request never offered can't be granted from the browser —
    clamped to deny BEFORE it reaches gate.decide (which would raise)."""
    from modulatio.web.actors import ApprovalBroker

    bus_q = get_bus("web").subscribe()
    broker = ApprovalBroker("web", timeout_s=10)
    results: list = []
    req = _security_request(available_scopes=("once", "deny"))

    t = threading.Thread(target=lambda: results.append(broker.prompt(req)))
    t.start()
    frame = bus_q.get(timeout=5)
    rid = frame["data"]["id"]
    assert frame["data"]["available_scopes"] == ["once", "deny"]
    assert broker.resolve(rid, "always") is True  # lands, but…
    t.join(timeout=5)
    assert [d.scope for d in results] == ["deny"]  # …clamped, fail-closed
    get_bus("web").unsubscribe(bus_q)


def test_approval_bogus_scope_denies():
    from modulatio.web.actors import ApprovalBroker

    bus_q = get_bus("web").subscribe()
    broker = ApprovalBroker("web", timeout_s=10)
    results: list = []

    t = threading.Thread(target=lambda: results.append(broker.prompt(_security_request())))
    t.start()
    rid = bus_q.get(timeout=5)["data"]["id"]
    assert broker.resolve(rid, "sudo-everything") is True
    t.join(timeout=5)
    assert [d.scope for d in results] == ["deny"]
    get_bus("web").unsubscribe(bus_q)


def test_approval_resolve_unknown_id_is_false():
    from modulatio.web.actors import ApprovalBroker

    assert ApprovalBroker("web", timeout_s=1).resolve("nope", "once") is False


# ── registry ──────────────────────────────────────────────────────────


def test_get_actor_is_per_project_singleton():
    from modulatio.web.actors import get_actor

    vault.init_project("web", "Web", "o", exist_ok=True)
    a = get_actor("web", stub=True)
    assert get_actor("web", stub=True) is a


def test_approval_resolution_publishes_resolved_frame():
    """However the ask ends, the broker announces it so the bus prunes the
    replay (no ghost modal) and open tabs drop the dead dialog."""
    from modulatio.web.actors import ApprovalBroker

    bus_q = get_bus("web").subscribe()
    broker = ApprovalBroker("web", timeout_s=10)
    results: list = []
    t = threading.Thread(
        target=lambda: results.append(broker.prompt(_security_request()))
    )
    t.start()
    rid = bus_q.get(timeout=5)["data"]["id"]
    assert broker.resolve(rid, "session") is True
    t.join(timeout=5)
    assert bus_q.get(timeout=5) == {"type": "approval_resolved", "data": {"id": rid}}
    get_bus("web").unsubscribe(bus_q)


def test_approval_timeout_publishes_resolved_frame():
    from modulatio.web.actors import ApprovalBroker

    bus_q = get_bus("web").subscribe()
    broker = ApprovalBroker("web", timeout_s=0.05)
    assert broker.prompt(_security_request()).scope == "deny"
    req = bus_q.get(timeout=5)
    assert req["type"] == "approval_request"
    assert bus_q.get(timeout=5) == {
        "type": "approval_resolved", "data": {"id": req["data"]["id"]},
    }
    get_bus("web").unsubscribe(bus_q)


def test_converse_supplies_capability_ask_via_the_ticket_bridge(actor, monkeypatch):
    """Broker wiring: the web actor passes ask= (the broker's
    capability surface) adapted over its EXISTING approval-ticket prompt_fn —
    so default/goal modes can actually ask for shell/network in the browser
    instead of denying without a prompt . One approval UI, both
    axes."""
    captured = {}

    class _Orch:
        def converse(self, message, **kw):
            captured.update(kw)
            return "ok"

        def session_mode_value(self):
            return "default"

    monkeypatch.setattr(actor, "_ensure_converse_orch", lambda: _Orch())
    actor.converse("hi")
    assert captured.get("prompt_fn") == actor.broker.prompt
    ask = captured.get("ask")
    assert ask is not None
    # The ask rides the SAME bridge: driving it fires an approval ticket
    # through broker.prompt (answered deny here via the zero-timeout path).
    from modulatio.permissions import Decision, capability_for
    monkeypatch.setattr(actor.broker, "_timeout_s", 0.01)
    assert ask(capability_for("run_shell", {"cmd": "ls"})) is Decision.DENY
