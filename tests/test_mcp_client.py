"""MCP client adapter — namespacing, tool translation, secrets, degrade.

These run WITHOUT the mcp SDK: the SDK boundary (the live connection) is faked,
so only the Modulatio adapter logic is under test. The real round-trip lives in
the ``@pytest.mark.live`` test at the bottom.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from modulatio import mcp_client, mcp_config
from modulatio.mcp_config import McpServer


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_config, "MCP_SERVERS_FILE", tmp_path / "mcp_servers.json")
    # never touch a real portal in unit tests
    monkeypatch.setattr(mcp_client, "_connections", {})


# ── namespacing ─────────────────────────────────────────────────────────────

def test_namespacing_roundtrip():
    assert mcp_client.tool_name("github", "create_issue") == "mcp__github__create_issue"
    assert mcp_client.parse_tool_name("mcp__github__create_issue") == ("github", "create_issue")
    assert mcp_client.parse_tool_name("web_search") is None
    assert mcp_client.parse_tool_name("mcp__nope") is None  # missing tool half


# ── secrets: injected here, never surfaced ──────────────────────────────────

def test_http_headers_from_vault(monkeypatch):
    monkeypatch.setenv("MCPKEY_T", "s3cr3t")
    s = McpServer(id="h", name="h", transport="http", base_url="https://x",
                  auth_shape="bearer", env_var="MCPKEY_T")
    assert mcp_client._http_headers(s) == {"Authorization": "Bearer s3cr3t"}
    s2 = McpServer(id="h", name="h", transport="http", base_url="https://x",
                   auth_shape="header:X-Api-Key", env_var="MCPKEY_T")
    assert mcp_client._http_headers(s2) == {"X-Api-Key": "s3cr3t"}


def test_stdio_env_resolves_vault_refs(monkeypatch):
    monkeypatch.setenv("MCPKEY_E", "tok")
    s = McpServer(id="s", name="s", transport="stdio", command="x",
                  env={"PLAIN": "v", "TOKEN": "$MCPKEY_E"})
    assert mcp_client._stdio_env(s) == {"PLAIN": "v", "TOKEN": "tok"}


def test_scrub_removes_token(monkeypatch):
    monkeypatch.setenv("MCPKEY_S", "leakme")
    s = McpServer(id="s", name="s", transport="http", base_url="https://x",
                  env_var="MCPKEY_S")
    assert "leakme" not in mcp_client._scrub("result with leakme in it", s)


# ── discovery -> Tool translation + call routing (fake connection) ──────────

class _FakeConn:
    def __init__(self, specs, result="ok"):
        self._specs = specs
        self.result = result
        self.calls = []

    def list_tools(self):
        return self._specs

    def call(self, tool_name, arguments):
        self.calls.append((tool_name, arguments))
        return self.result

    def close(self):
        pass


def _spec(name, desc="d", schema=None):
    return SimpleNamespace(name=name, description=desc,
                           inputSchema=schema or {"type": "object"})


def test_build_mcp_tools_translates_and_routes(monkeypatch):
    mcp_config.add_server(McpServer(id="fs", name="FS", transport="stdio", command="x"))
    fake = _FakeConn([_spec("read", "read a file"), _spec("write")], result="done")
    monkeypatch.setattr(mcp_client, "get_connection", lambda sid: fake if sid == "fs" else None)

    reg = mcp_client.build_mcp_tools()
    assert set(reg) == {"mcp__fs__read", "mcp__fs__write"}
    t = reg["mcp__fs__read"]
    assert t.description == "read a file" and t.cost_class is None
    # the Tool.call routes to the connection with the mcp tool name + kwargs
    assert t.call(path="/x") == "done"
    assert fake.calls == [("read", {"path": "/x"})]


def test_metered_server_marks_cost_class(monkeypatch):
    mcp_config.add_server(McpServer(id="pay", name="Pay", transport="http",
                                    base_url="https://x", metered=True))
    monkeypatch.setattr(mcp_client, "get_connection",
                        lambda sid: _FakeConn([_spec("gen")]))
    reg = mcp_client.build_mcp_tools()
    assert reg["mcp__pay__gen"].cost_class == "paid-cloud"


# ── graceful degradation ────────────────────────────────────────────────────

def test_no_servers_is_empty_noop():
    assert mcp_client.build_mcp_tools() == {}


def test_sdk_absent_degrades_with_hint(monkeypatch):
    mcp_config.add_server(McpServer(id="fs", name="FS", transport="stdio", command="x"))
    monkeypatch.setattr(mcp_client, "_have_sdk", lambda: False)
    assert mcp_client.build_mcp_tools() == {}          # inert, no crash
    assert mcp_client.get_connection("fs") is None


def test_call_tool_unreachable_returns_error(monkeypatch):
    monkeypatch.setattr(mcp_client, "get_connection", lambda sid: None)
    out = mcp_client.call_tool("gone", "do")
    assert "unavailable" in out and "gone" in out


def test_call_tool_failure_drops_connection(monkeypatch):
    class _Boom(_FakeConn):
        def call(self, *a, **k):
            raise RuntimeError("kaboom")
    boom = _Boom([])
    mcp_client._connections["s"] = boom
    monkeypatch.setattr(mcp_client, "get_connection", lambda sid: boom)
    out = mcp_client.call_tool("s", "do", x=1)
    assert "failed" in out and "kaboom" in out
    assert "s" not in mcp_client._connections   # dropped so next call reconnects


# ── the real round-trip (opt-in) ────────────────────────────────────────────

@pytest.mark.live
def test_live_stdio_roundtrip(tmp_path, monkeypatch):
    """A real stdio MCP server: connect, discover, call. Needs the mcp SDK."""
    pytest.importorskip("mcp")
    import sys
    monkeypatch.setattr(mcp_config, "MCP_SERVERS_FILE", tmp_path / "mcp.json")
    server_py = tmp_path / "srv.py"
    server_py.write_text(
        "import asyncio\n"
        "from mcp.server import Server\n"
        "from mcp.server.stdio import stdio_server\n"
        "import mcp.types as t\n"
        "app = Server('tiny')\n"
        "@app.list_tools()\n"
        "async def lt():\n"
        "    return [t.Tool(name='echo', description='echo',"
        " inputSchema={'type':'object','properties':{'msg':{'type':'string'}}})]\n"
        "@app.call_tool()\n"
        "async def ct(name, args):\n"
        "    return [t.TextContent(type='text', text='echo: '+args.get('msg',''))]\n"
        "async def _run():\n"
        "    async with stdio_server() as (r, w):\n"
        "        await app.run(r, w, app.create_initialization_options())\n"
        "asyncio.run(_run())\n"
    )
    mcp_config.add_server(McpServer(id="tiny", name="Tiny", transport="stdio",
                                    command=sys.executable, args=(str(server_py),)))
    try:
        conn = mcp_client.get_connection("tiny")
        assert conn is not None
        assert [s.name for s in conn.list_tools()] == ["echo"]
        assert conn.call("echo", {"msg": "hi"}) == "echo: hi"
    finally:
        mcp_client.shutdown()
