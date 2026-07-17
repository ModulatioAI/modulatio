# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Cowboy Claude and Clifton Knox.
"""WebOS event plumbing — ActivityEvent serialization, the per-project
bus, and the SSE endpoint."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

# The WebOS backend is the opt-in `[web]` extra; skip its suite cleanly
# when FastAPI isn't installed rather than erroring on a lean install.
pytest.importorskip("fastapi")

from modulatio.types import ActivityEvent  # noqa: E402 — after the extra guard


def _event(**over) -> ActivityEvent:
    base = dict(
        agent_id="randy",
        role="writer",
        phase="task_dispatched",
        task_id="T1",
        timestamp=datetime(2026, 7, 7, 12, 0, 0, tzinfo=timezone.utc),
        detail=None,
    )
    base.update(over)
    return ActivityEvent(**base)


# ── serialize ─────────────────────────────────────────────────────────


def test_event_to_json_is_json_safe():
    from modulatio.web.serialize import event_to_json

    data = event_to_json(_event())
    json.dumps(data)  # must round-trip
    assert data["agent_id"] == "randy"
    assert data["timestamp"] == "2026-07-07T12:00:00+00:00"


def test_event_to_json_scrubs_embedded_secrets_in_detail():
    from modulatio.web.serialize import event_to_json

    data = event_to_json(
        _event(detail={"note": "calling api_key=sk-live-abc123 now"})
    )
    assert "sk-live-abc123" not in json.dumps(data)


def test_event_to_json_scrubs_secret_shaped_dict_keys():
    """WB-2: a secret can ride in KEY position too — the scrub must reach
    keys, not just values."""
    from modulatio.web.serialize import event_to_json

    data = event_to_json(_event(detail={"api_key=sk-live-xyz789": "visible"}))
    assert "sk-live-xyz789" not in json.dumps(data)


def test_event_to_json_stringifies_unknown_detail_objects():
    from modulatio.web.serialize import event_to_json

    class Weird:
        def __repr__(self) -> str:
            return "Weird(token=xyz)"

    data = event_to_json(_event(detail=Weird()))
    json.dumps(data)


# ── bus ───────────────────────────────────────────────────────────────


def test_bus_replays_current_run_to_new_subscriber():
    from modulatio.web.events import EventBus

    bus = EventBus()
    bus.publish({"type": "event", "data": {"n": 0}})  # before subscribe → REPLAYED
    q = bus.subscribe()
    bus.publish({"type": "event", "data": {"n": 1}})  # live
    assert q.get(timeout=2) == {"type": "event", "data": {"n": 0}}  # replay first
    assert q.get(timeout=2) == {"type": "event", "data": {"n": 1}}  # then live
    bus.unsubscribe(q)
    bus.publish({"type": "event", "data": {"n": 2}})
    assert q.empty()  # unsubscribed → no more live frames


def test_run_started_resets_the_replay_buffer():
    from modulatio.web.events import EventBus

    bus = EventBus()
    bus.publish({"type": "event", "data": {"n": "old"}})  # a prior run's frame
    bus.publish({"type": "run_started", "data": {"run_id": "r2"}})
    bus.publish({"type": "event", "data": {"n": "new"}})
    q = bus.subscribe()
    # replay starts at the new run_started — the prior run's frame is gone
    assert q.get(timeout=2) == {"type": "run_started", "data": {"run_id": "r2"}}
    assert q.get(timeout=2) == {"type": "event", "data": {"n": "new"}}
    assert q.empty()


def test_telemetry_replays_latest_only():
    from modulatio.web.events import EventBus

    bus = EventBus()
    bus.publish({"type": "telemetry", "data": {"tokens": 10}})
    bus.publish({"type": "telemetry", "data": {"tokens": 20}})
    q = bus.subscribe()
    assert q.get(timeout=2) == {"type": "telemetry", "data": {"tokens": 20}}
    assert q.empty()  # the latest only, never a backlog of ticks


def test_replay_delivers_newest_frames_including_run_done():
    """A run longer than a fresh subscriber's queue depth must replay
    its NEWEST frames (the current burst + run_done) on reconnect — not the
    stale oldest that get iterated first and fill the queue, dropping run_done."""
    from modulatio.web.events import _SUBSCRIBER_DEPTH, EventBus

    bus = EventBus()
    bus.publish({"type": "run_started", "data": {"run_id": "r"}})
    for n in range(_SUBSCRIBER_DEPTH + 500):  # more activity than a queue holds
        bus.publish({"type": "event", "data": {"n": n}})
    bus.publish({"type": "run_done", "data": {"run_id": "r"}})  # MUST arrive
    q = bus.subscribe()
    drained = []
    while not q.empty():
        drained.append(q.get_nowait())
    types = [f["type"] for f in drained]
    assert "run_done" in types  # newest survives (the old oldest-first drop lost it)
    ns = [f["data"]["n"] for f in drained if f["type"] == "event"]
    assert ns and max(ns) == _SUBSCRIBER_DEPTH + 500 - 1  # newest activity, not oldest


def test_clear_replay_makes_the_clear_stick_across_resubscribe():
    """Clif 2026-07-09: clearing the TV then flipping tabs brought everything
    back — the DOM wipe was cosmetic while the replay buffer survived. CLEAR
    now drops the buffer: a re-subscribe repaints nothing cleared, but the
    latest telemetry (the rail, not the log) still arrives."""
    from modulatio.web.events import EventBus

    bus = EventBus()
    bus.publish({"type": "run_started", "data": {"run_id": "r"}})
    bus.publish({"type": "event", "data": {"n": 1}})
    bus.publish({"type": "telemetry", "data": {"tokens": 5}})
    bus.clear_replay()
    q = bus.subscribe()
    drained = []
    while not q.empty():
        drained.append(q.get_nowait())
    assert [f["type"] for f in drained] == ["telemetry"]  # log gone, rail kept
    bus.publish({"type": "event", "data": {"n": 2}})  # live frames still flow
    assert q.get(timeout=2) == {"type": "event", "data": {"n": 2}}


def test_get_bus_is_per_project_singleton():
    from modulatio.web.events import get_bus

    assert get_bus("alpha") is get_bus("alpha")
    assert get_bus("alpha") is not get_bus("beta")


# ── SSE endpoint ──────────────────────────────────────────────────────


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    from modulatio.web.app import create_app

    return TestClient(create_app(), base_url="http://localhost")


async def test_sse_stream_emits_hello_then_published_frames():
    """The generator is exercised directly: httpx's ASGI test transport
    buffers whole bodies, so an endless SSE body can't be consumed
    through TestClient (live-fire covers the wire end to end)."""
    from modulatio.web.events import get_bus
    from modulatio.web.routes.console import event_stream

    resp = await event_stream("alpha")
    assert resp.media_type == "text/event-stream"
    frames = resp.body_iterator

    hello = await frames.__anext__()
    assert hello.startswith("event: hello\n")

    get_bus("alpha").publish(
        {"type": "event", "data": {"phase": "qc_started"}}
    )
    frame = await frames.__anext__()
    name, payload = frame.split("\n")[0], json.loads(frame.split("\n")[1][6:])
    assert name == "event: event"
    assert payload == {"phase": "qc_started"}

    await frames.aclose()
    # After aclose the subscriber is gone — publishes go nowhere and the
    # bus doesn't accumulate for dead clients.
    get_bus("alpha").publish({"type": "event", "data": {"n": 2}})


def test_sse_rejects_invalid_project_code(client):
    resp = client.get("/api/BAD..CODE/events")
    assert resp.status_code in (404, 422)
