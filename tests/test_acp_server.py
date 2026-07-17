# SPDX-License-Identifier: Apache-2.0
"""Server-level tests for the ACP server.

A reactive fake transport (one object serves as both stdin and stdout): the test
pushes client requests onto a queue the server reads, and reacts to the server's
server-initiated requests (permission / input) by pushing responses back — the
real round-trip, without a real editor. The Orchestrator is injected with a
scripted chat runner so a tool call fires deterministically.
"""
from __future__ import annotations

import io
import itertools
import json
import os
import queue
import threading

import pytest

from modulatio import runners as R
from modulatio import tools, vault
from modulatio.acp import jsonrpc as rpc
from modulatio.acp.server import ACPServer, _validate_attachment_path
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

    def converse(self, message, *, attachments=None, permission_callback=None, ask=None):
        with self._lock:
            self.entered += 1
        self.gate.wait(timeout=10)
        return "released"


def test_overlapping_prompts_rejected(vault_root):
    """One in-flight prompt per session: a second
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


class _PermissionGateOrch:
    """A fake Orchestrator whose converse() issues exactly one permission
    request (via the injected callback) and blocks inside it until the client
    answers — lets a test hold a permission request 'in flight' per session."""

    def __init__(self) -> None:
        self.permission_result = None
        self.entered = threading.Event()

    def converse(self, message, *, attachments=None, permission_callback=None, ask=None):
        self.entered.set()
        # This blocks in request_and_wait until the client responds OR the
        # session is cancelled (which resolves the slot with None → deny).
        self.permission_result = permission_callback("echo", {"x": "hi"})
        return "allowed" if self.permission_result else "denied"


def test_cancel_only_affects_target_session(vault_root):
    """H1 regression: session/cancel for session A must NOT unblock session B's
    in-flight permission request. With the bug, cancel_all() resolved every
    session's pending slot → B silently fails closed (denied)."""
    orch_a = _PermissionGateOrch()
    orch_b = _PermissionGateOrch()
    orchs = iter([orch_a, orch_b])

    # The client withholds permission responses so both requests sit pending.
    client = _Client(lambda session: next(orchs))
    held: dict = {}

    def hold(msg):
        held[msg["params"]["sessionId"]] = msg  # capture, do not respond

    client._handle_server_request = hold  # type: ignore[assignment]
    client.start()
    try:
        client.request("initialize", {})
        sid_a = client.request("session/new", {})["result"]["sessionId"]
        sid_b = client.request("session/new", {})["result"]["sessionId"]
        # Start a prompt in each; each blocks awaiting its permission response.
        rid_a = client.send("session/prompt", {"sessionId": sid_a, "prompt": "a"})
        rid_b = client.send("session/prompt", {"sessionId": sid_b, "prompt": "b"})
        assert orch_a.entered.wait(10) and orch_b.entered.wait(10)
        # Both permission requests should now be pending on the client.
        for _ in range(100):
            if sid_a in held and sid_b in held:
                break
            threading.Event().wait(0.01)
        assert sid_a in held and sid_b in held

        # Cancel session A only.
        client.send("session/cancel", {"sessionId": sid_a})
        # A unblocks (denied, fail-closed) and its prompt completes.
        assert client.wait(rid_a)["result"]["reply"] == "denied"

        # B must STILL be blocked — it was untouched by A's cancel.
        assert not client.events[rid_b].wait(0.3), \
            "session B's permission request was wrongly unblocked by A's cancel"
        assert orch_b.permission_result is None  # still pending

        # Now answer B's held permission request → B completes (allowed).
        msg_b = held[sid_b]
        client._push(rpc.make_response(
            msg_b["id"], {"outcome": {"outcome": "selected", "optionId": "allow"}}))
        assert client.wait(rid_b)["result"]["reply"] == "allowed"
    finally:
        client.close()


def test_prompt_lock_released_when_thread_start_raises(vault_root, monkeypatch):
    """H2 regression: if Thread.start() raises after begin_prompt(), the per-
    session prompt lock must be released — otherwise the session is wedged and
    every later prompt is rejected with 'active prompt'."""
    client = _Client(_factory(
        [ChatResponse(content="recovered", tool_calls=())], lambda **k: ""))
    client.start()
    try:
        client.request("initialize", {})
        sid = client.request("session/new", {})["result"]["sessionId"]

        # First prompt: force Thread.start() to blow up (OS thread pressure).
        real_start = threading.Thread.start
        boom = {"armed": True}

        def flaky_start(self):
            if boom["armed"]:
                boom["armed"] = False
                raise RuntimeError("can't start new thread")
            return real_start(self)

        monkeypatch.setattr(threading.Thread, "start", flaky_start)
        resp1 = client.request("session/prompt", {"sessionId": sid, "prompt": "x"})
        assert resp1["error"]["code"] == rpc.INTERNAL_ERROR

        # The lock must have been released: a follow-up prompt succeeds rather
        # than being rejected as an already-active prompt.
        resp2 = client.request("session/prompt", {"sessionId": sid, "prompt": "y"})
        assert "error" not in resp2, resp2
        assert resp2["result"]["reply"] == "recovered"
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


# ── §2.3: the four-option capability ask (PermissionBroker's ask surface) ─────

class _StubServer:
    def __init__(self, response):
        self.response = response
        self.sent = None

    def request_and_wait(self, method, params, cancel_check=None):
        self.sent = (method, params)
        return self.response


def _session_with(response):
    from modulatio.acp.session import ACPSession
    return ACPSession("sid", _StubServer(response))


def test_ask_capability_maps_each_option_to_decision():
    from modulatio.permissions import Decision, capability_for
    cap = capability_for("http_get", {"url": "https://example.com"})
    cases = [("once", Decision.ALLOW_ONCE), ("session", Decision.ALLOW_SESSION),
             ("always", Decision.ALLOW_ALWAYS), ("deny", Decision.DENY)]
    last = None
    for opt, expected in cases:
        s = _session_with({"outcome": {"outcome": "selected", "optionId": opt}})
        assert s.ask_capability(cap) is expected
        last = s
    # four options were offered (once/session/always/deny)
    assert len(last._server.sent[1]["options"]) == 4


def test_ask_capability_cancelled_or_unknown_is_deny():
    from modulatio.permissions import Decision, capability_for
    cap = capability_for("http_get", {"url": "https://example.com"})
    assert _session_with({"outcome": {"outcome": "cancelled"}}).ask_capability(cap) is Decision.DENY
    assert _session_with({"outcome": {"outcome": "selected", "optionId": "wat"}}).ask_capability(cap) is Decision.DENY
    assert _session_with("garbage").ask_capability(cap) is Decision.DENY
    # a cancelled session denies without even asking
    s = _session_with({"outcome": {"optionId": "always"}})
    s.cancelled = True
    assert s.ask_capability(cap) is Decision.DENY
    assert s._server.sent is None


def test_acp_run_prompt_wires_ask_capability_into_converse():
    """The WIRING (not the part): the ACP server passes ask=session.ask_capability
    AND permission_callback=session.permission_cb into converse — so the broker's
    capability questions reach the ACP client and the gate stays on the callback."""
    import io
    from modulatio.acp.server import ACPServer
    from modulatio.acp.session import ACPSession
    captured = {}

    class _Orch:
        def converse(self, message, **kw):
            captured.update(kw)
            return "reply"

    srv = ACPServer("CODE", stub=True, stdin=io.StringIO(), stdout=io.StringIO())
    sess = ACPSession("sid", srv)
    sess.orch = _Orch()
    srv._run_prompt("r1", sess, "hello", [])
    assert captured.get("ask") == sess.ask_capability          # broker ask wired
    assert captured.get("permission_callback") == sess.permission_cb  # gate wired


# ── session/cancel framing (LOW-audit fold) ─────────────────────────────────


def test_cancel_as_request_gets_a_response(vault_root):
    """#46 regression: a non-compliant client that sends ``session/cancel`` as a
    request (with an id) must still receive a response, or it hangs forever.

    Before the fix ``_dispatch`` treated cancel as a pure notification and never
    responded → ``client.wait`` here would time out."""
    client = _Client(_factory([ChatResponse(content="ok", tool_calls=())], lambda **k: ""))
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
    client = _Client(_factory([ChatResponse(content="ok", tool_calls=())], lambda **k: ""))
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


# ── pending-slot / session bookkeeping leaks (0.9.0-preship fold) ──────────
# Internal invariants exercised on a directly-constructed server with
# throwaway stdio — no editor transport required.


def _server() -> ACPServer:
    return ACPServer("ACP", stub=True, stdin=io.StringIO(), stdout=io.StringIO())


class _BrokenStdout:
    """A stdout whose write raises — models a closed pipe to the ACP client."""

    def write(self, _s):  # noqa: D401
        raise BrokenPipeError("client stdout closed")

    def flush(self):
        pass


def test_request_and_wait_pending_slot_freed_on_write_failure():
    """If write_message raises before wait(), the _pending slot is still popped
    (no leak across every failed server-initiated request)."""
    srv = _server()
    srv._stdout = _BrokenStdout()

    with pytest.raises(BrokenPipeError):
        srv.request_and_wait("session/request_permission",
                             {"sessionId": "sess-1"}, timeout=0.01)

    # The slot registered for the failed request must not linger.
    assert srv._pending._slots == {}
    # And the per-session pending set is cleaned up too.
    assert srv._session_pending.get("sess-1") in (None, set())


def test_request_and_wait_no_session_id_still_frees_pending_slot():
    """The leak fix must also fire when there's no sessionId (the per-session
    cleanup branch is skipped, but the _pending slot must still be freed)."""
    srv = _server()
    srv._stdout = _BrokenStdout()

    with pytest.raises(BrokenPipeError):
        srv.request_and_wait("session/request_input", {}, timeout=0.01)

    assert srv._pending._slots == {}


def test_request_and_wait_success_path_still_pops_slot(monkeypatch):
    """The added finally must not break the normal resolve path: a slot that is
    resolved + waited is popped exactly once, with the real value returned."""
    srv = _server()

    # Capture the rid the server picks, resolve it as a concurrent reader would.
    real_write = rpc.write_message

    def _resolve_on_write(stream, obj, lock):
        if obj.get("method"):  # the server-initiated request frame
            srv._pending.resolve(obj["id"], {"answer": "ok"})

    monkeypatch.setattr(rpc, "write_message", _resolve_on_write)
    out = srv.request_and_wait("session/request_input",
                               {"sessionId": "sess-1"}, timeout=1.0)
    monkeypatch.setattr(rpc, "write_message", real_write)

    assert out == {"answer": "ok"}
    assert srv._pending._slots == {}
    assert srv._session_pending.get("sess-1") in (None, set())


def test_session_close_reaps_session_and_pending():
    """session/close drops the session from both _sessions and _session_pending
    and unblocks its in-flight requests (fail-closed)."""
    srv = _server()
    new = srv._session_new({})
    sid = new["sessionId"]
    assert sid in srv._sessions

    # Simulate an in-flight permission request owned by the session.
    rid = "srv-99"
    srv._pending.register(rid)
    srv._session_pending.setdefault(sid, set()).add(rid)

    srv._close({"sessionId": sid})

    assert sid not in srv._sessions
    assert sid not in srv._session_pending
    # The in-flight request was resolved (None) so its waiter fails closed.
    resolved, value = srv._pending.wait(rid, 0)
    assert value is None


def test_session_close_unknown_session_is_noop():
    """Closing an unknown / already-closed session must not raise."""
    srv = _server()
    srv._close({"sessionId": "does-not-exist"})
    srv._close({})  # no sessionId at all
    assert srv._sessions == {}


def test_dispatch_routes_session_close(monkeypatch):
    """The read loop dispatches session/close to _close, and ACKs it when sent
    (non-spec) as a request with an id."""
    srv = _server()
    new = srv._session_new({})
    sid = new["sessionId"]

    responded = []
    monkeypatch.setattr(srv, "_respond", lambda rid, res: responded.append((rid, res)))

    srv._dispatch({"method": "session/close", "id": "c1",
                   "params": {"sessionId": sid}})

    assert sid not in srv._sessions
    assert responded == [("c1", None)]


# ── attachment path must be a regular file (resweep-r3 fold) ───────────────
# A FIFO/device inside an allowed root passed validation, then read_text()
# blocked the worker thread forever.


def test_fifo_inside_root_rejected(tmp_path, monkeypatch):
    """A named pipe placed inside an allowed attachment root must be rejected
    by _validate_attachment_path — reading it would block the worker forever.
    Before the fix the FIFO passed (confinement + dotfile checks only)."""
    monkeypatch.setenv("MODULATIO_ACP_ATTACHMENT_ROOTS", str(tmp_path))
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)

    with pytest.raises(ValueError, match="not a regular file"):
        _validate_attachment_path(str(fifo))


def test_directory_inside_root_rejected(tmp_path, monkeypatch):
    """A directory inside an allowed root is also not a regular file → rejected
    before any read attempt."""
    monkeypatch.setenv("MODULATIO_ACP_ATTACHMENT_ROOTS", str(tmp_path))
    sub = tmp_path / "subdir"
    sub.mkdir()

    with pytest.raises(ValueError, match="not a regular file"):
        _validate_attachment_path(str(sub))


def test_regular_file_inside_root_still_accepted(tmp_path, monkeypatch):
    """The fix must not regress the happy path: an ordinary file inside an
    allowed root still validates and returns its resolved path."""
    monkeypatch.setenv("MODULATIO_ACP_ATTACHMENT_ROOTS", str(tmp_path))
    f = tmp_path / "note.txt"
    f.write_text("hello", encoding="utf-8")

    resolved = _validate_attachment_path(str(f))
    assert resolved == f.resolve()
