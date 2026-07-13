# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""MCP servers — the operator-configured list of external MCP servers.

Design: docs/design/mcp-client.md. An MCP server is an external supplier of
tools reached over the Model Context Protocol, in one of two transports:
``stdio`` (Modulatio spawns the server subprocess) or ``http`` (Modulatio
connects out with an auth token). The registry (``mcp_servers.json`` in the
config dir) records how to REACH each server; the tools it offers are
discovered live at connect (``tools/list``), not declared here. Secrets — an
http auth token, or a secret-shaped stdio env value — live write-only in the
vault (``config.set_env_secret``); this module never persists a secret.

An MCP server is a capability provider alongside the built-in tools and the
service-API pool: its tools become ``tools.Tool`` objects in the run registry.
The trust posture is per-server: ``gated`` calls go through the Leader
permission gate (once/session/always/deny); ``trusted`` calls run without a
prompt. Producers reach an MCP tool only when a skill's ``tool_loadout`` names
it (the loadout grant IS the authorization — jobs never block).
"""
from __future__ import annotations

import json
import re
import threading
from dataclasses import asdict, dataclass, field
from typing import Optional

from modulatio import config

MCP_SERVERS_FILE = config.CONFIG_DIR / "mcp_servers.json"

_TRANSPORTS = ("stdio", "http")
_TRUST = ("gated", "trusted")


@dataclass(frozen=True)
class McpServer:
    id: str          # slug
    name: str        # human label
    transport: str   # "stdio" | "http"
    # stdio:
    command: str = ""
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)   # NON-secret env only
    # http:
    base_url: str = ""
    auth_shape: str = ""   # "" | "bearer" | "header:<Name>"
    env_var: str = ""      # vault slot holding the http token (never the token)
    # both:
    trust: str = "gated"   # "gated" | "trusted"
    metered: bool = False  # tool calls route through the comptroller
    enabled: bool = True


#: HTTP header-name charset (alnum + hyphen) — anything else (whitespace, a
#: CR/LF, a colon) is a framing hazard, rejected at the operator boundary.
_HEADER_NAME = re.compile(r"^[A-Za-z0-9-]+$")
#: Vault slot names — conventional env-var shape.
_ENV_VAR = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _validate(s: McpServer) -> None:
    if not s.id or not s.id.replace("-", "").replace("_", "").isalnum():
        raise ValueError(f"mcp server id {s.id!r} must be a simple slug")
    if "__" in s.id:
        # "__" is the tool-namespace delimiter (mcp__<server>__<tool>) — an id
        # containing it makes the namespace ambiguous and the gate lookup miss.
        raise ValueError(f"mcp server id {s.id!r} must not contain '__'")
    if s.transport not in _TRANSPORTS:
        raise ValueError(f"mcp transport {s.transport!r} must be stdio|http")
    if s.trust not in _TRUST:
        raise ValueError(f"mcp trust {s.trust!r} must be gated|trusted")
    if s.env_var and not _ENV_VAR.match(s.env_var):
        raise ValueError(f"mcp server {s.id!r}: env_var {s.env_var!r} is not a valid slot name")
    if s.transport == "stdio":
        if not s.command:
            raise ValueError(f"mcp server {s.id!r}: stdio needs a command")
    else:  # http
        if not s.base_url.startswith(("https://", "http://")):
            raise ValueError(
                f"mcp server {s.id!r}: http needs an absolute base_url"
            )
        shape = s.auth_shape
        if shape and shape != "bearer":
            name = shape.split(":", 1)[1] if shape.startswith("header:") else None
            if name is None or not _HEADER_NAME.match(name):
                raise ValueError(
                    f"mcp server {s.id!r}: auth_shape {shape!r} must be "
                    "bearer or header:<Name>"
                )


def _load_raw() -> dict:
    if not MCP_SERVERS_FILE.exists():
        return {}
    try:
        data = json.loads(
            MCP_SERVERS_FILE.read_text(encoding="utf-8", errors="replace")
        )
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


# Serializes the load->mutate->save cycles below within one process (the WebOS
# server's concurrent request threads are the realistic race). Each write is
# already atomic (write_secret_file); the lock stops two in-process writers
# from reading the same base state and dropping one update.
_mcp_lock = threading.Lock()


def _save_raw(data: dict) -> None:
    config.write_secret_file(MCP_SERVERS_FILE, json.dumps(data, indent=2))


def load_servers() -> dict[str, McpServer]:
    out: dict[str, McpServer] = {}
    for sid, entry in _load_raw().get("servers", {}).items():
        if not isinstance(entry, dict):
            continue  # one corrupt entry must not hide the rest
        try:
            server = McpServer(
                id=sid,
                name=str(entry.get("name", sid)),
                transport=str(entry.get("transport", "stdio")),
                command=str(entry.get("command", "")),
                args=tuple(entry.get("args", ())),
                env=dict(entry.get("env", {})),
                base_url=str(entry.get("base_url", "")),
                auth_shape=str(entry.get("auth_shape", "")),
                env_var=str(entry.get("env_var", "")),
                trust=str(entry.get("trust", "gated")),
                metered=bool(entry.get("metered", False)),
                enabled=bool(entry.get("enabled", True)),
            )
            # A hand-edited record faces the same bar as add_server — an entry
            # that would be rejected at add (an ambiguous "__" id, a malformed
            # auth_shape) must not enter the run either.
            _validate(server)
            out[sid] = server
        except (TypeError, ValueError):
            continue  # one corrupt entry must not hide the rest
    return out


def get_server(server_id: str) -> Optional[McpServer]:
    return load_servers().get(server_id)


def enabled_servers() -> dict[str, McpServer]:
    return {sid: s for sid, s in load_servers().items() if s.enabled}


def add_server(s: McpServer) -> None:
    _validate(s)
    with _mcp_lock:
        data = _load_raw()
        entry = asdict(s)
        entry.pop("id")
        entry["args"] = list(s.args)
        data.setdefault("servers", {})[s.id] = entry
        _save_raw(data)


def remove_server(server_id: str) -> bool:
    with _mcp_lock:
        data = _load_raw()
        if server_id not in data.get("servers", {}):
            return False
        del data["servers"][server_id]
        _save_raw(data)
        return True


def _update(server_id: str, **fields) -> bool:
    with _mcp_lock:
        data = _load_raw()
        entry = data.get("servers", {}).get(server_id)
        if not isinstance(entry, dict):
            return False
        entry.update(fields)
        _save_raw(data)
        return True


def set_enabled(server_id: str, enabled: bool) -> bool:
    return _update(server_id, enabled=bool(enabled))


def set_trust(server_id: str, trust: str) -> bool:
    if trust not in _TRUST:
        raise ValueError(f"mcp trust {trust!r} must be gated|trusted")
    return _update(server_id, trust=trust)


__all__ = [
    "McpServer",
    "MCP_SERVERS_FILE",
    "load_servers",
    "get_server",
    "enabled_servers",
    "add_server",
    "remove_server",
    "set_enabled",
    "set_trust",
]
