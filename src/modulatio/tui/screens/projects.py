# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""PROJECTS config tab — browse, switch, and delete projects (roadmap #7).

A thin presentation layer over pure logic (``vault.list_projects`` /
``app.switch_project`` / ``backup.delete_project``) so a future web UI can
reuse the same functions. Switching is idle-only (the app refuses it under a
running job); delete is guarded three ways against accidental loss — the
active project is never deletable, delete is refused while a job is in flight,
and ``backup.delete_project`` itself backs up first and refuses anything that
isn't a marked project.
"""
from __future__ import annotations

from rich.markup import escape
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, DataTable, Label

from modulatio import backup, config, vault
from modulatio.tui.widgets.confirm_modal import ConfirmModal


class ProjectsScreen(Vertical):
    """PROJECTS tab — list of projects with [Switch] / [Delete]."""

    DEFAULT_CSS = """
    ProjectsScreen {
        padding: 1;
    }
    ProjectsScreen > #projects-controls {
        height: 3;
    }
    ProjectsScreen > #projects-controls > Button {
        margin-right: 2;
    }
    ProjectsScreen DataTable {
        height: 1fr;
        border: round $frame-dim;
    }
    """

    BINDINGS = [
        Binding("s", "switch", "Switch", show=True),
        Binding("x", "delete", "Delete", show=True),
        Binding("f", "refresh", "Refresh", show=True),
    ]

    def compose(self) -> ComposeResult:
        with Horizontal(id="projects-controls"):
            yield Button("Switch to", id="projects-switch", variant="primary")
            yield Button("Delete", id="projects-delete", variant="error")
        yield Label("Switching is disabled while a job is running. Delete backs up first.")
        table = DataTable(id="projects-table", cursor_type="row")
        table.add_columns("Project", "Status")
        yield table

    def on_mount(self) -> None:
        self._refresh()

    def on_show(self) -> None:
        self._refresh()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "projects-switch":
            self.action_switch()
        elif event.button.id == "projects-delete":
            self.action_delete()

    # ── Actions ─────────────────────────────────────────────────────────

    def action_refresh(self) -> None:
        self._refresh()

    def action_switch(self) -> None:
        code = self._selected_code()
        if not code:
            return
        if code == self._current():
            self.notify(f"Already on '{code}'.", severity="information")
            return
        if self.app.switch_project(code):  # type: ignore[attr-defined]
            self._refresh()

    def action_delete(self) -> None:
        code = self._selected_code()
        if not code:
            return
        if code == self._current():
            self.notify("Can't delete the active project — switch away first.", severity="error")
            return
        if self.app._any_job_in_flight():  # type: ignore[attr-defined]
            self.notify("Can't delete a project while a job is running.", severity="warning")
            return
        self.app.push_screen(
            ConfirmModal(
                f"Delete project '{code}'?\n\nIts folder is removed (backed up first).",
            ),
            lambda ok: self._do_delete(code) if ok else None,
        )

    def _do_delete(self, code: str) -> None:
        try:
            path = backup.delete_project(code)
        except (ValueError, OSError) as exc:
            self.notify(f"Delete failed: {exc}", severity="error")
            return
        self.notify(f"Deleted '{code}'. Backup: {path.name}", severity="warning")
        self._refresh()

    # ── Internal ────────────────────────────────────────────────────────

    def _current(self) -> str:
        return getattr(self.app, "project_code", "")

    def _selected_code(self) -> str | None:
        try:
            table = self.query_one("#projects-table", DataTable)
        except Exception:
            return None
        if table.row_count == 0:
            return None
        try:
            cell_key = table.coordinate_to_cell_key(table.cursor_coordinate)
            return cell_key.row_key.value
        except Exception:
            return None

    def _refresh(self) -> None:
        try:
            table = self.query_one("#projects-table", DataTable)
        except Exception:
            return
        table.clear()
        current = self._current()
        default = config.get_default_project_code()
        for code in vault.list_projects():
            if code == current:
                status = "● current"
            elif code == default:
                status = "○ default"
            else:
                status = ""
            table.add_row(escape(code), status, key=code)


__all__ = ["ProjectsScreen"]
