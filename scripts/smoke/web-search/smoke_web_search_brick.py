#!/usr/bin/env python3
"""First brick of the skill library: web search as a SEPARATE tool + a
single-purpose skill, composed onto a producer per task (no roles, no
bundling). Offline/deterministic — verifies the wiring, not a live search.

Run:  ~/modulatio/.venv/bin/python smoke_web_search_brick.py
"""
import modulatio.tools as t
import modulatio.skills as sk

# 1) web_search is a SEPARATE registry tool, beside http_get
reg = t.build_registry()
assert "web_search" in reg and "http_get" in reg, "web_search/http_get must be separate tools"

# 2) the web-search skill is SINGLE-PURPOSE (grants only web_search — not bundled)
ws = sk.load_with_metadata("web-search")
assert ws.tool_loadout == ("web_search",), ws.tool_loadout

# 3) the researcher skill is unbundled (its own single tool)
rs = sk.load_with_metadata("researcher")
assert rs.tool_loadout == ("http_get",), rs.tool_loadout

# 4) composing them = the UNION (http_get + web_search), neither skill holding both
loadout, seen = list(rs.tool_loadout), set(rs.tool_loadout)
for tool in ws.tool_loadout:
    if tool not in seen:
        seen.add(tool); loadout.append(tool)
assert set(loadout) == {"http_get", "web_search"}, loadout

# 5) http_get sends a polite identifying User-Agent (was 403'ing Wikipedia)
assert "Modulatio" in t._HTTP_USER_AGENT

print("PASS: web_search + http_get are separate tools; web-search skill is "
      "single-purpose; composing researcher+web-search unions to "
      "{http_get, web_search}; http_get has a polite UA.")
