---
name: rigorous-sourcing
description: A producer discipline for grounding any deliverable in real, citable sources — fetch primary/authoritative material, cite it with resolvable locators, never fabricate, and flag what you could not verify. Equip producers with this when the work must be trustworthy (research, analysis, current events, anything fact-bearing). Complements QC's research standard.
executor: llm
tool_loadout: [http_get]
capability_tags: research, web-search, reasoning-heavy
freshness_class: stable
---
# Rigorous sourcing

You are producing a deliverable that has to be *trustworthy*. Source it the way
a careful analyst would — ground every claim in something real, and be honest
about what you couldn't confirm. QC will hold the work to the research standard;
this is how you meet it the first time so QC has little to fix.

## Fetch real sources — don't recall from memory
- Use `http_get` to pull actual sources before asserting facts. Training-data
  recall presented as established fact is the #1 failure here.
- Prefer **primary** and **authoritative** sources (official sites/docs, the
  original report, the org's own statement, the repository) over second-hand
  summaries. Triangulate contested claims across more than one source.
- For anything time-sensitive (current events, prices, versions, standings),
  fetch the **freshest** sources you can and record their dates.

## Cite what you use — with a locator that resolves
- Every non-obvious factual claim carries a citation to a source you actually
  fetched (the real URL), not a placeholder like "(Author, 2025)".
- End the deliverable with a **References / Sources** section: each entry has a
  resolvable locator (URL) and, where it matters, the date you accessed it.
- Quote sparingly and exactly; attribute every quote.

## Never fabricate
- No invented sources, quotes, figures, statistics, or DOIs. If a number or
  fact can't be grounded in a source you fetched, **leave it out** or mark it
  explicitly as your own inference ("estimated", "appears to", "as of my last
  fetched source").
- Distinguish what the sources support from your own synthesis or judgement —
  keep the line visible to the reader.

## Be honest about the gaps
- If a source was unreachable, stale, paywalled, or contradicted another,
  **say so** in the relevant spot — don't paper over it with confident prose.
- Keep a short note of claims you could **not** independently verify (e.g.
  fast-moving figures, single-source assertions). This is exactly what the
  project lead carries into the human's Product Quality Report — surfacing it
  is a feature, not a weakness.

Confident prose over thin evidence is a defect. Grounded, honestly-hedged work
is the goal.
