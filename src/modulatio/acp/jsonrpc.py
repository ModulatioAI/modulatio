# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""JSON-RPC 2.0 transport + framing for the ACP server — no Modulatio knowledge.

Newline-delimited JSON (ndjson) over two file objects (stdin/stdout), the framing
Zed-class ACP clients use. Stdlib only (``json`` + ``threading``). The one subtle
piece is correlating *server-initiated* requests (the agent asking the client to
approve a tool call, or answer an input prompt) with the client's response, by
JSON-RPC id — handled by :class:`PendingRequests`.
"""
from __future__ import annotations

import itertools
import json
import threading
from typing import Any

JSONRPC_VERSION = "2.0"

# Standard JSON-RPC error codes we use.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INTERNAL_ERROR = -32603


def read_message(stream) -> dict | None:
    """Read one ndjson frame. Returns the parsed object, or None at EOF.
    Blank lines are skipped; a malformed line raises ``json.JSONDecodeError``
    (the caller turns that into a PARSE_ERROR)."""
    while True:
        line = stream.readline()
        if line == "" or line == b"":
            return None  # EOF
        if isinstance(line, bytes):
            line = line.decode("utf-8")
        line = line.strip()
        if not line:
            continue  # skip blank framing lines
        return json.loads(line)


def write_message(stream, obj: dict, lock: threading.Lock) -> None:
    """Serialize one object as a single ndjson frame and flush, guarded by a
    lock so concurrent writers (the prompt worker's permission request and the
    activity-callback notifications) never interleave bytes on the stream."""
    data = json.dumps(obj, separators=(",", ":"))
    with lock:
        stream.write(data + "\n")
        stream.flush()


# ── message builders ────────────────────────────────────────────────────────

def make_request(req_id: Any, method: str, params: dict | None = None) -> dict:
    msg = {"jsonrpc": JSONRPC_VERSION, "id": req_id, "method": method}
    if params is not None:
        msg["params"] = params
    return msg


def make_notification(method: str, params: dict | None = None) -> dict:
    msg = {"jsonrpc": JSONRPC_VERSION, "method": method}
    if params is not None:
        msg["params"] = params
    return msg


def make_response(req_id: Any, result: Any) -> dict:
    return {"jsonrpc": JSONRPC_VERSION, "id": req_id, "result": result}


def make_error(req_id: Any, code: int, message: str) -> dict:
    return {
        "jsonrpc": JSONRPC_VERSION,
        "id": req_id,
        "error": {"code": code, "message": message},
    }


def is_response(msg: dict) -> bool:
    """A message is a *response* (to one of OUR requests) if it carries an id
    and a result/error but no method."""
    return "method" not in msg and ("result" in msg or "error" in msg)


class IdGen:
    """Thread-safe monotonic id source for server-initiated requests."""

    def __init__(self, prefix: str = "srv") -> None:
        self._counter = itertools.count(1)
        self._prefix = prefix
        self._lock = threading.Lock()

    def next(self) -> str:
        with self._lock:
            return f"{self._prefix}-{next(self._counter)}"


class PendingRequests:
    """Correlates server-initiated requests with their client responses by id.

    The single reader thread owns stdin: when it sees a response carrying an id
    we issued, it ``resolve``s the matching slot, unblocking the worker thread
    that's waiting in ``wait``."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._slots: dict[Any, list] = {}  # id -> [Event, value]

    def register(self, req_id: Any) -> None:
        with self._lock:
            self._slots[req_id] = [threading.Event(), None]

    def has(self, req_id: Any) -> bool:
        with self._lock:
            return req_id in self._slots

    def resolve(self, req_id: Any, value: Any) -> None:
        with self._lock:
            slot = self._slots.get(req_id)
        if slot is not None:
            slot[1] = value
            slot[0].set()

    def wait(self, req_id: Any, timeout: float | None = None) -> tuple[bool, Any]:
        """Block until the slot is resolved (or timeout). Returns
        ``(resolved, value)``; pops the slot."""
        with self._lock:
            slot = self._slots.get(req_id)
        if slot is None:
            return (False, None)
        ok = slot[0].wait(timeout)
        with self._lock:
            self._slots.pop(req_id, None)
        return (ok, slot[1] if ok else None)

    def cancel_all(self) -> None:
        """Resolve every pending slot with None (used on shutdown / cancel) so
        no worker blocks forever."""
        with self._lock:
            slots = list(self._slots.values())
        for slot in slots:
            slot[0].set()


__all__ = [
    "JSONRPC_VERSION",
    "PARSE_ERROR", "INVALID_REQUEST", "METHOD_NOT_FOUND", "INTERNAL_ERROR",
    "read_message", "write_message",
    "make_request", "make_notification", "make_response", "make_error",
    "is_response", "IdGen", "PendingRequests",
]
