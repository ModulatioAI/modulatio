#!/usr/bin/env python3
"""Source-credibility discipline: web_search FLAGS known content-farm slop and
sinks it below credible hits — flag, never drop (the producer + audit see
everything). Offline/deterministic (ddgs mocked).

Run:  ~/modulatio/.venv/bin/python smoke_credibility.py
"""
import unittest.mock as m
import modulatio.tools as t


class FakeDDGS:
    def text(self, q, max_results):
        return [
            {"title": "Slop", "href": "https://grokipedia.com/x", "body": "fabricated"},
            {"title": "Real", "href": "https://www.reuters.com/y", "body": "reported"},
        ]


assert t._is_low_credibility("https://grokipedia.com/p")
assert t._is_low_credibility("https://www.kennelbiscotti.com/a")
assert not t._is_low_credibility("https://www.aljazeera.com/n")

with m.patch("ddgs.DDGS", FakeDDGS):
    out = t.web_search("israel iran 2026", max_results=2)

assert out.index("Real") < out.index("Slop"), "credible must re-rank first"
assert "LOW-CREDIBILITY" in out, "slop must be flagged"
assert "grokipedia.com/x" in out, "slop must NOT be dropped (flag, not censor)"
print("PASS: content-farm slop flagged + sunk below credible hits; nothing dropped.")
