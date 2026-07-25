# SPDX-License-Identifier: Apache-2.0
"""Pre-ship (0.9.0) re-sweep regression for one ACP session race.

Finding (MEDIUM/race): a ``session/cancel`` that fires between
``permission_cb``/``ask_operator``'s ``self.cancelled`` pre-check and
``request_and_wait``'s pending-registration would snapshot ``_session_pending``
WITHOUT the new rid, so nothing ever resolves it — the server-initiated
permission/input request blocked for the full ``_REQUEST_TIMEOUT_SECONDS``
(600s) instead of failing closed promptly.

The fix threads an atomic ``cancel_check`` into ``request_and_wait`` (evaluated
under the same ``_session_pending_lock`` the cancel snapshots, right after the
rid is registered) so a cancel-in-the-window is observed and the request bails
with None immediately. ``ask_operator`` also gains the entry guard
``permission_cb`` already had.

These are bookkeeping/timing invariants, so we drive an ACPServer + ACPSession
directly with throwaway stdio — no real editor transport.
"""
from __future__ import annotations

import io
import time

from modulatio.acp import jsonrpc as rpc
from modulatio.acp.server import ACPServer
from modulatio.acp.session import ACPSession


def _server() -> ACPServer:
    return ACPServer("ACP", stub=True, stdin=io.StringIO(), stdout=io.StringIO())


def test_cancel_check_short_circuits_when_cancel_lost_the_race():
    """If cancellation already happened in the window (cancel_check truthy),
    request_and_wait must fail closed with None promptly and must NOT issue the
    request — and must leave no _pending / _session_pending residue."""
    srv = _server()

    wrote = []

    def _record_write(stream, obj, lock):
        wrote.append(obj)

    srv_writes = rpc.write_message
    try:
        rpc.write_message = _record_write  # type: ignore[assignment]
        start = time.monotonic()
        out = srv.request_and_wait(
            "session/request_permission",
            {"sessionId": "sess-1"},
            timeout=600.0,  # would hang for 10 minutes without the fix
            cancel_check=lambda: True,
        )
        elapsed = time.monotonic() - start
    finally:
        rpc.write_message = srv_writes  # type: ignore[assignment]

    assert out is None
    assert elapsed < 1.0  # bailed immediately, did not block on the timeout
    assert wrote == []  # the request frame was never written
    assert srv._pending._slots == {}
    assert srv._session_pending.get("sess-1") in (None, set())


def test_cancel_check_false_proceeds_normally():
    """When cancel_check is falsy the request proceeds and resolves as usual —
    the new guard must not break the happy path."""
    srv = _server()

    def _resolve_on_write(stream, obj, lock):
        if obj.get("method"):  # the server-initiated request frame
            srv._pending.resolve(obj["id"], {"answer": "ok"})

    real = rpc.write_message
    try:
        rpc.write_message = _resolve_on_write  # type: ignore[assignment]
        out = srv.request_and_wait(
            "session/request_input",
            {"sessionId": "sess-1"},
            timeout=1.0,
            cancel_check=lambda: False,
        )
    finally:
        rpc.write_message = real  # type: ignore[assignment]

    assert out == {"answer": "ok"}
    assert srv._pending._slots == {}


def test_ask_operator_entry_guard_fails_closed_on_cancel():
    """ask_operator gained the same entry guard permission_cb has: a cancelled
    session returns None without issuing an input request."""
    srv = _server()
    session = ACPSession("sess-1", srv)
    session.cancelled = True

    issued = []

    def _record(method, params, timeout=600.0, cancel_check=None):
        issued.append(method)
        return {"answer": "should-not-be-used"}

    srv.request_and_wait = _record  # type: ignore[assignment]
    out = session.ask_operator("name?")

    assert out is None
    assert issued == []  # never reached request_and_wait
