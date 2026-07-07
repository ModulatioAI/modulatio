# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""JOBS tab — the run-folder browser (housekeeping).

A JOB is one run: ``<vault>/<project>/runs/<run_id>/`` — its own output folder
(objective.md + the run's subdirs: goals · tasks · decisions · tickets ·
research · artifacts · reports). This tab lists the runs, shows a folder card
for the selected one, and deletes a whole run you no longer want.

Distinct from ARTIFACTS (files you consume/export) and CRON (schedules): here
the unit is the FOLDER. ``delete`` removes the entire run permanently (run
output is ephemeral — no backup), so it's guarded by a ConfirmModal and refused
while a job is in flight.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from rich.markup import escape
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.widgets import DataTable, Input, Markdown, Static

from modulatio import vault
from modulatio.tui.widgets.confirm_modal import ConfirmModal
from modulatio.tui.widgets.controls_row import ControlsRow
from modulatio.tui.widgets.master_detail import MasterDetail


# Run sizing lives on vault (the neutral engine home) so the JOBS tab and
# the WebOS never show different numbers for the same run.
_run_size = vault.run_size
_human_size = vault.human_size


class JobsScreen(Vertical):
    """JOBS tab content — run-folder list + folder card + delete."""

    BINDINGS = [
        Binding("r", "refresh", "Refresh", show=True),
        Binding("o", "open_folder", "Open folder", show=True),
        Binding("d", "delete", "Delete job", show=True),
    ]

    DEFAULT_CSS = """
    JobsScreen { padding: 1; }
    JobsScreen #jobs-table { height: 1fr; }
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._query: str = ""
        self.detail_source: str = ""

    @property
    def project_code(self) -> str:
        return self.app.project_code  # type: ignore[attr-defined]

    def compose(self) -> ComposeResult:
        with MasterDetail():
            with Vertical(id="md-list"):
                yield ControlsRow(
                    counts=True, search=True, search_placeholder="/ search runs…")
                table = DataTable(id="jobs-table", cursor_type="row")
                table.add_columns("Run", "When", "Size")
                yield table
                yield Static(
                    "↑↓ move · type to search · o open folder · d delete job "
                    "(permanent) · r refresh",
                    id="jobs-affordance",
                    classes="affordance",
                )
            with VerticalScroll(id="md-detail"):
                yield Markdown("_Select a job to see its folder._", id="jobs-detail-md")

    def on_mount(self) -> None:
        self.refresh_jobs()

    def on_show(self) -> None:
        self.refresh_jobs()

    def refresh_jobs(self) -> None:
        try:
            table = self.query_one("#jobs-table", DataTable)
        except Exception:
            return
        table.clear()
        code = self.project_code
        q = self._query.lower()
        shown = 0
        for run_id in reversed(vault.list_runs(code)):  # newest first
            if q and q not in run_id.lower():
                continue
            try:
                path = vault.run_dir(code, run_id)
                size = _human_size(_run_size(path))
            except Exception:
                size = "—"
            table.add_row(
                escape(run_id), escape(_when_of(run_id)), escape(size), key=run_id)
            shown += 1
        self._set_counts(shown)
        if table.row_count > 0:
            first = list(table.rows.keys())[0].value
            if first:
                self._render_detail(first)
        else:
            self._set_detail("_No runs yet._  Kick off a job (CONSOLE tab) to "
                             "create one.")

    def _set_counts(self, shown: int) -> None:
        try:
            label = f"{shown} runs" + (" (filtered)" if self._query else "")
            self.query_one(ControlsRow).set_counts(label)
        except Exception:
            pass

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "controls-search":
            self._query = event.value.strip()
            self.refresh_jobs()

    # ── detail card ─────────────────────────────────────────────────────

    def on_data_table_row_highlighted(
        self, event: DataTable.RowHighlighted
    ) -> None:
        if event.row_key is not None and event.row_key.value:
            self._render_detail(event.row_key.value)

    def _render_detail(self, run_id: str) -> None:
        try:
            path = vault.run_dir(self.project_code, run_id)
        except Exception:
            self._set_detail(f"_Could not resolve run '{escape(run_id)}'._")
            return
        self._set_detail(_format_job_detail(run_id, path))

    def _set_detail(self, source: str) -> None:
        self.detail_source = source
        try:
            self.query_one("#jobs-detail-md", Markdown).update(source)
        except Exception:
            pass

    def _selected_run(self) -> str | None:
        try:
            table = self.query_one("#jobs-table", DataTable)
            if table.row_count == 0:
                return None
            return table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        except Exception:
            return None

    # ── actions ─────────────────────────────────────────────────────────

    def action_refresh(self) -> None:
        self.refresh_jobs()

    def action_open_folder(self) -> None:
        run_id = self._selected_run()
        if not run_id:
            return
        try:
            path = vault.run_dir(self.project_code, run_id)
        except Exception:
            return
        # Best-effort OS reveal; always surface the path so it's reachable even
        # when no opener exists (headless / no DISPLAY).
        try:
            subprocess.Popen(  # noqa: S603,S607 — fixed opener, vault-bounded path
                ["xdg-open", str(path)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
        self.app.notify(f"{path}")

    def action_delete(self) -> None:
        run_id = self._selected_run()
        if not run_id:
            return
        # In-flight guard: never delete run folders while a job is writing them.
        if getattr(self.app, "_any_job_in_flight", lambda: False)():
            self.app.notify(
                "A job is in flight — finish or stop it before deleting runs.",
                severity="warning")
            return
        self.app.push_screen(
            ConfirmModal(
                f"Delete job '{run_id}' — permanently?\n\n"
                "Removes the entire run folder (objective, goals, tasks, "
                "decisions, tickets, research, artifacts, reports). "
                "This cannot be undone.",
                confirm_label="Delete job"),
            lambda ok: self._do_delete(run_id) if ok else None,
        )

    def _do_delete(self, run_id: str) -> None:
        try:
            deleted = vault.delete_run(self.project_code, run_id)
        except Exception as exc:  # noqa: BLE001 — surface, never crash the screen
            self.app.notify(f"Couldn't delete: {exc}", severity="error")
            return
        self.app.notify(
            f"Deleted job '{run_id}'." if deleted else f"Run '{run_id}' not found.")
        self.refresh_jobs()


def _when_of(run_id: str) -> str:
    """Human-ish timestamp from a run_id (``YYYYMMDDTHHMMSSZ-hex``)."""
    head = run_id.split("-", 1)[0]
    if len(head) >= 15 and head[8] == "T":
        return f"{head[:4]}-{head[4:6]}-{head[6:8]} {head[9:11]}:{head[11:13]}"
    return run_id


def _format_job_detail(run_id: str, path: Path) -> str:
    """Markdown folder card for a run: path, objective, subdir contents, size."""
    lines = [f"# {escape(run_id)}", "", f"`{escape(str(path))}`", ""]
    objective = path / "objective.md"
    if objective.exists():
        try:
            body = objective.read_text(encoding="utf-8", errors="replace")
        except OSError:
            body = ""
        # objective.md leads with frontmatter + a "# Objective" heading; show it.
        lines += ["---", "", escape(body.strip()[:600]), "", "---", ""]
    lines.append("## Contents")
    lines.append("")
    for sub in vault.RUN_SUBDIRS:
        d = path / sub
        n = sum(1 for p in d.rglob("*") if p.is_file()) if d.exists() else 0
        lines.append(f"- **{sub}** — {n} file(s)")
    lines += ["", f"**total size:** {_human_size(_run_size(path))}"]
    return "\n".join(lines)


def build_jobs_panel() -> JobsScreen:
    return JobsScreen()


__all__ = ["JobsScreen", "build_jobs_panel"]
