#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Deterministic smoke: the overflow cures (no LLM, no network).

1. http_get caps a giant raw-HTML body to ~8k tokens and extracts text.
2. truncate_tool_result bounds a large result to its token budget.

Run from repo root:  .venv/bin/python scripts/smoke/qc-thesis/smoke_overflow_cures.py
"""
from modulatio import tools, tool_summarization as ts

fails = []

# 1) http_get cap + HTML->text on a ~1.2M-char page (the live overflow size).
big_html = "<html><body>" + ("<p>The conflict escalated. </p>" * 40_000) + "</body></html>"
capped = tools._cap_http_body(tools._html_to_text(big_html), over_read=False)
ratio = len(big_html) / max(1, len(capped))
print(f"[http_get] {len(big_html):,} chars -> {len(capped):,} chars ({ratio:.0f}x), marker={'truncated' in capped}")
if len(capped) > tools._HTTP_GET_MAX_CHARS + 200:
    fails.append("http_get did not cap to _HTTP_GET_MAX_CHARS")
if "<p>" in capped or "<html>" in capped:
    fails.append("http_get left raw HTML tags in the body")

# 2) truncate_tool_result bounds a large result to its token budget.
big = "The Israel-Iran conflict escalated sharply. " * 3000  # ~130k chars
out = ts.truncate_tool_result(big, call_id="smoke", max_tokens=500, model="gpt-4o")
toks = ts.count_tokens("gpt-4o", text=out)
print(f"[truncate] {ts.count_tokens('gpt-4o', text=big):,} tok -> {toks} tok (budget 500), pointer={'read_tool_result' in out}")
if toks > 600:
    fails.append(f"truncate_tool_result exceeded budget: {toks} tok")
if "read_tool_result" not in out or "smoke" not in out:
    fails.append("truncate_tool_result missing read_tool_result pointer")

print("RESULT:", "PASS" if not fails else "FAIL " + "; ".join(fails))
raise SystemExit(1 if fails else 0)
