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
        # Feng-Tui: the KIND cell is now "<glyph> <label>" (glyph+WORD).
        labels = " ".join(str(table.get_row_at(i)[0]) for i in range(table.row_count))
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


async def test_send_modal_headless_copies_link_and_stays_exitable(tui_logs, monkeypatch):
    """Headless / no browser: Report on GitHub copies the issue link, spells out
    the email + exit in the status, and the modal stays dismissable (the old
    'stranded worker' failure mode is gone — no worker, no network)."""
    from modulatio import bug_report, clipboard
    monkeypatch.setattr(
        bug_report, "open_issue",
        lambda t, b: (False, "https://github.com/ModulatioAI/modulatio/issues/new?title=t"),
    )
    copied: dict = {}
    monkeypatch.setattr(
        clipboard, "copy", lambda text: copied.setdefault("text", text) is None or True
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
        modal._report_on_github()
        await pilot.pause()
        status = str(modal.query_one("#send-status", Static).render())
        assert "copied" in status.lower()                       # link surfaced
        assert bug_report.CONTACT_EMAIL in status               # email fallback
        assert "github.com" in copied.get("text", "")           # the link was copied
        modal.dismiss(False)  # still exitable; never stranded
        await pilot.pause()


# ─── Controls row + affordance (Feng-Tui overhaul) ──────────────────────────


async def test_logs_has_controls_row_with_counts_and_search(tui_logs):
    """The list yields a ControlsRow (counts + search) atop the table, and the
    counts cell reports the visible log total."""
    from modulatio.tui.widgets.controls_row import ControlsRow

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        app.query_one(TabbedContent).active = "tab-logs"
        await pilot.pause()
        # Scope to the LOGS screen — TICKETS also carries a ControlsRow.
        row = app.query_one(LogsScreen).query_one(ControlsRow)
        assert row.query("#controls-counts")
        assert row.query("#controls-search")
        counts = str(row.query_one("#controls-counts", Static).render())
        assert "2 logs" in counts


async def test_logs_search_filters_rows_and_marks_filtered(tui_logs):
    """Typing a query filters the table to matching rows and flags the counts
    as filtered."""
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        app.query_one(TabbedContent).active = "tab-logs"
        await pilot.pause()
        screen = app.query_one(LogsScreen)
        screen._query = "producer"  # matches only the error log's summary
        screen.refresh_logs()
        await pilot.pause()
        assert screen.query_one("#logs-table", DataTable).row_count == 1
        counts = str(screen.query_one("#controls-counts", Static).render())
        assert "filtered" in counts


async def test_logs_affordance_names_send_and_delete(tui_logs):
    """The detail affordance names the send + delete keys (s · d)."""
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        app.query_one(TabbedContent).active = "tab-logs"
        await pilot.pause()
        text = str(app.query_one("#logs-affordance", Static).render())
        assert "s " in text and "send" in text.lower()
        assert "d " in text and "delete" in text.lower()
