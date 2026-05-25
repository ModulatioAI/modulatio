"""Tests for the researcher web-grounding hint slice.

Closes the stale-data-fabrication gap surfaced 2026-04-27 in
crypto_advisers, where the drafter cited pre-cutoff BTC prices as if
they were current. Root cause: the Researcher slot was on a
non-web-grounded model (glm-5.1) without disclaiming its lack of
web access — fabricated 'recent' market data from training cutoff.

The V2 researcher prompt source is the wizard template at
``src/modulatio/templates/researcher.md``; it must:
  - Explicitly disclaim web access.
  - Direct the model to flag INSUFFICIENT_FRESHNESS rather than
    fabricate when a task needs current/live data.

(The legacy ``modulatio.agents.RESEARCHER_BACKSTORY`` source was
removed alongside the V1 orphan ``agents.py`` during the V2 pre-tag
sweep. The corresponding test was dropped — V2 has only the template
path; the same invariant is verified below.)
"""
from __future__ import annotations

from pathlib import Path


def test_researcher_template_disclaims_web_access():
    """The wizard-creates-Researcher template body carries the same
    disclaimer so newly-seeded Researcher agents start with the
    guardrail in their identity."""
    template = Path(__file__).resolve().parent.parent / "src" / "modulatio" / "templates" / "researcher.md"
    body = template.read_text(encoding="utf-8").lower()
    assert "no web access" in body or "no web search" in body or "training cutoff" in body or "training data" in body
    assert "insufficient_freshness" in body or "insufficient freshness" in body


def test_researcher_template_keeps_existing_capability_tag():
    """The template's frontmatter already declares ``web-search`` as a
    capability tag — that signals what the Researcher SHOULD do, not
    what its model can. The disclaimer adds to the body, the tag
    stays so Coordinator dispatch can still match research tasks."""
    template = Path(__file__).resolve().parent.parent / "src" / "modulatio" / "templates" / "researcher.md"
    body = template.read_text(encoding="utf-8")
    # Frontmatter line.
    assert "default_capability_tags: [research, web-search, structured-output]" in body
