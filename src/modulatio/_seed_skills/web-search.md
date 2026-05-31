---
name: web-search
description: Search the web (DuckDuckGo, no API key) to find out what is TRUE NOW. A separate, single-purpose capability a producer holds whenever a task's answer depends on current truth — current events, live data, anything past a training cutoff, or any fact the producer can't already know — whatever the deliverable is. Never a hard-coded URL. Grants ONLY web_search; pair it with a fetch tool (http_get) to read what it finds.
executor: llm
tool_loadout: [web_search]
capability_tags: web-search, research, currency
freshness_class: stable
---
You hold the web-search capability for this task.

Use **`web_search(query, max_results)`** to DISCOVER sources: search a query and
read the ranked hits (title, URL, snippet). Run several searches with different
phrasings to triangulate — especially for anything time-sensitive, where you
must find what is true NOW, not what your training data remembers. Then, if your
task also grants a fetch tool (`http_get`), read the promising URLs in full to
ground your claims and cite them.

Never assume or hard-code a URL — find it by searching. Your training cutoff is
in the past; for current events, prices, versions, or any fast-moving subject,
searching the live web and reading it is the only way to be right. A claim
written from memory on a current topic is wrong by default.

SOURCE CREDIBILITY — the open web is full of AI-generated content farms and
unvetted wikis that fabricate plausible-looking "facts" (made-up operation
names, events, dates). Results may be marked
`[LOW-CREDIBILITY SOURCE — verify independently before citing]`:
- A low-credibility hit is NOT a citable source on its own. Treat it as a lead
  to chase down in a real source, not as evidence.
- Prefer primary / authoritative reporting: major news outlets, official
  agencies, peer-reviewed work, established reference works.
- For any current-events claim, corroborate it across at least TWO independent
  credible sources before stating it. If you can only find it on a flagged or
  single obscure source, do not assert it — mark it "unverified" instead.
