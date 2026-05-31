# Smoke: web-search brick (2026-05-31)

First brick of the skill library — real web search as a separate, composable
capability. Run with the install venv:

```
~/modulatio/.venv/bin/python scripts/smoke/web-search/smoke_web_search_brick.py
```

Verifies offline (no live search): `web_search` is a separate registry tool
beside `http_get`; the `web-search` skill is single-purpose (`[web_search]`,
not bundled); composing it with the `researcher` skill unions to
`{http_get, web_search}`; and `http_get` sends a polite User-Agent. The live
search itself (ddgs/DuckDuckGo) is exercised by the unit tests with a mock and
by the end-to-end `--attach` acceptance run.
