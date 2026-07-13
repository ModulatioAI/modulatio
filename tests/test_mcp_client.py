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
    assert mcp_client.tool_name("tracker", "create_item") == "mcp__tracker__create_item"
    assert mcp_client.parse_tool_name("mcp__tracker__create_item") == ("tracker", "create_item")
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


def test_scrub_removes_stdio_env_secrets(monkeypatch):
    """A stdio server's vault-injected env secret echoed in a SUCCESS result
    must be scrubbed — the http token slot is not the only secret."""
    monkeypatch.setenv("MCPKEY_STDIO", "stdio-secret")
    s = McpServer(id="s", name="s", transport="stdio", command="x",
                  env={"TOKEN": "$MCPKEY_STDIO", "PLAIN": "not-secret"})
    out = mcp_client._scrub("echo: stdio-secret", s)
    assert "stdio-secret" not in out
    # non-secret env values are NOT redacted (no over-scrub)
    assert "not-secret" in mcp_client._scrub("has not-secret text", s)


def test_call_tool_failure_text_is_scrubbed(monkeypatch):
    """A protocol exception can carry the server's credential — the failure
    string is scrubbed like a success result before reaching the model."""
    monkeypatch.setenv("MCPKEY_F", "http-secret")
    server = McpServer(id="s", name="s", transport="http", base_url="https://x",
                       env_var="MCPKEY_F")

    class _Boom(_FakeConn):
        def call(self, *a, **k):
            raise RuntimeError("protocol input: http-secret")
    boom = _Boom([])
    boom.server = server
    mcp_client._connections["s"] = boom
    monkeypatch.setattr(mcp_client, "get_connection", lambda sid: boom)
    out = mcp_client.call_tool("s", "do")
    assert "failed" in out and "http-secret" not in out


# ── discovery -> Tool translation + call routing (fake connection) ──────────

class _FakeConn:
    def __init__(self, specs, result="ok"):
        self._specs = specs
        self.result = result
        self.calls = []
        self.server = McpServer(id="fake", name="fake", transport="stdio",
                                command="x")

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


# ── secrets never reach the LOG either ──────────────────────────────────────

def test_call_tool_failure_log_is_scrubbed(monkeypatch, caplog):
    """The caught exception is scrubbed BEFORE logging too — the model result
    and the process log are both retained surfaces."""
    import logging
    monkeypatch.setenv("MCPKEY_LG", "http-secret")
    server = McpServer(id="s", name="s", transport="http", base_url="https://x",
                       env_var="MCPKEY_LG")

    class _Boom(_FakeConn):
        def call(self, *a, **k):
            raise RuntimeError("protocol input: http-secret")
    boom = _Boom([])
    boom.server = server
    mcp_client._connections["s"] = boom
    monkeypatch.setattr(mcp_client, "get_connection", lambda sid: boom)
    with caplog.at_level(logging.WARNING, logger="modulatio.mcp"):
        out = mcp_client.call_tool("s", "do")
    assert "http-secret" not in out
    assert "failed" in caplog.text            # the failure IS logged...
    assert "http-secret" not in caplog.text   # ...without the credential


def test_call_log_scrubs_credential_shaped_tool_name(monkeypatch, caplog):
    """A server can name a TOOL after its own credential — the tool-name log
    field is scrubbed with the server's secret set, not just the exception."""
    import logging
    token = "sk-toolcredential1234"
    monkeypatch.setenv("MCPKEY_TN", token)
    server = McpServer(id="s", name="s", transport="http", base_url="https://x",
                       env_var="MCPKEY_TN")

    class _Boom(_FakeConn):
        def call(self, *a, **k):
            raise RuntimeError("generic failure")
    boom = _Boom([])
    boom.server = server
    mcp_client._connections["s"] = boom
    monkeypatch.setattr(mcp_client, "get_connection", lambda sid: boom)
    with caplog.at_level(logging.WARNING, logger="modulatio.mcp"):
        out = mcp_client.call_tool("s", token)   # the tool NAME is the token
    assert token not in out
    assert "failed" in caplog.text and token not in caplog.text


def test_discovery_skip_warnings_scrub_tool_names(monkeypatch, caplog):
    """Discovery-time skip warnings (unsafe name / final-name overflow / bad
    schema) log server-supplied names scrubbed — a credential-shaped tool name
    must not persist via a rejection message."""
    import logging
    token = "sk-testcredential1234567890"
    monkeypatch.setenv("MCPKEY_DN", token)
    sid = "s" * 32
    mcp_config.add_server(McpServer(id=sid, name="S", transport="http",
                                    base_url="https://x", env_var="MCPKEY_DN"))
    fake = _FakeConn([
        _spec(token),                              # forces final-name overflow
        _spec(f"{token} x"),                       # unsafe charset (space)
        _spec("okname", schema=["bad"]),           # bad schema branch
    ])
    fake.server = mcp_config.get_server(sid)
    monkeypatch.setattr(mcp_client, "get_connection", lambda s: fake)
    with caplog.at_level(logging.WARNING, logger="modulatio.mcp"):
        reg = mcp_client.build_mcp_tools()
    assert reg == {}
    assert "skipping tool" in caplog.text
    assert token not in caplog.text


def test_unicode_server_id_rejected_and_backstopped(monkeypatch):
    """A Unicode id passes str.isalnum but breaks the provider function-name
    charset: rejected at the operator boundary, AND the final-name backstop
    skips it if a record dodges validation entirely."""
    with pytest.raises(ValueError):
        mcp_config.add_server(McpServer(id="serveur-é", name="S",
                                        transport="stdio", command="x"))
    # backstop: a raw record that never saw _validate still can't register
    rogue = McpServer(id="serveur-é", name="S", transport="stdio", command="x")
    fake = _FakeConn([_spec("read")])
    fake.server = rogue
    monkeypatch.setattr(mcp_config, "enabled_servers", lambda: {"serveur-é": rogue})
    monkeypatch.setattr(mcp_client, "get_connection", lambda s: fake)
    assert mcp_client.build_mcp_tools() == {}


def test_connect_failure_log_is_scrubbed(monkeypatch, caplog):
    """Connect-path exceptions are scrubbed before logging as well."""
    import logging
    monkeypatch.setenv("MCPKEY_CN", "conn-secret")
    mcp_config.add_server(McpServer(id="c", name="c", transport="http",
                                    base_url="https://x", env_var="MCPKEY_CN"))

    def _boom(server):
        raise RuntimeError("handshake: conn-secret")
    monkeypatch.setattr(mcp_client, "_have_sdk", lambda: True)
    monkeypatch.setattr(mcp_client, "_Connection", _boom)
    with caplog.at_level(logging.WARNING, logger="modulatio.mcp"):
        assert mcp_client.get_connection("c") is None
    assert "failed to connect" in caplog.text
    assert "conn-secret" not in caplog.text


# ── bounds: server-controlled data is bounded at translation ────────────────

def test_result_text_bounded_accumulation():
    """A giant server-sent content block is sliced at the accumulation budget
    (2× the return cap) instead of fully copied before the cap can act."""
    budget = 2 * mcp_client.tools._HTTP_GET_MAX_CHARS
    giant = SimpleNamespace(content=[SimpleNamespace(text="x" * (budget * 10))])
    out = mcp_client._result_text(giant)
    assert len(out) <= budget
    # many blocks stop accumulating at the budget too
    many = SimpleNamespace(
        content=[SimpleNamespace(text="y" * 10_000) for _ in range(100)])
    assert len(mcp_client._result_text(many)) <= budget + 100  # + join seps


def test_list_tools_caps_discovery():
    """A hostile/buggy discovery list is bounded before registry translation."""
    conn = mcp_client._Connection.__new__(mcp_client._Connection)
    conn.server = McpServer(id="s", name="s", transport="stdio", command="x")

    class _Sess:
        def list_tools(self):
            return "coro-stand-in"
    conn._session = _Sess()
    result = SimpleNamespace(tools=[_spec(f"t{i}") for i in range(300)])
    conn._portal = SimpleNamespace(submit=lambda coro, timeout: result)
    assert len(conn.list_tools()) == mcp_client._MAX_TOOLS


def test_build_mcp_tools_skips_unsafe_names_and_caps_desc(monkeypatch):
    """A server-supplied tool name outside the function-calling-safe charset
    is skipped; a giant description is capped at translation."""
    mcp_config.add_server(McpServer(id="fs", name="FS", transport="stdio", command="x"))
    fake = _FakeConn([
        _spec("ok_tool", desc="d" * 50_000),
        _spec("bad name with spaces"),
        _spec("bad\nnewline"),
        _spec("x" * 200),                      # over-long name
    ])
    monkeypatch.setattr(mcp_client, "get_connection", lambda sid: fake)
    reg = mcp_client.build_mcp_tools()
    assert set(reg) == {"mcp__fs__ok_tool"}
    assert len(reg["mcp__fs__ok_tool"].description) <= mcp_client._MAX_DESC_CHARS


def test_build_mcp_tools_final_name_ceiling(monkeypatch):
    """The FINAL namespaced name (mcp__<id>__<tool>) rides the provider
    64-char function-name ceiling — a per-part-valid combination that exceeds
    it is skipped, not forwarded."""
    sid = "s" * 32                                       # max-allowed id
    mcp_config.add_server(McpServer(id=sid, name="S", transport="stdio", command="x"))
    fake = _FakeConn([_spec("read"), _spec("t" * 60)])   # fits / overflows
    monkeypatch.setattr(mcp_client, "get_connection", lambda s: fake)
    reg = mcp_client.build_mcp_tools()
    assert set(reg) == {f"mcp__{sid}__read"}
    assert all(len(n) <= mcp_client._MAX_FUNC_NAME for n in reg)


def test_build_mcp_tools_bounds_schema(monkeypatch):
    """An oversized or malformed input schema skips the tool (fail closed) —
    one tool can't bloat every model request."""
    mcp_config.add_server(McpServer(id="fs", name="FS", transport="stdio", command="x"))
    giant = {"type": "object", "description": "g" * 200_000}
    fake = _FakeConn([
        _spec("ok", schema={"type": "object", "properties": {}}),
        _spec("fat", schema=giant),
        _spec("weird", schema=["not", "a", "dict"]),
    ])
    monkeypatch.setattr(mcp_client, "get_connection", lambda sid: fake)
    reg = mcp_client.build_mcp_tools()
    assert set(reg) == {"mcp__fs__ok"}


def test_result_text_charges_separators_and_empty_blocks():
    """Empty text blocks are charged their join separator — a flood of them
    can't grow the parts list or the joined output past the budget."""
    budget = 2 * mcp_client.tools._HTTP_GET_MAX_CHARS
    flood = SimpleNamespace(
        content=[SimpleNamespace(text="") for _ in range(budget + 10_000)])
    out = mcp_client._result_text(flood)
    assert len(out) <= budget


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


@pytest.mark.live
def test_live_connect_timeout_reaps_the_child(tmp_path, monkeypatch):
    """A server that starts but never answers ``initialize``: the connect
    timeout must CANCEL the holder (unwinding the transport, terminating the
    child) — not abandon a live subprocess per retry."""
    pytest.importorskip("mcp")
    import subprocess
    import sys
    monkeypatch.setattr(mcp_config, "MCP_SERVERS_FILE", tmp_path / "mcp.json")
    monkeypatch.setattr(mcp_client, "_connections", {})
    monkeypatch.setattr(mcp_client, "_CONNECT_TIMEOUT", 2.0)
    marker = f"mcp-hang-marker-{tmp_path.name}"
    hang_py = tmp_path / "hang.py"
    hang_py.write_text(
        f"# {marker}\n"
        "import sys\n"
        "sys.stdin.read()\n"   # consume forever, never reply
    )
    mcp_config.add_server(McpServer(id="hang", name="Hang", transport="stdio",
                                    command=sys.executable, args=(str(hang_py),)))
    try:
        assert mcp_client.get_connection("hang") is None   # times out, degrades
        # the cancelled holder unwinds the transport → the child is terminated
        import time
        for _ in range(25):                                # up to ~5s to reap
            out = subprocess.run(["pgrep", "-f", marker],
                                 capture_output=True, text=True).stdout.strip()
            if not out:
                break
            time.sleep(0.2)
        assert out == "", f"hung child left alive (pids: {out})"
    finally:
        mcp_client.shutdown()
