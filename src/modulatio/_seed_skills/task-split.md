---
name: task-split
description: Size ONE planned gather task against its prudent context cap — return it whole if it fits, else the fewest self-contained chunks that each fit. Invoked as a utility LLM call from the plan post-process.
executor: llm
capability_tags: task-breakdown, structured-output
freshness_class: stable
---
You size ONE planned gather task against its context budget. Do not plan new
work — judge only whether THIS scope fits, and cut it if it cannot.

TASK SCOPE:
{scope}

The producer running this task has a ~{window}-token context window, but its
projected WORKING context (instructions + gathered sources + draft) should stay
under ~{cap} tokens so it has room to gather AND draft.

- If the full scope's working context fits under ~{cap} tokens, it stays whole.
- Otherwise split it into the FEWEST self-contained chunks that EACH fit under
  ~{cap} tokens. Cut along the scope's natural lines (mechanisms, dimensions,
  sections, subtopics). YAGNI: do NOT invent extra pieces — if 5 chunks each
  fit, make 5, never 10. Each chunk gets a one-sentence description that stands
  alone; do NOT assign file paths.

Respond with ONLY a JSON object, no prose:
{{"fits": true, "estimated_tokens": <your size estimate for the whole scope>}}
or
{{"fits": false, "estimated_tokens": <estimate>, "chunks": ["<chunk description>", ...]}}
