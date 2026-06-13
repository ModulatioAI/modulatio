# SPDX-License-Identifier: Apache-2.0
"""LOW-audit regression tests for the ACP server.

Owned by the LOW-audit pass; kept in a separate file so it can't collide with
the main ``test_acp_server.py`` while sibling agents edit concurrently.
"""
from __future__ import annotations

import itertools
import json
import queue
import threading

import pytest

from modulatio import runners as R
from modulatio import vault
from modulatio.acp import jsonrpc as rpc
from modulatio.acp.server import ACPServer
from modulatio.runners import ChatResponse


class _Client:
    """Minimal fake stdin+stdout transport driving an ACPServer."""

    def __init__(self, orchestrator_factory) -> None:
        self._inq: queue.Queue = queue.Queue()
        self.responses: dict = {}
        self.events: dict = {}
        self._ids = itertools.count(1)
        self.server = ACPServer(
            "ACP", stub=False, stdin=self, stdout=self,
            orchestrator_factory=orchestrator_factory)
        self._thread = threading.Thread(target=self.server.run, daemon=True)

    def readline(self):
        return self._inq.get()

    def write(self, s: str) -> None:
        msg = json.loads(s)
        if "method" in msg and "id" in msg:          # server-initiated request
            pass
        elif "method" in msg:                        # notification
            pass
        else:                                        # response to OUR request
            self.responses[msg.get("id")] = msg
            ev = self.events.get(msg.get("id"))
            if ev:
                ev.set()

    def flush(self) -> None:
        pass

    def _push(self, obj: dict) -> None:
        self._inq.put(json.dumps(obj) + "\n")

    def start(self) -> None:
        self._thread.start()

    def send(self, method: str, params: dict | None = None):
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


def _factory():
    def factory(session):
        from modulatio.orchestration import Orchestrator
        from modulatio.types import Project, ProjectState
        vault.init_project("ACP", "acp", "obj", exist_ok=True)
        project = Project(
            code="ACP", name="acp", objective="obj",
            state=ProjectState.ACTIVE, leader_model="stub",
            wiki_path=str(vault.project_dir("ACP")))
        return Orchestrator(
            project, R.default_generic_stub_runners(),
            chat_runners={"leader": R.stub_chat_runner(
                [ChatResponse(content="ok", tool_calls=())])},
            chat_runner_models={"leader": "stub"},
            tool_registry={},
            operator_present=True,
            activity_callback=session.on_activity,
        )
    return factory


@pytest.fixture
def vault_root(tmp_path, monkeypatch):
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    return tmp_path


def test_cancel_as_request_gets_a_response(vault_root):
    """#46 regression: a non-compliant client that sends ``session/cancel`` as a
    request (with an id) must still receive a response, or it hangs forever.

    Before the fix ``_dispatch`` treated cancel as a pure notification and never
    responded → ``client.wait`` here would time out."""
    client = _Client(_factory())
    client.start()
    try:
        client.request("initialize", {})
        sid = client.request("session/new", {})["result"]["sessionId"]
        # Send cancel WITH an id (request-shaped, off-spec but real in the wild).
        rid = client.send("session/cancel", {"sessionId": sid})
        resp = client.wait(rid, timeout=5)
        assert "id" in resp
        # Either a JSON-RPC result (we ACK) — never silence.
        assert "result" in resp or "error" in resp
    finally:
        client.close()


def test_cancel_as_notification_still_silent(vault_root):
    """The spec path is preserved: a cancel with NO id remains a notification —
    the server emits no response frame for it (no spurious id:null response)."""
    client = _Client(_factory())
    client.start()
    try:
        client.request("initialize", {})
        sid = client.request("session/new", {})["result"]["sessionId"]
        # Notification-shaped cancel (no id).
        client._push(rpc.make_notification("session/cancel", {"sessionId": sid}))
        # A following request must round-trip normally (server didn't wedge and
        # didn't emit a stray response).
        again = client.request("session/new", {})
        assert again["result"]["sessionId"].startswith("sess-")
    finally:
        client.close()
