---
name: web-search
description: Search the web (DuckDuckGo, no API key) to DISCOVER current sources. A separate, single-purpose capability a producer holds when a task needs to find current/external information — never a hard-coded URL. Grants ONLY web_search; pair it with a fetch tool (http_get) to read what it finds. Grant it on any task whose answer depends on what is true NOW.
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
