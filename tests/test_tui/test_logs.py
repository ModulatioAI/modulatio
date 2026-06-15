# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""The LOGS tab — list captured logs, send one, delete crash/error/doctor (but
not a run log). Mirrors the TICKETS tab's list+preview rhythm.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from textual.widgets import DataTable, TabbedContent

from modulatio import logstore, vault
from modulatio.tui.app import ModulatioApp
from modulatio.tui.screens.logs import LogsScreen
from modulatio.tui.widgets.send_log_modal import SendLogModal

PROJECT_CODE = "LOG"


@pytest.fixture
def tui_logs(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    vault.init_project(PROJECT_CODE, "Logs fixture", "obj")
    monkeypatch.setenv("MODULATIO_CRASH_DIR", str(tmp_path / "logs"))
    (tmp_path / "logs").mkdir()
    # one error (deletable) + one run log (not deletable)
    logstore.write_error_log("a producer failure", context={"task": "T-1"})
    (tmp_path / "logs" / "run-20260101T010101_000000Z-1.log").write_text(
        "Modulatio run log\nactivity\n"
    )
    return tmp_path


async def test_logs_tab_lists_captured_logs(tui_logs):
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        app.query_one(TabbedContent).active = "tab-logs"
        await pilot.pause()
        table = app.query_one("#logs-table", DataTable)
        assert table.row_count == 2                      # error + run
        labels = {str(table.get_row_at(i)[0]) for i in range(table.row_count)}
        assert "Error log" in labels and "Run log" in labels


async def test_delete_removes_error_log_but_refuses_run_log(tui_logs):
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        app.query_one(TabbedContent).active = "tab-logs"
        await pilot.pause()
        screen = app.query_one(LogsScreen)

        # delete the error log → gone
        err_id = next(e.id for e in logstore.list_logs() if e.kind == "error")
        screen._selected_id = err_id
        screen.action_delete()
        await pilot.pause()
        assert all(e.kind != "error" for e in logstore.list_logs())

        # a run log is refused — survives
        run_id = next(e.id for e in logstore.list_logs() if e.kind == "run")
        screen._selected_id = run_id
        screen.action_delete()
        await pilot.pause()
        assert any(e.kind == "run" for e in logstore.list_logs())


async def test_send_opens_review_modal(tui_logs):
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        app.query_one(TabbedContent).active = "tab-logs"
        await pilot.pause()
        screen = app.query_one(LogsScreen)
        screen._selected_id = next(
            e.id for e in logstore.list_logs() if e.kind == "error"
        )
        screen.action_send()
        await pilot.pause()
        assert isinstance(app.screen, SendLogModal)
