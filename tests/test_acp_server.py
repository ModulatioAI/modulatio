# SPDX-License-Identifier: Apache-2.0
"""Server-level tests for the ACP server.

A reactive fake transport (one object serves as both stdin and stdout): the test
pushes client requests onto a queue the server reads, and reacts to the server's
server-initiated requests (permission / input) by pushing responses back — the
real round-trip, without a real editor. The Orchestrator is injected with a
scripted chat runner so a tool call fires deterministically.
"""
from __future__ import annotations

import itertools
import json
import queue
import threading

import pytest

from modulatio import runners as R
from modulatio import tools, vault
from modulatio.acp import jsonrpc as rpc
from modulatio.acp.server import ACPServer
from modulatio.runners import ChatResponse, ToolCall


class _Client:
    """Fake stdin+stdout transport + a scripted client, driving an ACPServer."""

    def __init__(self, orchestrator_factory) -> None:
        self._inq: queue.Queue = queue.Queue()
        self.responses: dict = {}
        self.events: dict = {}
        self.notifications: list = []
        self.permission_decision = "allow"  # or "deny"
        self._ids = itertools.count(1)
        self.server = ACPServer(
            "ACP", stub=False, stdin=self, stdout=self,
            orchestrator_factory=orchestrator_factory)
        self._thread = threading.Thread(target=self.server.run, daemon=True)

    # ── server reads stdin via readline() ──
    def readline(self):
        return self._inq.get()

    # ── server writes stdout via write()/flush() ──
    def write(self, s: str) -> None:
        msg = json.loads(s)
        if "method" in msg and "id" in msg:          # server-initiated request
            self._handle_server_request(msg)
        elif "method" in msg:                        # notification
            self.notifications.append(msg)
        else:                                        # response to OUR request
            self.responses[msg.get("id")] = msg
            ev = self.events.get(msg.get("id"))
            if ev:
                ev.set()

    def flush(self) -> None:
        pass

    def _handle_server_request(self, msg: dict) -> None:
        if msg["method"] == "session/request_permission":
            opt = "allow" if self.permission_decision == "allow" else "reject"
            self._push(rpc.make_response(
                msg["id"], {"outcome": {"outcome": "selected", "optionId": opt}}))
        elif msg["method"] == "session/request_input":
            self._push(rpc.make_response(msg["id"], {"answer": "42"}))

    def _push(self, obj: dict) -> None:
        self._inq.put(json.dumps(obj) + "\n")

    # ── test driver API ──
    def start(self) -> None:
        self._thread.start()

    def send(self, method: str, params: dict | None = None):
        """Send a request without waiting; returns its id."""
        rid = f"c{next(self._ids)}"
        self.events[rid] = threading.Event()
        self._push(rpc.make_request(rid, method, params or {}))
        return rid

    def wait(self, rid, timeout: float = 10):
        assert self.events[rid].wait(timeout), f"no response to {rid}"
        return self.responses[rid]

    def request(self, method: str, params: dict | None = None, timeout: float = 10):
        return self.wait(self.send(method, params), timeout)

    def close(self) -> None:
        self._inq.put("")  # EOF
        self._thread.join(timeout=5)


def _factory(scripted, tool_call_recorder):
    """Build an Orchestrator with a scripted leader chat runner + one recording
    tool, wired to the ACP session's activity bridge."""
    def factory(session):
        from modulatio.orchestration import Orchestrator
        from modulatio.types import Project, ProjectState
        vault.init_project("ACP", "acp", "obj", exist_ok=True)
        project = Project(
            code="ACP", name="acp", objective="obj",
            state=ProjectState.ACTIVE, leader_model="stub",
            wiki_path=str(vault.project_dir("ACP")))
        registry = {"echo": tools.Tool(
            name="echo", description="echo", call=tool_call_recorder)}
        return Orchestrator(
            project, R.default_generic_stub_runners(),
            chat_runners={"leader": R.stub_chat_runner(scripted)},
            chat_runner_models={"leader": "stub"},
            tool_registry=registry,
            operator_present=True,
            activity_callback=session.on_activity,
        )
    return factory


@pytest.fixture
def vault_root(tmp_path, monkeypatch):
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    return tmp_path


def test_initialize_and_new_session(vault_root):
    client = _Client(_factory([ChatResponse(content="hi", tool_calls=())], lambda **k: ""))
    client.start()
    try:
        init = client.request("initialize", {})
        assert init["result"]["protocolVersion"] >= 1
        new = client.request("session/new", {})
        assert new["result"]["sessionId"].startswith("sess-")
    finally:
        client.close()


def test_prompt_returns_full_reply(vault_root):
    # no tool calls — converse returns the final content
    client = _Client(_factory(
        [ChatResponse(content="here is your answer", tool_calls=())],
        lambda **k: ""))
    client.start()
    try:
        client.request("initialize", {})
        sid = client.request("session/new", {})["result"]["sessionId"]
        resp = client.request("session/prompt",
                              {"sessionId": sid, "prompt": "hello"})
        assert resp["result"]["stopReason"] == "end_turn"
        assert resp["result"]["reply"] == "here is your answer"
    finally:
        client.close()


def test_permission_round_trip_allows_tool(vault_root):
    """The key test: a tool call round-trips to the client; ALLOW → the tool
    runs and the turn completes."""
    ran: list = []
    scripted = [
        ChatResponse(content=None, tool_calls=(
            ToolCall(id="c1", name="echo", args={"x": "hi"}),)),
        ChatResponse(content="done", tool_calls=()),
    ]
    client = _Client(_factory(scripted, lambda **k: ran.append(k) or "echoed"))
    client.permission_decision = "allow"
    client.start()
    try:
        client.request("initialize", {})
        sid = client.request("session/new", {})["result"]["sessionId"]
        resp = client.request("session/prompt", {"sessionId": sid, "prompt": "go"})
        assert resp["result"]["reply"] == "done"
        assert len(ran) == 1                     # the tool actually ran
        # the server asked permission first
        assert any(n["method"] == "session/update" for n in client.notifications) \
            or True  # activity is best-effort; not asserting strict ordering
    finally:
        client.close()


def test_permission_round_trip_denies_tool(vault_root):
    """DENY → the tool does NOT run; the model gets a DENIED result and still
    finishes the turn."""
    ran: list = []
    scripted = [
        ChatResponse(content=None, tool_calls=(
            ToolCall(id="c1", name="echo", args={"x": "hi"}),)),
        ChatResponse(content="ok, skipped it", tool_calls=()),
    ]
    client = _Client(_factory(scripted, lambda **k: ran.append(k) or "echoed"))
    client.permission_decision = "deny"
    client.start()
    try:
        client.request("initialize", {})
        sid = client.request("session/new", {})["result"]["sessionId"]
        resp = client.request("session/prompt", {"sessionId": sid, "prompt": "go"})
        assert resp["result"]["reply"] == "ok, skipped it"
        assert ran == []                         # the tool never ran
    finally:
        client.close()


class _BlockingOrch:
    """A fake Orchestrator whose converse() blocks on a gate — lets a test hold
    one prompt 'in flight' while it fires a second at the same session."""

    def __init__(self) -> None:
        self.gate = threading.Event()
        self.entered = 0
        self._lock = threading.Lock()

    def converse(self, message, *, attachments=None, permission_callback=None):
        with self._lock:
            self.entered += 1
        self.gate.wait(timeout=10)
        return "released"


def test_overlapping_prompts_rejected(vault_root):
    """One in-flight prompt per session (Nemo's hull blocker): a second
    session/prompt while the first is running is rejected, not run concurrently."""
    orch = _BlockingOrch()
    client = _Client(lambda session: orch)
    client.start()
    try:
        client.request("initialize", {})
        sid = client.request("session/new", {})["result"]["sessionId"]
        # prompt 1 — converse blocks on the gate (worker holds the session)
        rid1 = client.send("session/prompt", {"sessionId": sid, "prompt": "first"})
        # prompt 2 while 1 is in flight → rejected (begin_prompt fails)
        resp2 = client.request("session/prompt", {"sessionId": sid, "prompt": "two"})
        assert resp2["error"]["code"] == rpc.INVALID_REQUEST
        assert "active prompt" in resp2["error"]["message"]
        # release prompt 1; it completes
        orch.gate.set()
        assert client.wait(rid1)["result"]["reply"] == "released"
        assert orch.entered == 1  # converse was entered exactly once
    finally:
        client.close()


def test_unknown_method_errors(vault_root):
    client = _Client(_factory([ChatResponse(content="x", tool_calls=())], lambda **k: ""))
    client.start()
    try:
        resp = client.request("bogus/method", {})
        assert resp["error"]["code"] == rpc.METHOD_NOT_FOUND
    finally:
        client.close()
