"""MCP server config store — CRUD, validation, secret-never-in-record."""
from __future__ import annotations

import json

import pytest

from modulatio import mcp_config
from modulatio.mcp_config import McpServer


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_config, "MCP_SERVERS_FILE", tmp_path / "mcp_servers.json")


def _stdio(sid="git", **kw):
    return McpServer(id=sid, name=kw.pop("name", sid), transport="stdio",
                     command=kw.pop("command", "run-server"), args=kw.pop("args", ("srv",)), **kw)


def test_add_load_roundtrip():
    mcp_config.add_server(_stdio())
    got = mcp_config.get_server("git")
    assert got is not None
    assert got.transport == "stdio" and got.command == "run-server" and got.args == ("srv",)
    assert got.trust == "gated" and got.enabled is True and got.metered is False


def test_add_http_and_flags():
    mcp_config.add_server(McpServer(
        id="hub", name="Hub", transport="http",
        base_url="https://mcp.example/api", auth_shape="bearer",
        env_var="MCPKEY_ABC", trust="trusted", metered=True))
    s = mcp_config.get_server("hub")
    assert s.base_url == "https://mcp.example/api" and s.auth_shape == "bearer"
    assert s.trust == "trusted" and s.metered is True


def test_record_never_holds_a_secret():
    # env_var is a vault SLOT NAME, not the token — the record file must be
    # readable without exposing any credential.
    mcp_config.add_server(McpServer(
        id="hub", name="Hub", transport="http",
        base_url="https://x.example", auth_shape="bearer", env_var="MCPKEY_XYZ"))
    raw = mcp_config.MCP_SERVERS_FILE.read_text()
    assert "MCPKEY_XYZ" in raw          # the slot name is fine
    assert "secret" not in raw.lower()  # no token value


def test_validation_rejects_bad_shape():
    with pytest.raises(ValueError):
        mcp_config.add_server(McpServer(id="x", name="x", transport="ftp"))
    with pytest.raises(ValueError):
        mcp_config.add_server(McpServer(id="x", name="x", transport="stdio", command=""))
    with pytest.raises(ValueError):
        mcp_config.add_server(McpServer(id="x", name="x", transport="http", base_url="not-a-url"))
    with pytest.raises(ValueError):
        mcp_config.add_server(_stdio(trust="maybe"))


def test_validation_rejects_namespace_delimiter_in_id():
    """'__' is the tool-namespace delimiter — an id containing it would make
    mcp__<id>__<tool> parse to a DIFFERENT (unconfigured) server and dodge the
    per-server gate lookup."""
    with pytest.raises(ValueError):
        mcp_config.add_server(_stdio("safe__aux"))


def test_validation_rejects_malformed_auth():
    base = dict(id="h", name="h", transport="http", base_url="https://x")
    # header-name framing hazards rejected at the operator boundary
    for shape in ("header:", "header:X\r\nInjected: y", "header:has space",
                  "query:api_key", "made-up-shape"):
        with pytest.raises(ValueError):
            mcp_config.add_server(McpServer(**base, auth_shape=shape))
    # the two supported shapes pass
    mcp_config.add_server(McpServer(**base, auth_shape="bearer"))
    mcp_config.remove_server("h")
    mcp_config.add_server(McpServer(**base, auth_shape="header:X-Api-Key"))
    # env_var must be a conventional slot name
    with pytest.raises(ValueError):
        mcp_config.add_server(McpServer(id="e", name="e", transport="http",
                                        base_url="https://x",
                                        env_var="$(rm -rf x)"))


def test_load_skips_hand_edited_invalid_record():
    """A hand-edited record faces the same bar as add_server: an entry with an
    ambiguous '__' id (the gate-bypass shape) must not enter the run."""
    mcp_config.add_server(_stdio("good"))
    data = json.loads(mcp_config.MCP_SERVERS_FILE.read_text())
    data["servers"]["safe__aux"] = {
        "name": "sneaky", "transport": "stdio", "command": "x"}
    mcp_config.MCP_SERVERS_FILE.write_text(json.dumps(data))
    assert set(mcp_config.load_servers()) == {"good"}


def test_enable_disable_trust_and_enabled_filter():
    mcp_config.add_server(_stdio("a"))
    mcp_config.add_server(_stdio("b"))
    assert mcp_config.set_enabled("b", False) is True
    assert set(mcp_config.enabled_servers()) == {"a"}
    assert mcp_config.set_trust("a", "trusted") is True
    assert mcp_config.get_server("a").trust == "trusted"
    with pytest.raises(ValueError):
        mcp_config.set_trust("a", "nope")


def test_remove():
    mcp_config.add_server(_stdio())
    assert mcp_config.remove_server("git") is True
    assert mcp_config.remove_server("git") is False
    assert mcp_config.get_server("git") is None


def test_corrupt_entry_does_not_hide_the_rest():
    mcp_config.add_server(_stdio("good"))
    data = json.loads(mcp_config.MCP_SERVERS_FILE.read_text())
    data["servers"]["bad"] = "not-a-dict"
    mcp_config.MCP_SERVERS_FILE.write_text(json.dumps(data))
    assert set(mcp_config.load_servers()) == {"good"}
