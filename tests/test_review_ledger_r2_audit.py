# SPDX-License-Identifier: Apache-2.0
"""r2 audit regressions for review_ledger._norm_unit.

Finding: ``_norm_unit`` used ``lstrip("./")`` — a CHARACTER-SET strip that
mangles leading-dot filenames (``.config`` -> ``config``), so a dotfile unit
would never set-equal its own dependency record and a clean assembly would
silently fall back to a full review (or worse, mis-compare).
"""
from modulatio.review_ledger import _norm_unit


def test_norm_unit_preserves_leading_dot_filename():
    # Before the fix lstrip("./") stripped the leading dot -> "config".
    assert _norm_unit(".config") == ".config"
    assert _norm_unit("..hidden") == "..hidden"


def test_norm_unit_preserves_dotfile_in_subdir():
    assert _norm_unit("sub/.env") == "sub/.env"
    assert _norm_unit("./sub/.gitignore") == "sub/.gitignore"


def test_norm_unit_strips_single_dot_slash_prefix():
    assert _norm_unit("./chapter1.md") == "chapter1.md"
    assert _norm_unit("chapter1.md") == "chapter1.md"


def test_norm_unit_strips_leading_slashes_but_not_dots():
    assert _norm_unit("/abs/.dotfile") == "abs/.dotfile"
    assert _norm_unit("  ./x.md  ") == "x.md"


def test_norm_unit_dotfile_set_equality_roundtrip():
    # The set-comparison use-site: a dep path and the manifest entry for the
    # SAME dotfile must normalize equal.
    dep = "./out/.report"
    manifest_entry = "out/.report"
    assert _norm_unit(dep) == _norm_unit(manifest_entry) == "out/.report"
