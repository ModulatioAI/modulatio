# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Bridge conformance: each operator surface's REAL approval bridge carries
the authorization coordinator's bundle — zero or one approval event per tool
call, never more. The pure-cell matrix (test_access_matrix.py) proves the
engine contract; this file proves the surface WIRING: the TUI modal bridge,
the web approval broker, and the ACP ``session/request_permission`` round
trip each deliver exactly one event for a multi-request call and zero for a
silently-resolved one.

Deliberately a separate file: these cases mount real UI/transport machinery
(a Textual pilot, an SSE bus, a JSON-RPC server thread) and are slower than
pure gate cells — run per-surface when touching a bridge, and in the full
gate; they do not ride the per-edit scoped path.
"""
from __future__ import annotations

import threading

import pytest

from modulatio import leader_gate as lg
from modulatio import permissions as perm
from modulatio import vault

CODE = "bridge"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    vault.init_project(CODE, "b", "b")
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "in.txt").write_text("x")
    o1 = tmp_path / "alpha"
    o2 = tmp_path / "beta"
    o1.mkdir()
    o2.mkdir()
    (o1 / "a.txt").write_text("a")
    (o2 / "b.txt").write_text("b")
    return tmp_path, ws, o1, o2


def _coordinator(ws, prompt_fn, tmp_path):
    gate = lg.LeaderPermissionGate(CODE, workspace=ws)
    broker = perm.PermissionBroker(
        mode=perm.RunMode.DEFAULT,
        grants=perm.GrantStore(tmp_path / "grants.json"),
        ask=None,
        sandbox_available=lambda: True,
    )
    return perm.build_authorization_coordinator(
        gate=gate, root=ws, prompt_fn=prompt_fn, broker=broker)


def _bundle_call(o1, o2):
    return "run_shell", {"cmd": f"cat {o1 / 'a.txt'} {o2 / 'b.txt'}"}


def _silent_call(ws):
    return "read_file", {"path": str(ws / "in.txt")}


def _drain_approval_frames(bus_q) -> list:
    import queue as _q

    frames = []
    try:
        while True:
            f = bus_q.get_nowait()
            if f["type"] == "approval_request":
                frames.append(f)
    except _q.Empty:
        return frames


# ── web: the ApprovalBroker bus/resolve bridge ──────────────────────────────


def test_web_bridge_one_event_per_bundle_call(env):
    from modulatio.web.actors import ApprovalBroker
    from modulatio.web.events import get_bus

    tmp_path, ws, o1, o2 = env
    bus_q = get_bus(CODE).subscribe()
    try:
        approvals = ApprovalBroker(CODE, timeout_s=10)
        coord = _coordinator(ws, approvals.prompt, tmp_path)
        tool, args = _bundle_call(o1, o2)
        result: list = []

        t = threading.Thread(target=lambda: result.append(coord(tool, args)))
        t.start()
        frame = bus_q.get(timeout=5)
        assert frame["type"] == "approval_request"
        # The single frame discloses the whole bundle to the browser.
        assert frame["data"]["why"]
        approvals.resolve(frame["data"]["id"], "session")
        t.join(timeout=5)
        assert result == [True]
        extra = _drain_approval_frames(bus_q)
        assert extra == []  # one bundle, one browser approval event
    finally:
        get_bus(CODE).unsubscribe(bus_q)


def test_web_bridge_zero_events_for_silent_call(env):
    from modulatio.web.actors import ApprovalBroker
    from modulatio.web.events import get_bus

    tmp_path, ws, o1, o2 = env
    bus_q = get_bus(CODE).subscribe()
    try:
        approvals = ApprovalBroker(CODE, timeout_s=1)
        coord = _coordinator(ws, approvals.prompt, tmp_path)
        tool, args = _silent_call(ws)
        assert coord(tool, args) is True
        assert _drain_approval_frames(bus_q) == []
    finally:
        get_bus(CODE).unsubscribe(bus_q)


# ── tui: the modal prompt bridge (worker thread → UI thread) ────────────────


async def test_tui_bridge_one_modal_per_bundle_call(env):
    from textual.app import App, ComposeResult
    from textual.widgets import Static

    from modulatio.tui.leader_prompt import make_modal_prompt_fn
    from modulatio.tui.widgets.leader_approval_modal import LeaderApprovalModal

    tmp_path, ws, o1, o2 = env

    class _Host(App[None]):
        def get_css_variables(self) -> dict:
            v = super().get_css_variables()
            v.setdefault("frame", "#6cb6e4")
            v.setdefault("frame-dim", "#3f6d8c")
            return v

        def compose(self) -> ComposeResult:
            yield Static("host")

    app = _Host()
    async with app.run_test() as pilot:
        shown: list = []
        real_prompt = make_modal_prompt_fn(app)

        def counting_prompt(req):
            shown.append(req)
            return real_prompt(req)

        coord = _coordinator(ws, counting_prompt, tmp_path)
        tool, args = _bundle_call(o1, o2)
        box: dict = {}

        t = threading.Thread(
            target=lambda: box.setdefault("allowed", coord(tool, args)),
            daemon=True)
        t.start()
        for _ in range(200):
            await pilot.pause()
            if isinstance(app.screen, LeaderApprovalModal):
                break
        assert isinstance(app.screen, LeaderApprovalModal)
        await pilot.click("#scope-session")
        for _ in range(200):
            await pilot.pause()
            if not t.is_alive():
                break
        t.join(timeout=2)
        assert box.get("allowed") is True
        assert len(shown) == 1  # one bundle, one modal
        # The single modal disclosed the whole bundle: both outside roots.
        assert str(o2) in shown[0].why or str(o2 / "b.txt") in shown[0].why


async def test_tui_bridge_zero_modals_for_silent_call(env):
    from textual.app import App, ComposeResult
    from textual.widgets import Static

    from modulatio.tui.leader_prompt import make_modal_prompt_fn

    tmp_path, ws, o1, o2 = env

    class _Host(App[None]):
        def get_css_variables(self) -> dict:
            v = super().get_css_variables()
            v.setdefault("frame", "#6cb6e4")
            v.setdefault("frame-dim", "#3f6d8c")
            return v

        def compose(self) -> ComposeResult:
            yield Static("host")

    app = _Host()
    async with app.run_test() as pilot:
        shown: list = []
        real_prompt = make_modal_prompt_fn(app)

        def counting_prompt(req):
            shown.append(req)
            return real_prompt(req)

        coord = _coordinator(ws, counting_prompt, tmp_path)
        tool, args = _silent_call(ws)
        box: dict = {}
        t = threading.Thread(
            target=lambda: box.setdefault("allowed", coord(tool, args)),
            daemon=True)
        t.start()
        for _ in range(50):
            await pilot.pause()
            if not t.is_alive():
                break
        t.join(timeout=2)
        assert box.get("allowed") is True
        assert shown == []


# ── acp: the session/request_permission JSON-RPC round trip ────────────────


def test_acp_bridge_one_permission_request_per_bundle_call(env, tmp_path):
    from tests.test_acp_server import _Client

    _tmp, ws, o1, o2 = env

    class _CountingClient(_Client):
        def __init__(self, factory):
            super().__init__(factory)
            self.permission_requests = 0

        def _handle_server_request(self, msg):
            if msg["method"] == "session/request_permission":
                self.permission_requests += 1
            super()._handle_server_request(msg)  # answers the narrowed menu

    class _CoordinatorOrch:
        """converse() drives the real coordinator over the server's real
        permission bridge — the production wiring, one tool call deep."""

        def converse(self, message, *, attachments=None,
                     permission_callback=None, prompt_fn=None, ask=None):
            coord = _coordinator(ws, prompt_fn, _tmp)
            if message == "bundle":
                tool, args = _bundle_call(o1, o2)
            else:
                tool, args = _silent_call(ws)
            return "allowed" if coord(tool, args) else "denied"

    client = _CountingClient(lambda session: _CoordinatorOrch())
    client.start()
    try:
        client.request("initialize", {})
        sid = client.request("session/new", {})["result"]["sessionId"]
        reply = client.request(
            "session/prompt", {"sessionId": sid, "prompt": "bundle"})
        assert reply["result"]["reply"] == "allowed"
        assert client.permission_requests == 1
        silent = client.request(
            "session/prompt", {"sessionId": sid, "prompt": "silent"})
        assert silent["result"]["reply"] == "allowed"
        assert client.permission_requests == 1  # unchanged: zero new events
    finally:
        client.close()
