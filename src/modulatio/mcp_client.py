# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""MCP client — connect to configured MCP servers and expose their tools.

The official ``mcp`` SDK (opt-in ``[mcp]``
extra) speaks the protocol over both transports; this module is the thin
Modulatio adapter: it opens/holds one connection per configured server,
discovers the server's tools, and translates each into a ``tools.Tool`` that
routes calls back through the connection.

The SDK is asyncio and its transports are anyio context managers that must stay
open for the connection's life, so a single background event-loop thread (the
"portal") holds every session alive; synchronous ``Tool.call`` closures submit
coroutines to it and block for the result. Absent the extra, everything here is
inert — ``build_mcp_tools`` returns ``{}`` with an install hint, so the base
install is unaffected.

Trust + metering ride the record: a ``gated`` server's tool calls are gated by
``leader_gate`` in the Leader lane (see ``leader_gate.extract_tool_requests``);
a ``metered`` server's tools carry ``cost_class="paid-cloud"`` so the
comptroller authorizes each call. Secrets (an http token, a secret stdio env
value) are injected here from the vault and never enter model context; tool
results are secret-scrubbed before returning.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import threading
from typing import Optional

from modulatio import mcp_config, tools
from modulatio.mcp_config import McpServer

_logger = logging.getLogger("modulatio.mcp")

#: Per-call wall-clock ceiling (seconds) — a hung MCP tool must not hang a run.
_CALL_TIMEOUT = 60.0
#: Handshake + discovery ceiling when opening a connection.
_CONNECT_TIMEOUT = 30.0
#: Discovery bound — tools past this are dropped (logged), not translated.
_MAX_TOOLS = 128
#: Description ceiling per translated tool (rides every model request).
_MAX_DESC_CHARS = 2_000
#: Function-calling-safe tool name — anything else is skipped at translation.
_TOOL_NAME_OK = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
#: Provider ceiling for the FINAL namespaced function name (mcp__<id>__<tool>).
_MAX_FUNC_NAME = 64
#: Serialized input-schema ceiling — one tool must not bloat every request.
_MAX_SCHEMA_CHARS = 16_000


def _have_sdk() -> bool:
    try:
        import mcp  # noqa: F401
        return True
    except ImportError:
        return False


_INSTALL_HINT = (
    'MCP support needs the mcp extra. Install it with:  '
    'pip install "modulatio[mcp]"'
)


# ── secrets: read from the vault, inject here, never surface ────────────────

def _http_headers(server: McpServer) -> dict[str, str]:
    """Auth headers for an http server from its vault token, or ``{}``. The
    token value is injected here and never returned to the model."""
    if not server.env_var:
        return {}
    token = (os.environ.get(server.env_var) or "").strip()
    if not token:
        return {}
    shape = server.auth_shape or "bearer"
    if shape == "bearer":
        return {"Authorization": f"Bearer {token}"}
    if shape.startswith("header:"):
        return {shape.split(":", 1)[1]: token}
    return {}  # unreachable for a validated record (bearer|header:<Name> only)


def _stdio_env(server: McpServer) -> dict[str, str]:
    """Environment for a stdio subprocess: the record's non-secret ``env`` plus,
    for any value shaped ``$VAULT_SLOT``, the real secret read from the vault.
    The child inherits nothing else Modulatio doesn't hand it explicitly."""
    out: dict[str, str] = {}
    for k, v in server.env.items():
        if isinstance(v, str) and v.startswith("$"):
            out[k] = (os.environ.get(v[1:]) or "").strip()
        else:
            out[k] = str(v)
    return out


def _secret_values(server: McpServer) -> list[str]:
    """EVERY secret injected for this server — the http token slot plus each
    ``$VAULT_SLOT``-shaped stdio env value. The scrub set: anything a server
    could echo back that must never reach the model."""
    out: list[str] = []
    if server.env_var:
        token = (os.environ.get(server.env_var) or "").strip()
        if token:
            out.append(token)
    for v in server.env.values():
        if isinstance(v, str) and v.startswith("$"):
            secret = (os.environ.get(v[1:]) or "").strip()
            if secret:
                out.append(secret)
    return out


def _redact(text: str, server: McpServer) -> str:
    """Strip every one of the server's injected secrets from text."""
    secrets = _secret_values(server)
    if secrets:
        from modulatio.service_tools import _redact_key
        for secret in secrets:
            text = _redact_key(text, secret)
    return text


def _scrub(text: str, server: McpServer) -> str:
    """Redact text bound for the model — a success result OR a failure message
    (an auth'd server can echo its own credential in either) — then cap."""
    return tools._cap_http_body(_redact(text, server), over_read=False)


def _safe_exc(exc: BaseException, server: McpServer) -> str:
    """An exception rendered safe for the LOG: class + scrubbed message. Never
    the raw exception/traceback (``exc_info``) — a protocol error can carry the
    server's credential, and process logs are retained surfaces too."""
    return _scrub(f"{type(exc).__name__}: {exc}", server)


def _safe_field(value, server: McpServer) -> str:
    """A SERVER-SUPPLIED value (a tool name, a spec field) rendered safe for
    the LOG: redacted with the server's scrub set and sliced short — a
    credential-shaped tool name must not persist in a retained surface."""
    return _redact(str(value), server)[:120]


# ── the portal: one background event loop holding every live session ────────

class _Portal:
    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run, name="modulatio-mcp-portal", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def submit(self, coro, timeout: float):
        """Run ``coro`` on the portal loop and block for its result. A timeout
        CANCELS the submitted work — a hung call must not keep running on the
        shared loop after its caller has given up (it would starve siblings)."""
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)
        try:
            return fut.result(timeout=timeout)
        except TimeoutError:
            fut.cancel()
            raise

    def stop(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)


_portal: "Optional[_Portal]" = None
_portal_lock = threading.Lock()


def _get_portal() -> _Portal:
    global _portal
    with _portal_lock:
        if _portal is None:
            _portal = _Portal()
            # First portal = first live session this process will hold. Close
            # them on interpreter exit so stdio children are terminated by the
            # SDK's transport teardown, not orphaned by a dying daemon thread.
            import atexit
            atexit.register(shutdown)
        return _portal


class _Connection:
    """One live MCP session held open in the portal loop for a server's life."""

    def __init__(self, server: McpServer) -> None:
        self.server = server
        self._portal = _get_portal()
        self._session = None          # set in the loop once initialized
        self._close_evt = None        # asyncio.Event, created in the loop
        self._ready = threading.Event()
        self._error: "Optional[Exception]" = None
        self._box: dict = {}
        # Schedule the long-lived holder; it parks until close(). The future
        # is RETAINED so a connect timeout CANCELS it — cancellation unwinds
        # the transport stack (terminating a spawned stdio child) instead of
        # abandoning a live holder + subprocess on the shared loop per retry.
        self._holder = asyncio.run_coroutine_threadsafe(
            self._hold(), self._portal.loop)
        if not self._ready.wait(timeout=_CONNECT_TIMEOUT):
            self._holder.cancel()
            raise TimeoutError("MCP connect timed out")
        if self._error is not None:
            raise self._error
        self._session = self._box["session"]

    async def _hold(self) -> None:
        from contextlib import AsyncExitStack

        from mcp import ClientSession
        try:
            self._close_evt = asyncio.Event()
            self._box["close_evt"] = self._close_evt
            async with AsyncExitStack() as stack:
                read, write = await self._open_transport(stack)
                session = await stack.enter_async_context(
                    ClientSession(read, write))
                await session.initialize()
                self._box["session"] = session
                self._ready.set()
                await self._close_evt.wait()   # park until close()
        except Exception as exc:  # noqa: BLE001 — surfaced to the sync caller
            self._error = exc
            self._ready.set()

    async def _open_transport(self, stack):
        from mcp import StdioServerParameters
        if self.server.transport == "stdio":
            from mcp.client.stdio import stdio_client
            params = StdioServerParameters(
                command=self.server.command,
                args=list(self.server.args),
                env=_stdio_env(self.server) or None,
            )
            read, write = await stack.enter_async_context(stdio_client(params))
            return read, write
        from mcp.client.streamable_http import streamablehttp_client
        read, write, _ = await stack.enter_async_context(
            streamablehttp_client(
                self.server.base_url, headers=_http_headers(self.server)))
        return read, write

    def list_tools(self) -> list:
        """The server's tool specs (mcp.types.Tool objects), capped at
        ``_MAX_TOOLS`` — a hostile/buggy discovery list is bounded before any
        registry translation."""
        result = self._portal.submit(
            self._session.list_tools(), _CONNECT_TIMEOUT)
        specs = list(result.tools[:_MAX_TOOLS])
        if len(result.tools) > _MAX_TOOLS:
            _logger.warning(
                "MCP server %s offered %d tools; capped at %d",
                self.server.id, len(result.tools), _MAX_TOOLS)
        return specs

    def call(self, tool_name: str, arguments: dict) -> str:
        """Call one tool; return its text result (scrubbed + capped)."""
        result = self._portal.submit(
            self._session.call_tool(tool_name, arguments or {}), _CALL_TIMEOUT)
        text = _result_text(result)
        if getattr(result, "isError", False):
            text = f"[tool error] {text}"
        return _scrub(text, self.server)

    def close(self) -> None:
        evt = self._box.get("close_evt")
        if evt is not None:
            self._portal.loop.call_soon_threadsafe(evt.set)


def _result_text(result) -> str:
    """Flatten a CallToolResult's content blocks into text, bounded as it
    accumulates — a giant server-sent block is sliced at the budget instead of
    fully copied and joined before the return cap can act. The budget is 2× the
    return ceiling so redaction still sees everything the model could see.
    Every block is charged at least its join separator, so block COUNT is
    bounded by the same budget — a flood of empty blocks can't grow the parts
    list or the joined output past it."""
    budget = 2 * tools._HTTP_GET_MAX_CHARS
    parts: list[str] = []
    total = 0
    for block in getattr(result, "content", None) or []:
        remaining = budget - total
        if remaining <= 0:
            break
        t = getattr(block, "text", None)
        piece = str(t) if t is not None else str(getattr(block, "type", block))
        parts.append(piece[:remaining])
        total += min(len(piece), remaining) + 1   # +1 charges the separator
    return "\n".join(parts)


# ── connection cache ────────────────────────────────────────────────────────

_connections: dict[str, _Connection] = {}
_conn_lock = threading.Lock()


def get_connection(server_id: str) -> "_Connection | None":
    """A live (cached) connection for a server, or ``None`` if it can't open.
    A failure logs and degrades — a caller sees the tool as unavailable."""
    server = mcp_config.get_server(server_id)
    if server is None or not server.enabled:
        return None
    with _conn_lock:
        conn = _connections.get(server_id)
        if conn is not None:
            return conn
        if not _have_sdk():
            _logger.warning("MCP server %s configured but %s", server_id, _INSTALL_HINT)
            return None
        try:
            conn = _Connection(server)
        except Exception as exc:  # noqa: BLE001 — degrade, never crash a run
            _logger.warning("MCP server %s failed to connect: %s",
                            server_id, _safe_exc(exc, server))
            return None
        _connections[server_id] = conn
        return conn


def call_tool(server_id: str, tool_name: str, **kwargs) -> str:
    """Route a namespaced MCP tool call to its server; a connection or call
    failure returns an honest error string (never raises into the run)."""
    conn = get_connection(server_id)
    if conn is None:
        return f"MCP server {server_id!r} unavailable (not connected)."
    try:
        return conn.call(tool_name, kwargs)
    except Exception as exc:  # noqa: BLE001 — one bad call must not sink the run
        _logger.warning("MCP call %s/%s failed: %s",
                        server_id, _safe_field(tool_name, conn.server),
                        _safe_exc(exc, conn.server))
        # Drop the dead connection so the next call reconnects once.
        with _conn_lock:
            _connections.pop(server_id, None)
        conn.close()
        # A protocol exception can carry the server's own credential (an
        # auth'd URL, an echoed header) — the failure text is scrubbed like a
        # success result before it reaches the model.
        return _scrub(
            f"MCP tool {server_id}:{tool_name} failed: {type(exc).__name__}: {exc}",
            conn.server,
        )


# ── tool-name namespacing (function-calling-safe charset) ───────────────────

_PREFIX = "mcp__"


def tool_name(server_id: str, mcp_tool: str) -> str:
    return f"{_PREFIX}{server_id}__{mcp_tool}"


def parse_tool_name(name: str) -> "tuple[str, str] | None":
    """``mcp__<server>__<tool>`` → ``(server, tool)``, else ``None``."""
    if not name.startswith(_PREFIX):
        return None
    rest = name[len(_PREFIX):]
    server, sep, mcp_tool = rest.partition("__")
    if not sep or not server or not mcp_tool:
        return None
    return server, mcp_tool


# ── registry contribution (mirrors service_tools.build_service_tools) ───────

def build_mcp_tools() -> dict[str, tools.Tool]:
    """One ``Tool`` per discovered tool across every enabled MCP server. A
    server that won't connect contributes nothing (logged). Absent the SDK,
    returns ``{}`` — the base install is unaffected."""
    out: dict[str, tools.Tool] = {}
    servers = mcp_config.enabled_servers()
    if not servers:
        return out
    if not _have_sdk():
        _logger.warning("%d MCP server(s) configured but %s", len(servers), _INSTALL_HINT)
        return out
    for sid, server in servers.items():
        conn = get_connection(sid)
        if conn is None:
            continue
        try:
            specs = conn.list_tools()
        except Exception as exc:  # noqa: BLE001 — a bad server contributes nothing
            _logger.warning("MCP server %s: tools/list failed: %s",
                            sid, _safe_exc(exc, server))
            continue
        cost = "paid-cloud" if server.metered else None
        for spec in specs:
            # Server-supplied names/descriptions/schemas are untrusted: a name
            # outside the function-calling-safe charset — or a FINAL namespaced
            # name over the provider function-name ceiling — is skipped (it
            # would poison every model request carrying the registry); a
            # description is capped; an oversized/malformed schema skips the
            # tool (fail closed, never forwarded whole).
            if not _TOOL_NAME_OK.fullmatch(spec.name):
                _logger.warning(
                    "MCP server %s: skipping tool with unsafe name %r",
                    sid, _safe_field(spec.name, server))
                continue
            # The FINAL namespaced name is the provider contract — charset AND
            # length are re-checked on the whole thing (a per-part-valid pair
            # can still overflow, and an id the slug check let through must not
            # reach a model request).
            name = tool_name(sid, spec.name)
            if not _TOOL_NAME_OK.fullmatch(name):
                _logger.warning(
                    "MCP server %s: skipping tool %r — namespaced name is not "
                    "function-calling-safe (charset, or over %d chars — shorten "
                    "the server id)", sid, _safe_field(spec.name, server),
                    _MAX_FUNC_NAME)
                continue
            schema = getattr(spec, "inputSchema", None) or {"type": "object"}
            if not _schema_ok(schema):
                _logger.warning(
                    "MCP server %s: skipping tool %r — oversized or malformed "
                    "input schema", sid, _safe_field(spec.name, server))
                continue
            desc = (getattr(spec, "description", "") or f"{sid}:{spec.name}")
            out[name] = build_server_tool(
                name=name, description=desc, schema=schema, cost=cost,
                server=server, call=_make_call(sid, spec.name),
            )
    return out


def build_server_tool(*, name, description, schema, cost, server, call):
    """Construct one served MCP tool — THE builder that stamps the
    ``mcp-<trust>`` origin from the server record, factored out so the
    origin-completeness guard can enumerate origins from this real
    constructor rather than from expected strings."""
    return tools.Tool(
        name=name,
        description=description[:_MAX_DESC_CHARS],
        call=call,
        params_schema=schema,
        cost_class=cost,
        origin=f"mcp-{server.trust}",
    )


def _schema_ok(schema) -> bool:
    """A server-supplied input schema is forwarded only if it is a dict whose
    serialized size fits the ceiling — one tool must not be able to bloat every
    model request (or blow the JSON encoder) with an unbounded/deep schema."""
    if not isinstance(schema, dict):
        return False
    try:
        import json
        return len(json.dumps(schema)) <= _MAX_SCHEMA_CHARS
    except (TypeError, ValueError, RecursionError):
        return False


def _make_call(server_id: str, mcp_tool: str):
    def _call(**kwargs) -> str:
        return call_tool(server_id, mcp_tool, **kwargs)
    return _call


def shutdown() -> None:
    """Close every open connection, AWAIT each holder's transport teardown
    (so stdio children are terminated, not orphaned), then stop the portal."""
    global _portal
    with _conn_lock:
        conns = list(_connections.values())
        _connections.clear()
    for c in conns:
        try:
            c.close()
        except Exception:  # noqa: BLE001 — best-effort teardown
            pass
    for c in conns:
        try:
            # The holder future completes once its AsyncExitStack has unwound
            # (session closed, child reaped). Bounded — a wedged transport
            # can't hang interpreter exit.
            c._holder.result(timeout=5.0)
        except Exception:  # noqa: BLE001 — best-effort teardown
            c._holder.cancel()
    with _portal_lock:
        if _portal is not None:
            _portal.stop()
            _portal = None


__all__ = [
    "build_mcp_tools",
    "call_tool",
    "get_connection",
    "tool_name",
    "parse_tool_name",
    "shutdown",
]
