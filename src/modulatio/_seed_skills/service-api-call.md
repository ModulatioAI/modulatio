---
name: service-api-call
description: Call any operator-configured outside service's API through the generic api_call tool (relative to the service's pinned base URL). For services without a purpose-built tool.
executor: llm
capability_tags: api-integration, tool-using
required_capabilities: writing
freshness_class: stable
tool_loadout: api_call
---

`api_call` reaches any service the operator configured in the SERVICES pool.
Auth is injected by the engine — you never handle keys.

## How to call it

- `service` — the configured service id (an unknown id returns the list of
  configured ones).
- `path` — RELATIVE to the service's pinned base URL (`v1/things`). Absolute
  URLs are refused by design; you cannot choose the host.
- `method`, `params` (query dict), `json` (body dict) as the API requires.

## Discipline

- Look for a skill named for the service first (the Leader may have authored
  one documenting its endpoints) — `search_skills` before guessing shapes.
- Metered spend: budget-gated per call. Plan the call, make it count, treat
  `DENIED (metered)` as a budget stop to report, not retry.
- An HTTP 4xx/5xx comes back as the tool result — read the body, fix your
  request shape, or report the service-side failure.
