"""LOW-audit regression tests for src/modulatio/assembly.py.

Finding #47 [resource-leak]: ``_merge_csv`` called ``csv.field_size_limit()``,
which mutates process-wide CSV parser state, and never restored it — leaking the
merge ceiling onto all subsequent CSV parsing in the process.
"""

import csv

from modulatio.assembly import _MAX_UNIT_BYTES, _merge_csv


def test_merge_csv_restores_global_field_size_limit() -> None:
    """After a merge, the process-wide CSV field-size limit is unchanged."""
    before = csv.field_size_limit()
    content, errors = _merge_csv([("a", "h1,h2\n1,2\n"), ("b", "h1,h2\n3,4\n")], dedupe=False)
    after = csv.field_size_limit()
    # The merge produced output (sanity) and left the global state untouched.
    assert "h1,h2" in content
    assert after == before
    # And it was NOT left pinned at the merge ceiling.
    assert after != _MAX_UNIT_BYTES or before == _MAX_UNIT_BYTES


def test_merge_csv_restores_limit_even_when_no_header() -> None:
    """The early-return (no header) path also restores the prior limit."""
    before = csv.field_size_limit()
    content, errors = _merge_csv([], dedupe=False)
    assert content == ""
    assert csv.field_size_limit() == before


def test_merge_csv_restores_limit_with_known_prior_value() -> None:
    """Set a distinct prior limit; confirm it survives the merge exactly."""
    original = csv.field_size_limit()
    try:
        sentinel = 12345
        csv.field_size_limit(sentinel)
        _merge_csv([("a", "h\n1\n")], dedupe=True)
        assert csv.field_size_limit() == sentinel
    finally:
        csv.field_size_limit(original)
