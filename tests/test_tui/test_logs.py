# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""The LOGS tab — list captured logs, send one, delete crash/error/doctor (but
not a run log). Mirrors the TICKETS tab's list+preview rhythm.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from textual.widgets import DataTable, Static, TabbedContent

from modulatio import logstore, vault
from modulatio.tui.app import ModulatioApp
from modulatio.tui.screens.logs import LogsScreen
from modulatio.tui.widgets.confirm_modal import ConfirmModal
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


async def test_delete_confirms_then_removes_error_but_refuses_run_log(tui_logs):
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        app.query_one(TabbedContent).active = "tab-logs"
        await pilot.pause()
        screen = app.query_one(LogsScreen)

        # error log: delete prompts a confirm, and confirming removes it
        err_id = next(e.id for e in logstore.list_logs() if e.kind == "error")
        screen._selected_id = err_id
        screen.action_delete()
        await pilot.pause()
        assert isinstance(app.screen, ConfirmModal)        # L1: confirm first
        await pilot.click("#confirm-yes")
        await pilot.pause()
        assert all(e.kind != "error" for e in logstore.list_logs())

        # a run log is refused outright — no confirm prompt, file survives
        run_id = next(e.id for e in logstore.list_logs() if e.kind == "run")
        screen._selected_id = run_id
        screen.action_delete()
        await pilot.pause()
        assert not isinstance(app.screen, ConfirmModal)
        assert any(e.kind == "run" for e in logstore.list_logs())


async def test_delete_cancel_keeps_the_log(tui_logs):
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        app.query_one(TabbedContent).active = "tab-logs"
        await pilot.pause()
        screen = app.query_one(LogsScreen)
        err_id = next(e.id for e in logstore.list_logs() if e.kind == "error")
        screen._selected_id = err_id
        screen.action_delete()
        await pilot.pause()
        assert isinstance(app.screen, ConfirmModal)
        await pilot.click("#confirm-no")                   # cancel → keep
        await pilot.pause()
        assert any(e.kind == "error" for e in logstore.list_logs())


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


async def test_send_modal_survives_a_submit_exception(tui_logs, monkeypatch):
    """Nemo M2: a raise in submit_issue must surface in the modal (not strand it
    on 'Filing…' and re-raise as WorkerFailed), and re-enable Send."""
    from textual.widgets import Button

    from modulatio import bug_report
    monkeypatch.setattr(
        bug_report, "submit_issue",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("network blew up")),
    )
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
        modal = app.screen
        assert isinstance(modal, SendLogModal)
        modal._submit()
        await app.workers.wait_for_complete()
        await pilot.pause()
        status = modal.query_one("#send-status", Static).render()
        assert "Couldn't file issue" in str(status)              # surfaced, not stranded
        assert modal.query_one("#send-submit", Button).disabled is False  # retry enabled
