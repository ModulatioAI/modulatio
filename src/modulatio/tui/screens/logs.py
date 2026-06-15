# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""LOGS tab — view captured diagnostic logs, send one to GitHub, or delete it.

Layout (mirrors the TICKETS tab):
  ┌─ Logs ─────────────────────────────────────┐
  │ DataTable (left)        │ Preview pane      │
  │  Kind / When / Summary  │ raw (redacted)    │
  │  / Sent?                │ log text          │
  └─────────────────────────────────────────────┘

Keybinds (when this container has focus):
  r — refresh    s — send to GitHub    d — delete

The store (:mod:`modulatio.logstore`) is the single source of truth: crash /
error / doctor logs (the **Run log** kind is view+send only — a project record,
not disposable here). Send routes through :class:`SendLogModal`, which redacts +
lets the user review before anything reaches a public issue.
"""
from __future__ import annotations

from rich.markup import escape
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import DataTable, Static

from modulatio import logstore
from modulatio.tui.widgets.send_log_modal import SendLogModal

#: Shared with `modulatio logs list` so both surfaces format the stamp identically.
_fmt_ts = logstore.format_timestamp


class LogsScreen(Vertical):
    """LOGS tab content — list + preview + send/delete."""

    BINDINGS = [
        Binding("r", "refresh", "Refresh", show=True),
        Binding("s", "send", "Send to GitHub", show=True),
        Binding("d", "delete", "Delete", show=True),
    ]

    DEFAULT_CSS = """
    LogsScreen {
        padding: 1;
    }
    LogsScreen #logs-layout {
        height: 1fr;
    }
    LogsScreen #logs-table {
        width: 55%;
    }
    LogsScreen #log-preview-pane {
        width: 45%;
        border-left: solid $accent;
        padding: 0 1;
    }
    LogsScreen #log-preview {
        height: 1fr;
    }
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._by_id: dict[str, logstore.LogEntry] = {}
        self._selected_id: str | None = None

    def compose(self) -> ComposeResult:
        with Horizontal(id="logs-layout"):
            table = DataTable(id="logs-table", cursor_type="row")
            table.add_columns("Kind", "When", "Summary", "Sent")
            yield table
            with Vertical(id="log-preview-pane"):
                with VerticalScroll(id="log-preview"):
                    yield Static("Select a log to preview.", id="log-preview-text")

    def on_mount(self) -> None:
        self.refresh_logs()

    def on_show(self) -> None:
        """Reload when the tab becomes visible — picks up logs captured since."""
        self.refresh_logs()

    def refresh_logs(self) -> None:
        table = self.query_one("#logs-table", DataTable)
        table.clear()
        self._by_id = {}
        for e in logstore.list_logs():
            self._by_id[e.id] = e
            table.add_row(
                e.label, _fmt_ts(e.timestamp), e.summary[:60],
                "sent" if e.sent else "", key=e.id,
            )
        if table.row_count > 0:
            first = list(table.rows.keys())[0].value
            if first:
                self._selected_id = first
                self._render_preview(first)
        else:
            self._selected_id = None
            self._set_preview("No logs captured.")

    # ── Bindings ────────────────────────────────────────────────────────

    def action_refresh(self) -> None:
        self.refresh_logs()

    def action_send(self) -> None:
        entry = self._by_id.get(self._selected_id or "")
        if entry is None:
            return
        self.app.push_screen(SendLogModal(entry), self._after_send)

    def _after_send(self, filed: bool | None) -> None:
        if filed:
            self.refresh_logs()

    def action_delete(self) -> None:
        entry = self._by_id.get(self._selected_id or "")
        if entry is None:
            return
        if logstore.delete_log(entry):
            self.refresh_logs()
            self.app.notify(f"Deleted {entry.label}.")
        else:
            self.app.notify(
                f"A {entry.label} can't be deleted here.", severity="warning"
            )

    # ── Preview pane ────────────────────────────────────────────────────

    def on_data_table_row_highlighted(
        self, event: DataTable.RowHighlighted
    ) -> None:
        if event.row_key is None or not event.row_key.value:
            return
        self._selected_id = event.row_key.value
        self._render_preview(self._selected_id)

    def _render_preview(self, log_id: str) -> None:
        entry = self._by_id.get(log_id)
        if entry is None:
            return
        try:
            text = entry.path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            text = f"(could not read log: {exc})"
        self._set_preview(text)

    def _set_preview(self, text: str) -> None:
        try:
            # Escape so a log's own ``[...]`` can't be read as console markup.
            self.query_one("#log-preview-text", Static).update(escape(text))
        except Exception:
            # Preview not yet mounted (refresh called pre-mount) — next pass renders.
            pass


def build_logs_panel() -> LogsScreen:
    return LogsScreen()


__all__ = ["LogsScreen", "build_logs_panel"]
