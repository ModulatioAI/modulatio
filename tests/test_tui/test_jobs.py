"""Tests for the JOBS tab (run-folder browser, Feng-Tui overhaul, Group D)."""
from __future__ import annotations

from pathlib import Path

import pytest
from textual.widgets import DataTable, Static, TabbedContent

from modulatio import vault
from modulatio.tui.app import ModulatioApp
from modulatio.tui.screens.jobs import JobsScreen

PROJECT_CODE = "JOB"


@pytest.fixture
def project_with_runs(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    vault.init_project(PROJECT_CODE, "Jobs fixture", "obj")
    vault.init_run(PROJECT_CODE, "20260101T010101Z-aaa111", "write the essay")
    vault.init_run(PROJECT_CODE, "20260102T020202Z-bbb222", "build the report")
    return tmp_path


async def test_jobs_tab_lists_runs_newest_first(project_with_runs):
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        app.query_one(TabbedContent).active = "tab-jobs"
        await pilot.pause()
        table = app.query_one("#jobs-table", DataTable)
        assert table.row_count == 2
        # newest run id sorts last lexicographically → shown first
        assert str(table.get_row_at(0)[0]) == "20260102T020202Z-bbb222"


async def test_jobs_detail_card_shows_objective_and_contents(project_with_runs):
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        app.query_one(TabbedContent).active = "tab-jobs"
        await pilot.pause()
        screen = app.query_one(JobsScreen)
        screen._render_detail("20260101T010101Z-aaa111")
        await pilot.pause()
        assert "write the essay" in screen.detail_source
        assert "Contents" in screen.detail_source


async def test_jobs_search_filters_runs(project_with_runs):
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        app.query_one(TabbedContent).active = "tab-jobs"
        await pilot.pause()
        screen = app.query_one(JobsScreen)
        screen._query = "bbb222"
        screen.refresh_jobs()
        await pilot.pause()
        table = app.query_one("#jobs-table", DataTable)
        assert table.row_count == 1
        counts = str(screen.query_one("#controls-counts", Static).render())
        assert "filtered" in counts


async def test_jobs_delete_removes_the_run(project_with_runs):
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        app.query_one(TabbedContent).active = "tab-jobs"
        await pilot.pause()
        screen = app.query_one(JobsScreen)
        screen._do_delete("20260101T010101Z-aaa111")
        await pilot.pause()
        assert "20260101T010101Z-aaa111" not in vault.list_runs(PROJECT_CODE)
        assert app.query_one("#jobs-table", DataTable).row_count == 1


async def test_jobs_delete_refused_while_a_job_is_in_flight(project_with_runs, monkeypatch):
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        app.query_one(TabbedContent).active = "tab-jobs"
        await pilot.pause()
        screen = app.query_one(JobsScreen)
        monkeypatch.setattr(app, "_any_job_in_flight", lambda: True)
        app.query_one("#jobs-table", DataTable).move_cursor(row=0)
        stack_before = len(app.screen_stack)
        screen.action_delete()
        await pilot.pause()
        # no confirm modal pushed, and both runs survive
        assert len(app.screen_stack) == stack_before
        assert len(vault.list_runs(PROJECT_CODE)) == 2
