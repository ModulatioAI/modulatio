"""Tests for the offline docs accessor (Feng-Tui DOCS tab)."""
from __future__ import annotations

import pytest

from modulatio import docs


def test_list_docs_returns_bundled_pages_in_order():
    pages = docs.list_docs()
    slugs = [s for s, _ in pages]
    # numeric prefixes order the nav: list is in filename order, overview first.
    assert slugs[0] == "01-overview"
    assert slugs == sorted(slugs)


def test_list_docs_titles_come_from_the_heading():
    titles = dict(docs.list_docs())
    assert titles["01-overview"] == "Overview"


def test_read_doc_returns_markdown():
    body = docs.read_doc("01-overview")
    assert body.startswith("# Overview")
    assert "Modulatio" in body


def test_read_doc_unknown_slug_is_empty():
    assert docs.read_doc("no-such-page") == ""


def test_read_doc_rejects_traversal():
    with pytest.raises(ValueError):
        docs.read_doc("../secrets")
    with pytest.raises(ValueError):
        docs.read_doc("a/b")
