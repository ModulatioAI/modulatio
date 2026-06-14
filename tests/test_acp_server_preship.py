# SPDX-License-Identifier: Apache-2.0
"""Pre-ship (0.9.0) regressions for two resource leaks in the ACP server.

1. MEDIUM/resource-leak: ``request_and_wait`` must not strand a ``_pending``
   slot when ``write_message`` raises before ``wait()`` ever runs.
2. LOW/resource-leak: an explicit ``session/close`` must reap the session from
   ``_sessions`` / ``_session_pending`` so neither grows unbounded.

Both are internal-bookkeeping invariants, so they're exercised by constructing
an ACPServer directly with throwaway stdio and inspecting its slot maps — no
real editor transport required.
"""
from __future__ import annotations

import io

import pytest

from modulatio.acp import jsonrpc as rpc
from modulatio.acp.server import ACPServer


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
