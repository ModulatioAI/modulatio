# SPDX-License-Identifier: Apache-2.0
"""Tests for the ACP JSON-RPC transport/framing (modulatio.acp.jsonrpc)."""
from __future__ import annotations

import io
import threading

from modulatio.acp import jsonrpc as rpc


def test_write_message_emits_one_ndjson_frame():
    out = io.StringIO()
    rpc.write_message(out, {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}},
                      threading.Lock())
    text = out.getvalue()
    assert text.endswith("\n")
    assert text.count("\n") == 1
    import json
    assert json.loads(text) == {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}


def test_read_message_parses_and_skips_blanks_then_eof():
    stream = io.StringIO('{"jsonrpc":"2.0","method":"ping"}\n\n'
                         '{"jsonrpc":"2.0","method":"pong"}\n')
    assert rpc.read_message(stream)["method"] == "ping"
    assert rpc.read_message(stream)["method"] == "pong"  # blank line skipped
    assert rpc.read_message(stream) is None  # EOF


def test_builders_shapes():
    assert rpc.make_request("a", "m", {"x": 1}) == {
        "jsonrpc": "2.0", "id": "a", "method": "m", "params": {"x": 1}}
    assert rpc.make_notification("m") == {"jsonrpc": "2.0", "method": "m"}
    assert rpc.make_response(2, "ok") == {"jsonrpc": "2.0", "id": 2, "result": "ok"}
    err = rpc.make_error(3, rpc.METHOD_NOT_FOUND, "nope")
    assert err["error"]["code"] == rpc.METHOD_NOT_FOUND


def test_is_response_distinguishes_responses_from_requests():
    assert rpc.is_response({"id": 1, "result": {}})
    assert rpc.is_response({"id": 1, "error": {"code": -1, "message": "x"}})
    assert not rpc.is_response({"id": 1, "method": "m"})       # a request
    assert not rpc.is_response({"method": "m"})                # a notification


def test_pending_requests_correlates_across_threads():
    p = rpc.PendingRequests()
    p.register("req-1")
    threading.Thread(
        target=lambda: p.resolve("req-1", {"outcome": "allow"})).start()
    ok, value = p.wait("req-1", timeout=5)
    assert ok is True
    assert value == {"outcome": "allow"}
    assert not p.has("req-1")  # slot popped


def test_pending_requests_timeout_returns_false():
    p = rpc.PendingRequests()
    p.register("never")
    ok, value = p.wait("never", timeout=0.05)
    assert ok is False
    assert value is None


def test_idgen_is_monotonic_and_unique():
    g = rpc.IdGen()
    ids = [g.next() for _ in range(3)]
    assert len(set(ids)) == 3
