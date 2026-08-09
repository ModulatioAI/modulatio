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
    """A secret can ride in KEY position too — the scrub must reach
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
    # The version stamp rides the hello: engine (this process) + disk (read
    # at call time) + the stale verdict — the reconnect after a reinstall is
    # the one that reports the skew.
    hd = json.loads(hello.split("\n")[1][6:])
    from modulatio import __version__
    assert hd["engine"] == __version__
    assert "disk" in hd and hd["stale"] in (True, False)
    assert hd["stale"] is (hd["disk"] is not None and hd["disk"] != __version__)

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


def test_resolved_approval_never_replays_a_pending_one_does():
    """A modal is a one-shot interaction, not view state: a PENDING ask still
    replays to a late-opening tab, but once resolved (grant/deny/timeout) a
    reconnect must never re-pop the dead dialog (the ghost-modal bug)."""
    from modulatio.web.events import EventBus

    bus = EventBus()
    bus.publish({"type": "event", "data": {"n": 0}})
    bus.publish({"type": "approval_request", "data": {"id": "a1"}})
    q1 = bus.subscribe()  # pending → the ask replays
    assert q1.get(timeout=2)["type"] == "event"
    assert q1.get(timeout=2) == {"type": "approval_request", "data": {"id": "a1"}}
    bus.publish({"type": "approval_resolved", "data": {"id": "a1"}})
    # live subscribers see the resolution (closes an open dialog)…
    assert q1.get(timeout=2) == {"type": "approval_resolved", "data": {"id": "a1"}}
    # …but a reconnect sees neither the dead ask nor the resolution.
    q2 = bus.subscribe()
    assert q2.get(timeout=2)["type"] == "event"
    assert q2.empty()


def test_pending_approval_survives_run_started_and_clear_screen():
    """A pending ask is operator-interaction state, not run
    history — run_started resets and the operator's clear-screen must not
    erase a live ask a reconnecting tab still needs to answer."""
    from modulatio.web.events import EventBus

    bus = EventBus()
    bus.publish({"type": "approval_request", "data": {"id": "a1"}})
    bus.publish({"type": "run_started", "data": {"run_id": "r1"}})
    q = bus.subscribe()
    frames = [q.get(timeout=2), q.get(timeout=2)]
    assert {"type": "approval_request", "data": {"id": "a1"}} in frames
    bus.unsubscribe(q)
    bus.clear_replay()
    q2 = bus.subscribe()
    assert q2.get(timeout=2)["type"] == "approval_request"
    bus.unsubscribe(q2)
    bus.publish({"type": "approval_resolved", "data": {"id": "a1"}})
    q3 = bus.subscribe()
    assert q3.empty()
    bus.unsubscribe(q3)


def test_pending_approvals_survive_a_saturated_replay():
    """Pending asks outrank telemetry and run history — a
    replay at full depth must crowd out stale run frames, never a live ask;
    resolving one of two asks replays only the unresolved id."""
    from modulatio.web.events import _SUBSCRIBER_DEPTH, EventBus

    bus = EventBus()
    for i in range(_SUBSCRIBER_DEPTH):
        bus.publish({"type": "event", "data": {"n": i}})
    bus.publish({"type": "telemetry", "data": {"t": 1}})
    bus.publish({"type": "approval_request", "data": {"id": "a1"}})
    bus.publish({"type": "approval_request", "data": {"id": "a2"}})
    q = bus.subscribe()
    frames = []
    while not q.empty():
        frames.append(q.get_nowait())
    bus.unsubscribe(q)
    ids = [f["data"]["id"] for f in frames if f["type"] == "approval_request"]
    assert ids == ["a1", "a2"]
    assert any(f["type"] == "telemetry" for f in frames)
    assert len(frames) <= _SUBSCRIBER_DEPTH
    bus.publish({"type": "approval_resolved", "data": {"id": "a1"}})
    q2 = bus.subscribe()
    frames2 = []
    while not q2.empty():
        frames2.append(q2.get_nowait())
    bus.unsubscribe(q2)
    ids2 = [f["data"]["id"] for f in frames2 if f["type"] == "approval_request"]
    assert ids2 == ["a2"]


def test_boot_stamp_emits_once_on_the_real_entry_path():
    """The stamp must land on the path an operator actually launches. Read
    alone, the factory-body version looked right and emitted NOTHING: the
    entry passes create_app() as an argument to uvicorn.run(), so the factory
    runs before Uvicorn installs logging, AND Uvicorn configures only the
    uvicorn* loggers (root has no handler, so a modulatio.* INFO record dies
    at lastResort). Hence: launch the real server, read the real log."""
    import socket
    import subprocess
    import sys
    import tempfile
    import time
    from pathlib import Path

    from modulatio import __version__

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()

    # Output to a file, not a pipe: the server runs until we stop it, so
    # reading a pipe mid-flight risks a deadlock.
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "boot.log"
        with out.open("w") as fh:
            proc = subprocess.Popen(
                [sys.executable, "-c",
                 "from modulatio.web import server; "
                 "server.run(['--port', '%d'])" % port],
                stdout=fh, stderr=subprocess.STDOUT,
            )
            try:
                deadline = time.monotonic() + 45
                while time.monotonic() < deadline:
                    if "Application startup complete" in out.read_text():
                        break
                    if proc.poll() is not None:
                        raise AssertionError(f"server exited early:\n{out.read_text()}")
                    time.sleep(0.2)
                else:
                    raise AssertionError(f"server never started:\n{out.read_text()}")
            finally:
                proc.terminate()
                proc.wait(timeout=30)
        log = out.read_text()

    stamps = [ln for ln in log.splitlines() if "modulatio-api" in ln]
    assert len(stamps) == 1, f"expected exactly one boot stamp, got {stamps!r}\n{log}"
    assert __version__ in stamps[0]


async def test_hello_reports_stale_on_upgrade_and_downgrade(monkeypatch):
    """The wire pin, both directions: stale is ANY non-empty
    mismatch, so a rollback under a live server must fire exactly like an
    upgrade. Disk versions are INJECTED (not inherited from the test host's
    packaging state) — the route reads modulatio.installed_version at call
    time, so the patch rides the real hello path."""
    import modulatio
    from modulatio.web.routes.console import event_stream

    engine = modulatio.__version__
    for disk in (engine + ".post1", "0.0.1"):  # ahead of / behind the engine
        monkeypatch.setattr(modulatio, "installed_version", lambda d=disk: d)
        resp = await event_stream("alpha")
        frames = resp.body_iterator
        hello = await frames.__anext__()
        await frames.aclose()
        hd = json.loads(hello.split("\n")[1][6:])
        assert hd["stale"] is True, f"disk={disk} must read stale"
        assert hd["disk"] == disk and hd["engine"] == engine

    # Same version and unknown must NOT fire — the no-false-stale contract.
    for disk in (engine, None):
        monkeypatch.setattr(modulatio, "installed_version", lambda d=disk: d)
        resp = await event_stream("alpha")
        frames = resp.body_iterator
        hello = await frames.__anext__()
        await frames.aclose()
        hd = json.loads(hello.split("\n")[1][6:])
        assert hd["stale"] is False, f"disk={disk} must not read stale"


def test_stale_notice_makes_no_version_ordering_claim():
    """The server calls ANY non-empty mismatch stale, so a rollback reaches
    this notice too — copy that says "newer" would misreport the direction.
    Contract: name both versions, ask for a restart, assert no ordering.

    Source-level pin: the repo carries no JS harness, and standing one up to
    drive one string would cost more than it pins. If the notice grows real
    logic, that trade flips and this becomes a DOM test."""
    from pathlib import Path

    import modulatio.web as web

    js = (Path(web.__file__).parent / "static/js/pages/console.js").read_text()
    start = js.index("if (frame.data.stale)")
    # Comment lines are prose ABOUT the contract (they name the rejected
    # wording on purpose) — judge the emitted copy only.
    notice = "\n".join(
        ln for ln in js[start:start + 900].splitlines()
        if not ln.lstrip().startswith("//")
    )

    assert "${frame.data.disk}" in notice      # names the disk version
    assert "${frame.data.engine}" in notice    # names the running version
    assert "restart it" in notice              # names the remedy
    for claim in ("newer", "older", "upgrade", "downgrade"):
        assert claim not in notice, f"notice claims ordering: {claim!r}"


def test_create_app_alone_does_not_emit_the_boot_stamp(caplog):
    """Factory construction stays side-effect-free — the stamp belongs to
    server startup, so building an app must not claim a boot happened."""
    import logging

    from modulatio.web.app import create_app

    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        create_app(stub=True)
    assert not [r for r in caplog.records if "modulatio-api" in r.getMessage()]


def test_boot_stamp_reports_in_memory_version_not_disk(monkeypatch, caplog):
    """The boot line pins WHICH engine this process loaded — that is
    __version__, never the disk metadata (which a reinstall can move
    underneath a live process; the SSE hello reports that skew instead)."""
    import logging

    import modulatio
    from fastapi.testclient import TestClient

    from modulatio.web.app import create_app

    monkeypatch.setattr(modulatio, "installed_version", lambda: "9.9.9-from-disk")
    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        with TestClient(create_app(stub=True)):
            pass
    stamps = [r.getMessage() for r in caplog.records if "modulatio-api" in r.getMessage()]
    assert stamps == [f"modulatio-api {modulatio.__version__}"]


class _FakeDist:
    """A stand-in for importlib.metadata.Distribution: the two attributes
    installed_version() touches. ``direct_url`` None → the file is absent."""

    def __init__(self, version, direct_url=None):
        self.version = version
        self._direct_url = direct_url

    def read_text(self, name):
        return self._direct_url if name == "direct_url.json" else None


def _patch_distribution(monkeypatch, *results):
    """Patch metadata.distribution — the seam production actually calls —
    to yield ``results`` in order. Returns the call log."""
    from importlib import metadata as _md

    calls = []
    seq = list(results)

    def _fake(name):
        calls.append(name)
        outcome = seq.pop(0) if len(seq) > 1 else seq[0]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(_md, "distribution", _fake)
    return calls


def test_installed_version_rereads_disk_every_call(monkeypatch):
    """The skew detector's whole point: the disk is read at CALL time, not
    cached at import. Two successive reads see two different versions."""
    import modulatio

    calls = _patch_distribution(monkeypatch, _FakeDist("1.0.0"), _FakeDist("1.0.1"))
    assert modulatio.installed_version() == "1.0.0"
    assert modulatio.installed_version() == "1.0.1"
    assert len(calls) == 2


def test_installed_version_missing_metadata_is_unknown(monkeypatch):
    """Unreadable dist-info is UNKNOWN (None), never a false stale."""
    import modulatio
    from importlib import metadata as _md

    _patch_distribution(monkeypatch, _md.PackageNotFoundError("modulatio"))
    assert modulatio.installed_version() is None


def test_installed_version_editable_is_unknown(monkeypatch):
    """An editable install's dist-info doesn't track the code — unknown even
    when its recorded version differs from __version__ (the dev-tree case:
    a stale 0.9.5.1 stamp must NOT light the siren on every session)."""
    import modulatio

    _patch_distribution(
        monkeypatch,
        _FakeDist("0.9.5.1", direct_url='{"dir_info": {"editable": true}}'),
    )
    assert modulatio.installed_version() is None


def test_installed_version_missing_direct_url_uses_wheel_version(monkeypatch):
    """No direct_url.json = an ordinary wheel install: report its version."""
    import modulatio

    _patch_distribution(monkeypatch, _FakeDist("1.0.2", direct_url=None))
    assert modulatio.installed_version() == "1.0.2"


def test_installed_version_malformed_direct_url_is_unknown(monkeypatch):
    """Malformed direct_url.json degrades to unknown rather than guessing —
    an unparseable editable marker must not become a false stale."""
    import modulatio

    _patch_distribution(monkeypatch, _FakeDist("1.0.2", direct_url="{not json"))
    assert modulatio.installed_version() is None


# ── Doctor's unknown-disk-stamp line is cause-neutral ───────────────────────

def test_doctor_unknown_disk_stamp_is_cause_neutral(monkeypatch):
    """``installed_version() is None`` covers editable installs AND missing/
    malformed/unreadable metadata — the Doctor line must say the stamp is
    unavailable and skew detection is off WITHOUT categorically diagnosing
    an editable install."""
    import modulatio
    from modulatio import cli
    monkeypatch.setattr(modulatio, "installed_version", lambda: None)
    line = cli._doctor_version_line()
    assert "skew detection off" in line
    assert "no reliable disk stamp" in line
    assert "editable install;" not in line, (
        "the diagnosis must not be categorical — unreadable metadata also "
        "returns None"
    )


def test_doctor_version_line_reports_skew_and_clean(monkeypatch):
    """The other two branches keep their meaning: mismatch names the disk
    version; a match adds nothing."""
    import modulatio
    from modulatio import cli
    monkeypatch.setattr(modulatio, "installed_version", lambda: "0.0.1-disk")
    assert "0.0.1-disk" in cli._doctor_version_line()
    monkeypatch.setattr(
        modulatio, "installed_version", lambda: modulatio.__version__)
    line = cli._doctor_version_line()
    assert "skew" not in line and "reinstall" not in line


def test_the_console_capitalizes_an_acronym_role_as_itself():
    """The console falls back to a humanized token when the roster carries no
    name for a seat, and a run with no reviewer configured emits the role word
    where an id belongs. Capitalizing each word spells that acronym as a word.

    Source-level pin, matching the notice test above: the repo carries no JS
    harness, and standing one up to drive one branch would cost more than it
    pins."""
    from pathlib import Path

    import modulatio.web as web

    js = (Path(web.__file__).parent / "static/js/pages/console.js").read_text()
    start = js.index("function humanize(")
    body = "\n".join(
        ln for ln in js[start:start + 600].splitlines()
        if not ln.lstrip().startswith("//")
    )
    end = body.index("\n}")

    assert 'return "QC"' in body[:end]


def test_the_console_wires_escape_to_the_interrupt_it_advertises():
    """The chrome hints that Escape interrupts. Advertising an affordance no
    handler listens for leaves a wedged turn with no way out but killing the
    process, and the route it would call sitting unread.

    Source-level pin, matching the notice test above: the repo carries no JS
    harness, and standing one up to drive one binding would cost more than it
    pins."""
    from pathlib import Path

    import modulatio.web as web

    static = Path(web.__file__).parent / "static/js"
    chrome = (static / "app.js").read_text()
    console = (static / "pages/console.js").read_text()

    assert "Esc</span> interrupt" in chrome, "the hint is the claim"
    assert 'ev.key === "Escape"' in console, "nothing listens for it"
    assert "converse/interrupt" in console, "it must reach the route"
    # The approval modal owns Escape as a fail-closed deny while it is up.
    assert "if (modal.open) return;" in console
