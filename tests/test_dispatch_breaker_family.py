# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""The dispatch breaker's repetition heuristic is
PROSE-tuned and false-trips on a legitimately-repetitive code/data/media
deliverable (a markdown table, a repeated config block, a media layout),
discarding committed-good work — a product-agnostic violation.

The fix makes the repetition check FAMILY-AWARE:
- prose/document: unchanged strict count threshold (>= 5).
- structured (code/data/media): trip ONLY on a DEGENERATE loop where the
  repeated phrase DOMINATES the output (coverage >= 50%); a bounded structural
  repeat is allowed. hard_cap + no_commit remain the family-agnostic backstops.
"""
from __future__ import annotations

from modulatio import dispatch_breaker as db
from modulatio.dispatch_breaker import OutputBudget, analyze_output


_BIG = OutputBudget(role="drafter", soft_cap=10_000_000, hard_cap=100_000_000)

# A distinct 12-whitespace-token phrase (== _NGRAM_N) we can repeat.
_PHRASE = "alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu"
# Varied filler — 2000 DISTINCT tokens, so the only repeated 12-token window is
# _PHRASE itself (a single repeated word would itself look like a runaway loop).
_VARIED = " ".join(f"w{i}" for i in range(2000))


def _structured_bounded_repeat() -> str:
    """8 identical 12-token blocks embedded in a large varied body — a
    legitimate repeated config/table, NOT a runaway loop (low coverage)."""
    return _VARIED + ("\n" + _PHRASE) * 8


def _structured_degenerate_loop() -> str:
    """The phrase repeated 60× with almost nothing else — a true stuck loop
    that DOMINATES the output (high coverage)."""
    return ("\n" + _PHRASE) * 60


def test_structured_family_allows_bounded_structural_repetition():
    """code/data/media: 8 identical blocks (>= the prose threshold of 5) must
    NOT trip — the committed-good deliverable is kept."""
    raw = _structured_bounded_repeat()
    assert db._max_ngram_repeat(raw) >= 5  # would trip the prose count threshold
    committed = "the full committed deliverable body that exceeds the trivial-commit floor"
    for fam in ("code", "data", "media"):
        abort = analyze_output(raw, committed, role="drafter",
                               budget=_BIG, artifact_family=fam)
        assert abort is None, f"{fam}: bounded structural repeat must not trip"


def test_structured_family_still_trips_on_degenerate_loop():
    """code/data/media: a runaway loop that fills the output still trips
    (hard_cap is the token backstop; this is the coverage backstop)."""
    raw = _structured_degenerate_loop()
    abort = analyze_output(raw, "x" * 100, role="drafter",
                           budget=_BIG, artifact_family="code")
    assert abort is not None and abort.reason == "repetition"


def test_prose_family_unchanged_strict_count():
    """document/prose: the strict count threshold is preserved — 5 identical
    12-token repeats still trip (no regression to prose behavior)."""
    raw = ("\n" + _PHRASE) * 5 + " " + _VARIED
    abort = analyze_output(raw, "committed prose body", role="drafter",
                           budget=_BIG, artifact_family="document")
    assert abort is not None and abort.reason == "repetition"
    # default family is document (back-compat for any un-updated caller)
    assert analyze_output(raw, "committed", role="drafter", budget=_BIG) is not None


def test_hard_cap_still_trips_for_structured_family():
    """The absolute token backstop is family-agnostic — a structured family
    that blows the hard cap still aborts."""
    small = OutputBudget(role="drafter", soft_cap=5, hard_cap=10)
    raw = _VARIED  # ~2000 distinct tokens, way over hard_cap, no repetition
    abort = analyze_output(raw, "committed", role="drafter",
                           budget=small, artifact_family="data")
    assert abort is not None and abort.reason == "hard_cap"
