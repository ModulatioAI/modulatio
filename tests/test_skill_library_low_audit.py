"""LOW-audit regression tests for :mod:`modulatio.skill_library`.

Finding #82: ``search_skills`` returned 1 result when ``limit <= 0`` instead
of 0, because the slice used ``max(1, limit)``. A caller asking for zero (or
a negative number of) results should get an empty list.
"""

from modulatio import skill_library


def test_search_limit_zero_returns_nothing() -> None:
    # A query that DOES match real skills, so the only thing keeping the
    # result empty is the limit guard (not a no-match).
    assert skill_library.search_skills("web search", limit=0) == []


def test_search_negative_limit_returns_nothing() -> None:
    # Negative limit must not fall through to Python's from-the-end slicing.
    assert skill_library.search_skills("web search", limit=-3) == []


def test_search_positive_limit_still_works() -> None:
    hits = skill_library.search_skills("web search", limit=2)
    assert 0 < len(hits) <= 2
