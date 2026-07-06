---
name: research-via-api
description: Discover ranked sources for a topic via the operator's configured research service (the service-API pool). Returns titles, URLs, and content snippets — a discovery step, not a full-page fetch.
executor: llm
capability_tags: research, sourcing, tool-using
required_capabilities: writing
freshness_class: stable
tool_loadout: research_search
---

You can search the web with the `research_search` tool. The operator has
configured an outside research service; the engine checks its API key out of
the pool and injects it — you never see or need the key.

## How to call it

- `query` — plain keywords, the way you'd phrase a search, not a natural-
  language question. Specific beats broad.
- `max_results` — 1 to 12 (default 5). Ask for what the task needs, not the
  ceiling.

The result is a list of ranked sources, each with a title, URL, and a content
snippet — a DISCOVERY step, not a full-page fetch. `http_get` is not in your
loadout: cite directly from the returned snippets, or note in your summary
that a producer with fetch access should pull the full page if the task
needs more than the snippet gives.

## Discipline

- **Metered spend.** Each call is budget-gated; your per-task cap may allow
  only a few. Make every query distinct and purposeful — never repeat a
  failed or empty query verbatim hoping for a different answer. Refine the
  wording instead.
- Treat `DENIED (metered)` as a budget stop to report in your summary, not
  something to retry.
- If the tool reports no research service configured, say so — that's an
  operator setup step, not something you can fix.
