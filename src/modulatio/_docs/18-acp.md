# ACP — drive Modulatio from an editor

Modulatio's conversational Leader isn't only reachable inside the TUI. The
**Agent Client Protocol (ACP)** server exposes him over JSON-RPC-on-stdio, so any
ACP-speaking client — a Zed-class editor — can drive him: send a prompt, watch
the activity, and approve the tools he wants to run.

It's the same Leader, the same per-project memory: a turn over ACP and a turn in
the TUI continue **one conversation**.

---

## Running it

```bash
modulatio acp --code <PROJECT>
```

The process speaks JSON-RPC frames on **stdin/stdout** (newline-delimited JSON)
and nothing else — all logs go to stderr, so the protocol stream stays clean for
the client. It runs until the client closes the connection (EOF).

You'll usually configure this as the *agent command* in your editor's ACP
settings rather than running it by hand.

> A configured leader model is required (Modulatio's `leader` default) — wire one
> with `modulatio setup` first. Use `--stub` for an offline, model-free smoke of
> the transport.

---

## What the client gets

| ACP method | What happens |
|---|---|
| `initialize` | Handshake — protocol version + capabilities. |
| `session/new` | Builds the Leader for `<PROJECT>`; returns a session id. |
| `session/prompt` | Runs the Leader's turn; returns his **full reply** (`stopReason: end_turn`). |
| `session/update` | Live activity — the Leader thinking, tool calls, any kickoff he runs. |
| `session/request_permission` | **Before each tool call** — the client approves or rejects. |
| `session/cancel` | Cooperatively stops the turn (denies any pending permission). |

### Client-approved tool calls

This is the heart of it. Before the Leader runs *any* tool — read a file, run a
shell command, fetch a URL, even hand a job to the producer swarm — the server
sends a `session/request_permission` request and **blocks** for your answer:

- **Allow** → the tool runs.
- **Reject** → the Leader gets a `DENIED` result and re-plans around it.

It's fail-closed: a tool never runs without an explicit allow (a timeout or a
cancel counts as deny). The decision is yours; the Leader carries it out — the
same contract as the [conversational approval](/v0-7-2/) inside the TUI.

---

## Scope (v1)

- **Full reply per turn** — the turn returns the Leader's complete reply; live
  token streaming is a later addition.
- **One session per process** — a second `session/prompt` while one is in flight
  is rejected rather than run concurrently.
- **Cooperative cancel** — `session/cancel` stops at the next tool gate / between
  iterations, not mid-model-call.
- **Permission covers the conversational tool-loop** — the tools the Leader calls
  while talking to you. A kickoff he launches runs the producer swarm on its own
  path; those sub-agent tool calls aren't individually gated in v1.
- **stdio only** — no network transport; the editor launches the process locally.

---

## A minimal client (for testing)

The repo ships a tiny reference client at `scripts/smoke/acp/client.py` that
spawns the server, does the handshake, auto-approves tool permissions, and prints
the activity + the Leader's reply:

```bash
python scripts/smoke/acp/client.py --code <PROJECT> "your message to the Leader"
```

There's also a stub transport smoke at `scripts/smoke/acp/smoke.py`.
