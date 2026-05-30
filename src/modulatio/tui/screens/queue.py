# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Queue tab — Heartbeat persistent task queue (slice 6, Phase 2.5 merge).

Read-only viewer with per-row [Cancel] action for pending tasks. Live
operations (add, clear-done) happen via the CLI ``modulatio heartbeat``
subcommand or programmatically; the tab just shows current state +
lets the user cancel.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, DataTable, Select

from modulatio import heartbeat


_ALL = "__all__"


class QueueScreen(Vertical):
    """Heartbeat queue — pending / running / done / failed / cancelled."""

    DEFAULT_CSS = """
    QueueScreen {
        padding: 1;
    }
    QueueScreen > #queue-controls {
        height: 3;
    }
    QueueScreen > #queue-controls > Select {
        width: 28;
        margin-right: 2;
    }
    QueueScreen DataTable {
        height: 1fr;
        border: solid $panel;
    }
    """

    BINDINGS = [
        Binding("c", "cancel", "Cancel selected", show=True),
        Binding("r", "refresh", "Refresh", show=True),
        Binding("d", "clear_done", "Clear done", show=True),
    ]

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._status_filter: str | None = None  # None = all

    def compose(self) -> ComposeResult:
        with Horizontal(id="queue-controls"):
            yield Select(
                [
                    ("All statuses", _ALL),
                    ("Pending", "pending"),
                    ("Running", "running"),
                    ("Done", "done"),
                    ("Failed", "failed"),
                    ("Cancelled", "cancelled"),
                ],
                id="queue-filter",
                value=_ALL,
                allow_blank=False,
            )
            yield Button("Refresh", id="queue-refresh", variant="primary")
            yield Button("Clear done", id="queue-clear-done")

        table = DataTable(id="queue-table", cursor_type="row")
        table.add_columns("ID", "Status", "Priority", "Project", "Description", "Every", "Created")
        yield table

    def on_mount(self) -> None:
        self._refresh()

    def on_show(self) -> None:
        self._refresh()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "queue-refresh":
            self._refresh()
        elif event.button.id == "queue-clear-done":
            removed = heartbeat.clear_done()
            self.notify(f"Cleared {removed} terminal task(s).", severity="information")
            self._refresh()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id != "queue-filter":
            return
        val = event.value
        self._status_filter = None if val == _ALL else str(val)
        self._refresh()

    # ── Bindings ────────────────────────────────────────────────────────

    def action_refresh(self) -> None:
        self._refresh()

    def action_clear_done(self) -> None:
        removed = heartbeat.clear_done()
        self.notify(f"Cleared {removed} terminal task(s).", severity="information")
        self._refresh()

    def action_cancel(self) -> None:
        try:
            table = self.query_one("#queue-table", DataTable)
        except Exception:
            return
        if table.row_count == 0:
            return
        try:
            cell_key = table.coordinate_to_cell_key(table.cursor_coordinate)
            task_id = cell_key.row_key.value
        except Exception:
            return
        if not task_id:
            return
        if heartbeat.cancel_task(task_id):
            self.notify(f"Cancelled task {task_id}.", severity="warning")
            self._refresh()

    # ── Internal ────────────────────────────────────────────────────────

    def _refresh(self) -> None:
        try:
            table = self.query_one("#queue-table", DataTable)
        except Exception:
            return
        table.clear()
        try:
            tasks = heartbeat.list_tasks(status=self._status_filter)
        except Exception:
            tasks = []
        # Sort: priority asc, created asc — most-urgent at top
        tasks.sort(key=lambda t: (t.get("priority", 99), t.get("created", "")))
        for t in tasks:
            table.add_row(
                t.get("id", "?"),
                t.get("status", "?"),
                str(t.get("priority", "?")),
                t.get("project_code", "?"),
                (t.get("description", "") or "")[:60],
                t.get("every") or "",
                (t.get("created", "") or "")[:19],
                key=t.get("id"),
            )


def build_queue_panel() -> QueueScreen:
    return QueueScreen()


__all__ = ["QueueScreen", "build_queue_panel"]
