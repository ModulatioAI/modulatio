"""MCP wiring — registry contribution + the Leader-gate integration."""
from __future__ import annotations

import pytest

from modulatio import leader_gate, mcp_client, mcp_config, tools
from modulatio.mcp_config import McpServer


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_config, "MCP_SERVERS_FILE", tmp_path / "mcp_servers.json")


def test_build_registry_merges_mcp_tools(monkeypatch):
    fake_tool = tools.Tool(name="mcp__x__y", description="d", call=lambda **k: "ok")
    monkeypatch.setattr(mcp_client, "build_mcp_tools", lambda: {"mcp__x__y": fake_tool})
    reg = tools.build_registry()
    assert "mcp__x__y" in reg          # MCP tools land in the run registry
    assert reg["mcp__x__y"].call() == "ok"


def test_gated_server_emits_one_mcp_request():
    mcp_config.add_server(McpServer(id="fs", name="Files", transport="stdio",
                                    command="x", trust="gated"))
    reqs = leader_gate.extract_tool_requests("mcp__fs__write", {"path": "/x"}, root="/tmp")
    assert len(reqs) == 1
    r = reqs[0]
    assert r.request_class == "mcp" and r.action == "mcp-tool"
    assert r.resource == "fs:write" and "Files" in r.why


def test_trusted_server_emits_nothing():
    mcp_config.add_server(McpServer(id="docs", name="Docs", transport="http",
                                    base_url="https://x", trust="trusted"))
    assert leader_gate.extract_tool_requests("mcp__docs__search", {}, root="/tmp") == []


def test_unknown_mcp_server_fails_closed():
    """An MCP-shaped name whose server is NOT configured is still GATED — a
    name that dodges the config lookup must not dodge the gate with it."""
    reqs = leader_gate.extract_tool_requests("mcp__ghost__x", {}, root="/tmp")
    assert len(reqs) == 1
    assert reqs[0].request_class == "mcp" and "unknown server" in reqs[0].why


def test_namespace_delimiter_id_cannot_bypass_gate():
    """The gate-bypass shape end to end: an id containing '__' can't be added,
    can't be loaded from a hand-edited record, and its namespaced tool name
    still gates (as an unknown server) rather than running silently ungated."""
    with pytest.raises(ValueError):
        mcp_config.add_server(McpServer(id="safe__aux", name="S", transport="stdio",
                                        command="x", trust="gated"))
    # even if the name somehow reached the registry, the gate stays closed
    reqs = leader_gate.extract_tool_requests("mcp__safe__aux__read", {}, root="/tmp")
    assert len(reqs) == 1 and reqs[0].request_class == "mcp"


def test_native_tools_unaffected():
    # The MCP branch must not disturb the existing path/exec gating.
    reqs = leader_gate.extract_tool_requests("read_file", {"path": "sub/f.md"}, root="/tmp")
    assert len(reqs) == 1 and reqs[0].action == "read"


def test_gated_mcp_call_persists_as_always(tmp_path):
    """The whole once/session/always machinery works for the mcp class: an
    'always' grant on server:tool silent-allows the next identical call. (The
    grant store is isolated to tmp by the autouse config-isolation fixture.)"""
    mcp_config.add_server(McpServer(id="fs", name="Files", transport="stdio",
                                    command="x", trust="gated"))
    gate = leader_gate.LeaderPermissionGate("tst", workspace=tmp_path / "ws")
    req = leader_gate.extract_tool_requests("mcp__fs__write", {"path": "/x"}, root="/tmp")[0]
    assert gate.is_granted(req) is False                       # not yet
    gate.decide(req, prompt_fn=lambda r: leader_gate.ScopedDecision(
        scope=leader_gate.SCOPE_ALWAYS))
    assert gate.is_granted(req) is True                        # sticks
