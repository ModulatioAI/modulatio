# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""PROJECTS config tab — browse, switch, create, and delete projects (roadmap #7).

Lives under CONFIG (beside MODELS/AGENTS) because switching projects is a form
of workspace configuration. Given the configurator treatment to match its
siblings: a persistent project registry on the left (the doorway) + a swappable
companion on the right — the selected project's detail card, or the create form
swapped in for "new".

A thin presentation layer over pure logic (``vault.list_projects`` /
``app.switch_project`` / ``backup.delete_project`` / ``roster.create_project``)
so a future web UI can reuse the same functions. Switching is idle-only (the app
refuses it under a running job); delete is guarded three ways against accidental
loss — the active project is never deletable, delete is refused while a job is in
flight, and ``backup.delete_project`` itself backs up first and refuses anything
that isn't a marked project.
"""
from __future__ import annotations

from rich.markup import escape
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, DataTable, Input, Static

from modulatio import backup, config, roster, vault
from modulatio.tui.widgets.confirm_modal import ConfirmModal
from modulatio.tui.widgets.configurator import Configurator


class ProjectsScreen(Vertical):
    """PROJECTS tab — persistent registry + a detail/create companion."""

    DEFAULT_CSS = """
    ProjectsScreen { padding: 1; }
    ProjectsScreen #projects-controls { height: 3; }
    ProjectsScreen #projects-controls > Button { margin-right: 2; }
    ProjectsScreen #projects-table { height: 1fr; border: round $frame-dim; }
    ProjectsScreen #proj-detail, ProjectsScreen #newproj-status {
        color: $text-muted; height: auto;
    }
    ProjectsScreen Configurator { height: 1fr; }
    ProjectsScreen #cfg-companion Input { margin: 1 0; }
    """

    BINDINGS = [
        Binding("n", "new", "New", show=True),
        Binding("s", "switch", "Switch", show=True),
        Binding("x", "delete", "Delete", show=True),
        Binding("f", "refresh", "Refresh", show=True),
    ]

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._flow: str | None = None  # None = showing detail; "new" = create form

    def compose(self) -> ComposeResult:
        with Configurator():
            with Vertical(id="cfg-list"):
                with Horizontal(id="projects-controls"):
                    yield Button("New", id="projects-new", variant="primary")
                    yield Button("Switch to", id="projects-switch")
                    yield Button("Delete", id="projects-delete", variant="error")
                table = DataTable(id="projects-table", cursor_type="row")
                table.add_columns("Project", "Status")
                yield table
                yield Static(
                    "↑↓ move · s switch to · n new project · x delete (guarded) "
                    "· f refresh",
                    id="projects-affordance",
                    classes="affordance",
                )
            yield Vertical(Static("", id="proj-detail"), id="cfg-companion")

    def on_mount(self) -> None:
        self._refresh()

    def on_show(self) -> None:
        self._refresh()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "projects-new":
            self.action_new()
        elif bid == "projects-switch":
            self.action_switch()
        elif bid == "projects-delete":
            self.action_delete()
        elif bid == "newproj-create":
            self._submit_new()
        elif bid == "newproj-cancel":
            self.run_worker(self._rest_companion())

    # ── Actions ─────────────────────────────────────────────────────────

    def action_refresh(self) -> None:
        self._refresh()

    def action_new(self) -> None:
        """`n` / New — swap the companion to the create form (the registry
        list stays mounted)."""
        self._flow = "new"
        self.run_worker(self._show_new_form())

    async def _show_new_form(self) -> None:
        await self.query_one(Configurator).swap_companion(
            Static("New project", classes="cfg-title"),
            Input(
                placeholder="folder name — lowercase letters/digits/_ (e.g. my_book)",
                id="newproj-code"),
            Input(placeholder="objective (optional)", id="newproj-objective"),
            Static("", id="newproj-status"),
            Horizontal(
                Button("Create", id="newproj-create", variant="primary"),
                Button("Cancel", id="newproj-cancel"),
                id="newproj-buttons"),
        )
        try:
            self.query_one("#newproj-code", Input).focus()
        except Exception:
            pass

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id in ("newproj-code", "newproj-objective"):
            self._submit_new()

    def _submit_new(self) -> None:
        code = self.query_one("#newproj-code", Input).value.strip().lower()
        objective = self.query_one("#newproj-objective", Input).value.strip()
        if not code:
            self._set_new_status("[bold red]A folder name is required.[/]")
            return
        self._on_new_project((code, objective))

    def _set_new_status(self, text: str) -> None:
        try:
            self.query_one("#newproj-status", Static).update(text)
        except Exception:
            pass

    def _on_new_project(self, result: tuple[str, str] | None) -> None:
        if result is None:
            return
        code, objective = result
        try:
            roster.create_project(code, objective)
        except FileExistsError:
            self._set_new_status(f"[bold red]Project '{escape(code)}' already exists.[/]")
            return
        except ValueError as exc:
            self._set_new_status(f"[bold red]Invalid folder name: {escape(str(exc))}[/]")
            return
        except Exception as exc:  # noqa: BLE001 — surface any failure, never strand the screen
            self.notify(
                f"Couldn't create '{code}': {type(exc).__name__}: {exc}", severity="error")
            return
        self.notify(f"Created project '{code}'.", severity="information")
        self._flow = None
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

    # ── detail companion ────────────────────────────────────────────────

    def on_data_table_row_highlighted(
        self, event: DataTable.RowHighlighted
    ) -> None:
        # Don't clobber the create form while it's up.
        if self._flow == "new":
            return
        if event.row_key is not None and event.row_key.value:
            self._render_detail(event.row_key.value)

    async def _rest_companion(self) -> None:
        self._flow = None
        await self.query_one(Configurator).swap_companion(
            Static("", id="proj-detail"))
        code = self._selected_code()
        if code:
            self._render_detail(code)

    def _render_detail(self, code: str) -> None:
        try:
            runs = len(vault.list_runs(code))
        except Exception:
            runs = 0
        try:
            agents = len(roster.list_agents(code))
        except Exception:
            agents = 0
        current = self._current()
        default = config.get_default_project_code()
        if code == current:
            mark = "● active project"
        elif code == default:
            mark = "○ default project"
        else:
            mark = "○ not active"
        lines = [
            code, mark, "─" * 26,
            f"runs       {runs}",
            f"agents     {agents}",
            "",
        ]
        if code == current:
            lines.append("s switch (already active)  ·  x delete (active — blocked)")
        else:
            lines.append("s switch to  ·  x delete")
        lines += ["n new project", "",
                  "delete backs up first + is refused while a job runs"]
        try:
            self.query_one("#proj-detail", Static).update(escape("\n".join(lines)))
        except Exception:
            pass

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
        if table.row_count > 0 and self._flow != "new":
            first = list(table.rows.keys())[0].value
            if first:
                self._render_detail(first)


__all__ = ["ProjectsScreen"]
