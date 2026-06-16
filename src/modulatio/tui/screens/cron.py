# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Cron tab — scheduled-job management (slice 7, Phase 2.5 merge).

Read-only viewer + per-row [Enable]/[Disable]/[Run now]/[Remove] actions.
Adding new jobs goes through the CLI (``modulatio cron add ...``) so the
interactive form complexity stays out of the TUI for now.
"""

from __future__ import annotations

from rich.markup import escape

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, DataTable, Label, Select

from modulatio import cron
from modulatio.tui.widgets.confirm_modal import ConfirmModal


_ALL = "__all__"

#: Glyph per last-run status — paired with the WORD (Feng-Tui §10).
_STATUS_GLYPH = {"ok": "✓", "failed": "✖", "running": "▸", "skipped": "·"}


class CronScreen(Vertical):
    """Cron tab — list + enable/disable/run-now/remove."""

    DEFAULT_CSS = """
    CronScreen {
        padding: 1;
    }
    CronScreen > #cron-controls {
        height: 3;
    }
    CronScreen > #cron-controls > Select {
        width: 28;
        margin-right: 2;
    }
    CronScreen DataTable {
        height: 1fr;
        border: round $frame-dim;   /* theme-aware (was solid $panel) */
    }
    """

    BINDINGS = [
        Binding("e", "enable", "Enable", show=True),
        Binding("d", "disable", "Disable", show=True),
        Binding("r", "run_now", "Run now", show=True),
        Binding("x", "remove", "Remove", show=True),
        Binding("f", "refresh", "Refresh", show=True),
    ]

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._project_filter: str | None = None

    def compose(self) -> ComposeResult:
        with Horizontal(id="cron-controls"):
            yield Select(
                [("All projects", _ALL)],
                id="cron-project-filter",
                value=_ALL,
                allow_blank=False,
            )
            yield Button("Refresh", id="cron-refresh", variant="primary")

        yield Label(
            "Add jobs via CLI: `modulatio cron add --name X --schedule \"daily 09:00\" "
            "--code STA --objective '...'`"
        )
        table = DataTable(id="cron-table", cursor_type="row")
        table.add_columns("ID", "Name", "Project", "Schedule", "Enabled", "Next run", "Last status")
        yield table

    def focus_project(self, project_code: str) -> None:
        """Programmatic project filter from the ``/cron <code>`` command
        (sibling of ``MemoryScreen.focus_agent``). Switches the table to that
        project's jobs; a blank or unknown code falls back to All projects so
        the Select widget always stays on a valid option."""
        code = (project_code or "").strip().upper()
        self._populate_project_filter()
        known = {j.get("project_code", "?") for j in cron.list_jobs()}
        if code and code in known:
            self._project_filter = code
        else:
            self._project_filter = None
            code = _ALL
        # Best-effort: reflect the filter in the Select if it's mounted.
        try:
            sel = self.query_one("#cron-project-filter", Select)
            sel.value = code
        except Exception:
            pass
        self._refresh()

    def on_mount(self) -> None:
        self._populate_project_filter()
        self._refresh()

    def on_show(self) -> None:
        self._populate_project_filter()
        self._refresh()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cron-refresh":
            self._populate_project_filter()
            self._refresh()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id != "cron-project-filter":
            return
        val = event.value
        self._project_filter = None if val == _ALL else str(val)
        self._refresh()

    # ── Bindings ────────────────────────────────────────────────────────

    def action_refresh(self) -> None:
        self._populate_project_filter()
        self._refresh()

    def action_enable(self) -> None:
        jid = self._selected_id()
        if jid and cron.enable(jid):
            self.notify(f"Enabled {jid}.", severity="information")
            self._refresh()

    def action_disable(self) -> None:
        jid = self._selected_id()
        if jid and cron.disable(jid):
            self.notify(f"Disabled {jid}.", severity="warning")
            self._refresh()

    def action_run_now(self) -> None:
        jid = self._selected_id()
        if jid and cron.run_now(jid):
            self.notify(f"Queued manual run for {jid} (heartbeat will dispatch).", severity="information")
            self._refresh()

    def action_remove(self) -> None:
        jid = self._selected_id()
        if not jid:
            return
        # Removing a cron job deletes the schedule — guard it (consistent with
        # the other destructive deletes, cadre 2026-06-16).
        self.app.push_screen(
            ConfirmModal(f"Remove cron job '{jid}'?\n\nThe schedule is deleted."),
            lambda ok: self._do_remove(jid) if ok else None,
        )

    def _do_remove(self, jid: str) -> None:
        if cron.remove(jid):
            self.notify(f"Removed {jid}.", severity="warning")
            self._refresh()

    # ── Internal ────────────────────────────────────────────────────────

    def _selected_id(self) -> str | None:
        try:
            table = self.query_one("#cron-table", DataTable)
        except Exception:
            return None
        if table.row_count == 0:
            return None
        try:
            cell_key = table.coordinate_to_cell_key(table.cursor_coordinate)
            return cell_key.row_key.value
        except Exception:
            return None

    def _populate_project_filter(self) -> None:
        try:
            sel = self.query_one("#cron-project-filter", Select)
        except Exception:
            return
        codes = sorted({j.get("project_code", "?") for j in cron.list_jobs()})
        options = [("All projects", _ALL)] + [(c, c) for c in codes]
        sel.set_options(options)
        if self._project_filter and self._project_filter not in codes:
            self._project_filter = None
            sel.value = _ALL

    def _refresh(self) -> None:
        try:
            table = self.query_one("#cron-table", DataTable)
        except Exception:
            return
        table.clear()
        try:
            jobs = cron.list_jobs(project_code=self._project_filter)
        except Exception:
            jobs = []
        # Sort: enabled first, then by next_run asc
        jobs.sort(key=lambda j: (not j.get("enabled"), j.get("next_run", "")))
        for j in jobs:
            status = j.get("last_status") or ""
            last = f"{_STATUS_GLYPH.get(status, '·')} {status}" if status else ""
            table.add_row(
                escape(j.get("id", "?")),
                escape(j.get("name", "?")),
                escape(j.get("project_code", "?")),
                escape(j.get("schedule", "?")),
                "● on" if j.get("enabled") else "○ off",   # glyph + WORD (§10)
                escape((j.get("next_run", "") or "")[:19]),
                escape(last),
                key=j.get("id"),
            )


def build_cron_panel() -> CronScreen:
    return CronScreen()


__all__ = ["CronScreen", "build_cron_panel"]
