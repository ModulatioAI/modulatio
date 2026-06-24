"""Tests for the DOCS tab (offline doc reader, Feng-Tui overhaul, Group D)."""
from __future__ import annotations

import pytest
from textual.widgets import DataTable, Static, TabbedContent

from modulatio.tui.app import ModulatioApp
from modulatio.tui.screens.docs import DocsScreen

PROJECT_CODE = "DOC"


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    from modulatio import vault
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    vault.init_project(PROJECT_CODE, "docs", "obj")
    yield


async def test_docs_tab_lists_bundled_pages():
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        app.query_one(TabbedContent).active = "tab-docs"
        await pilot.pause()
        nav = app.query_one("#docs-nav", DataTable)
        assert nav.row_count >= 5  # the bundled starter set
        titles = " ".join(str(nav.get_row_at(i)[0]) for i in range(nav.row_count))
        assert "Overview" in titles


async def test_docs_uses_a_wide_reading_pane():
    """The reading pane is the doorway → MasterDetail -wide-detail (60%)."""
    from modulatio.tui.widgets.master_detail import MasterDetail

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        app.query_one(TabbedContent).active = "tab-docs"
        await pilot.pause()
        md = app.query_one(DocsScreen).query_one(MasterDetail)
        assert md.has_class("-wide-detail")


async def test_selecting_a_page_renders_it():
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        app.query_one(TabbedContent).active = "tab-docs"
        await pilot.pause()
        screen = app.query_one(DocsScreen)
        screen._render_page("01-overview")
        await pilot.pause()
        # the Markdown widget rendered the page (no crash); the source came back
        from modulatio import docs
        assert docs.read_doc("01-overview").startswith("# Overview")


async def test_docs_search_filters_pages():
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        app.query_one(TabbedContent).active = "tab-docs"
        await pilot.pause()
        screen = app.query_one(DocsScreen)
        screen._query = "memory"
        screen.refresh_docs()
        await pilot.pause()
        nav = app.query_one("#docs-nav", DataTable)
        assert nav.row_count == 1  # only the memory/jobs/cron page matches
        counts = str(screen.query_one("#controls-counts", Static).render())
        assert "filtered" in counts
