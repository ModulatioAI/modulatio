# MCP client — consuming external MCP servers

## What & why

Modulatio's agents act through **tools** (named callables in a per-run registry).
Today those tools are either built-in (`web_search`, `run_shell`, …) or come from
operator-configured outside **services** (`generate_image`, `api_call`). The MCP
(Model Context Protocol) ecosystem is a large, standard-shaped supply of external
tools; this feature lets Modulatio act as an **MCP client** so an operator can
plug an MCP server in and its tools become first-class Modulatio tools.

Direction is **client only** (Modulatio consumes; it does not expose itself as an
MCP server — the ACP server already covers "drive Modulatio from outside").

## The controlling design principle

**MCP is a fourth kind of capability provider, and the tool registry is already
the universal sink every provider feeds.** So MCP reuses Modulatio's whole
control plane instead of re-earning it:

| Concern | Existing seam MCP reuses |
| --- | --- |
| Tools reach agents | the run's `tool_registry` (`dict[str, Tool]`) |
| Config CRUD + write-only keys | `config` store + `write_secret_file` / key vault |
| Producer access | a tool reaches a producer only if a skill's `tool_loadout` names it |
| Leader-lane authorization | `leader_gate` (`extract_tool_requests` → `decide` → once/session/always/deny) |
| Metering | `comptroller` via `Tool.cost_class` + the metered-authorizer |
| Subprocess safety | the sandbox's process-group reaping + rlimits + call-timeout |
| JSON-RPC framing | `acp/jsonrpc.py` |

**New code is only the MCP client itself**: the protocol/transport layer (an
opt-in `[mcp]` extra pulling the official `mcp` SDK) plus a thin Modulatio adapter
that manages the operator's server list, opens/pools connections, translates
discovered MCP tools into `Tool` objects, and routes calls.

## Scope

- **Transports:** both local **stdio** (Modulatio spawns the server subprocess)
  and remote **http/SSE** (Modulatio connects out with an auth token).
- **Consumers:** the interactive **Leader** (each gated MCP tool call approved
  once/session/always via `leader_gate`); **producers** only via an explicit
  `tool_loadout` grant (jobs never block — loadout membership *is* the
  authorization).
- **Not in this build (clean seam kept for later):** MCP-server mode (#exposing);
  installable capability packs (#2); Python-entry-point plugins (#3). The
  provider-config handling is kept from hardcoding "services + MCP" as the only
  two kinds, so those slot in as further provider types later without a fork.

## Components

### `mcp_config.py` — the server record + store
```
@dataclass McpServer:
    id: str                 # slug
    name: str               # label
    transport: str          # "stdio" | "http"
    # stdio:
    command: str = ""       # executable
    args: tuple[str, ...] = ()
    env: dict[str, str] = {} # non-secret env (secret-shaped keys go to the vault)
    # http:
    base_url: str = ""
    auth_shape: str = ""    # "" | "bearer" | "header:<Name>"
    env_var: str = ""       # vault slot name for the token (reused from Service)
    # both:
    trust: str = "gated"    # "gated" | "trusted"
    metered: bool = False   # tools route through the comptroller
    enabled: bool = True
```
Persisted to `<data>/mcp_servers.json` via the config store; the http token and
any secret-shaped stdio env value live write-only in the key vault
(`write_secret_file` / `set_env_secret`), never in the record. CRUD mutators
serialize their read-modify-write under a module lock (same posture as
`services`). **Tools are NOT declared here** — an MCP server's tools are
discovered live at connect (`tools/list`); the record stores only how to reach
it, plus a short-TTL discovered-tools cache.

### `mcp_client.py` — connection manager + adapter
- **Connection manager:** lazy — a connection opens on first need (a Leader lane
  with an enabled server, or a producer run whose loadout names an `mcp__…`
  tool), one pooled connection per server, reused across calls, torn down on
  run-end / idle timeout / interpreter shutdown. stdio subprocesses are reaped by
  process group (reuse of the sandbox reaper); http uses a pooled client.
- **Async bridge:** the `mcp` SDK is asyncio; Modulatio tools are synchronous.
  The manager runs one dedicated event-loop thread (a portal); each sync tool
  `call` submits a coroutine via `run_coroutine_threadsafe(...).result(timeout)`.
  The `ClientSession` lives in that loop for the connection's lifetime.
- **Secrets:** the http token / secret stdio env values are read from the vault
  and injected at this layer (subprocess env or request headers), never entering
  model context. Tool arguments and results are secret-scrubbed (reuse of
  `service_tools`' redaction) before returning to the model.
- **`build_mcp_tools() -> dict[str, Tool]`:** for each enabled server, connect +
  `tools/list`; each MCP tool becomes a `Tool` named `mcp__<server>__<tool>`
  (function-calling-safe charset, collision-free), description + `params_schema`
  from the MCP `inputSchema`, `cost_class="paid-cloud"` iff `server.metered`, and
  a `call` closure that routes to `mcp_client.call(server, tool, **kwargs)`.
  Absent the `[mcp]` extra, returns `{}` and logs the install hint — the base
  install is unaffected.
- **Errors:** a connection/spawn/handshake/http failure makes the tool return a
  clear `"MCP server X unavailable: …"` string (never crashes a run); one
  reconnect attempt then degrade; per-call timeout (reuse of the call-timeout);
  output capped (reuse of `_cap_http_body`).

## Wiring (the whole integration footprint)

1. `tools.build_registry()` — after the service-tools merge, one line:
   `registry.update(mcp_client.build_mcp_tools())`.
2. `leader_gate.extract_tool_requests()` — recognize a `mcp__…` tool name: for a
   **gated** server emit one `SecurityRequest(action="mcp-tool",
   resource="<server>:<tool>", why=<description>)` so `decide` gives
   once/session/always/deny; for a **trusted** server emit nothing (auto-allow).
   The rest of the gate machinery is unchanged.
3. Producers — **no change**. An `mcp__…` Tool is grantable by a skill's
   `tool_loadout` exactly like any tool.
4. Metering — **no change**. `cost_class="paid-cloud"` already routes through the
   metered authorizer.
5. `modulatio mcp` CLI — `add` (stdio: command/args/env; http: url/auth) / `list`
   / `remove` / `enable` / `disable` / `trust` / `test` (connect + list tools).
   The graphical SERVICES-tab section (TUI + WebOS) is a fast-follow over this
   same config API.

## Error handling & degradation

Every failure degrades to an honest tool-result string and a log line — never a
run crash (consistent with the stability discipline). A server that vanishes
mid-run reconnects once, then reports unavailable. The `[mcp]` extra absent →
MCP config is inert with an install hint.

## Testing

- `test_mcp_config` — CRUD, persistence, secret-to-vault (never in the record),
  RMW serialization, validation.
- `test_mcp_client` — discovery→Tool translation, `mcp__` namespacing,
  `cost_class` from `metered`, secret injection + result scrub, timeout + cap,
  graceful degrade on connection failure — all against an **in-process fake**
  `ClientSession` (no real subprocess, runs without the SDK).
- `test_mcp_wiring` — `build_registry` includes MCP tools; `extract_tool_requests`
  emits a gated request for a gated server and nothing for a trusted server; a
  producer loadout naming `mcp__…` reaches the tool.
- One `@pytest.mark.live` round-trip against a real reference MCP server (opt-in,
  like the other live tests).

## Future-proofing (nearly free)

The `Tool` registry sink is already universal; MCP `build_mcp_tools` produces
`Tool`s the same way a future pack-installer or Python-entry-point plugin would.
The only forward-looking care taken: provider-config handling isn't hardcoded to
"services + MCP", so packs (#2) and plugins (#3) become further provider types
using the same store + tab pattern rather than a fork. That generality is **not**
built now (YAGNI) — only left unblocked.
