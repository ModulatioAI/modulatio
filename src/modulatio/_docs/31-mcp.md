# MCP servers

Modulatio's agents act through **tools**. Beyond the built-ins and the outside
services you configure, Modulatio can consume **MCP servers** — external
suppliers of tools that speak the Model Context Protocol. Plug one in and its
tools become first-class Modulatio tools: the Leader can use them, and a
producer can when a skill grants it.

## Install

MCP support is an opt-in extra so the base install stays lean:

```bash
pip install "modulatio[mcp]"
```

Without it, a configured MCP server is inert and Modulatio prints an install
hint — nothing else changes.

## Add a server

Two transports. A **stdio** server is a program Modulatio launches on your own
machine; an **http** server is a hosted endpoint Modulatio connects out to.

```bash
# local (stdio) — Modulatio spawns the subprocess
modulatio mcp add-stdio files --command /path/to/files-server --arg /home/me/notes

# remote (http) — a hosted MCP endpoint, with an auth token
modulatio mcp add-http hub --url https://mcp.example.com/api --auth bearer --token <TOKEN>
```

The token is stored **write-only** in the vault; the server record never holds a
secret. A server id is an ASCII slug (letters, digits, `-`, `_`), 32 chars or
fewer, with no `__` — the id rides the `mcp__<server>__<tool>` function name,
so those limits keep every tool name valid for the model providers. List,
test, and manage them:

```bash
modulatio mcp list
modulatio mcp test files      # connect + show the tools it offers
modulatio mcp disable files   # keep the config, stop using it
modulatio mcp remove files
```

## How its tools reach the team

Each discovered tool is registered as `mcp__<server>__<tool>` (e.g.
`mcp__files__read_file`).

- **The Leader** can use any enabled server's tools. Each call is authorized the
  same way `/work` folder access is — the operator approves it **once**, for the
  **session**, or **always** (then never asked again), or denies it. A
  prompt-injected Leader can't silently fire an external tool.
- **Producers run unattended**, so they never prompt. A producer gets a specific
  MCP tool only when a skill's `tool_loadout` names it — that grant *is* the
  authorization.

## Trust and cost

- **Trust posture** is per server. A server that only reads (a docs search, say)
  can be marked **trusted** — its tools run in the Leader lane with no prompt.
  Leave a server that can write or run commands **gated** (the default): one
  "always" tap and it's silent thereafter, but the seatbelt is there for the
  first use.

  ```bash
  modulatio mcp trust docs trusted
  ```

- **Metered** servers (a hosted MCP that calls a paid API) route each tool call
  through the spend gate like any paid service. Off by default; flag it with
  `--metered` when you add the server.

## Safety notes

- A stdio server is a subprocess you chose to trust when you configured its
  command. It runs under Modulatio's resource limits and timeouts, and its tool
  calls are still gated per the trust posture — but it is not namespace-isolated
  (a filesystem server needs real filesystem access to be useful). Add servers
  whose command you trust.
- A server's auth token is injected at the connection layer and never enters an
  agent's context; tool results are scrubbed of the token before an agent sees
  them.
- If a server won't connect or a call fails, the tool returns a clear
  "unavailable" message — a bad server never crashes a run.
- **Reload services** (the terminal's `/reload`, or the WebOS Agents
  sub-page button) closes held MCP connections — and any stdio server
  subprocesses — so the next use reconnects against the current config.
  Edit a server's command or token, reload, done.
