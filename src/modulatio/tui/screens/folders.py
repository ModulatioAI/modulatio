# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""FOLDERS config tab — named operator folders for job runs.

Register folder locations (local dirs, mapped drives, already-mounted
smb/cifs/nfs shares) the team can use during a run: **read-only** (items to
process), **output** (the finished product may be delivered here — pick one
as the job-output destination), or **read-write** (files may be created or
modified live mid-run). Kickoff directions reference folders BY NAME
("process the document files in folder contracts one at a time").

The tab IS the permission decision: a registered folder reaches every seat
(Leader, QC, producers) with its configured mode and never fires a runtime
permission prompt. Validation here is defense-in-depth with
``config.folder_grant_roots`` (the USE-time re-check): refuse at ADD what
the classifier would drop at grant time, plus registry-shape rules
(duplicate names/paths). A thin presentation layer over ``config``
accessors so a future web UI reuses the same functions.
"""
from __future__ import annotations

from pathlib import Path

from rich.markup import escape
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, DataTable, Input, Select, Static

from modulatio import config
from modulatio.tui.widgets.confirm_modal import ConfirmModal
from modulatio.tui.widgets.configurator import Configurator
from modulatio.tui.widgets.file_picker import FolderPickerModal

#: mode value → the word the operator sees (the registry stores the value).
_MODE_CHOICES = [
    ("read-only — items for the team to read", "ro"),
    ("output — finished product may be delivered here", "output"),
    ("read-write — team may create/modify files here", "rw"),
]
_MODE_WORDS = {"ro": "read-only", "output": "output", "rw": "read-write"}


class FoldersScreen(Vertical):
    """FOLDERS tab — persistent registry + a detail/add companion."""

    DEFAULT_CSS = """
    FoldersScreen { padding: 1; }
    FoldersScreen #folders-controls { height: 3; }
    FoldersScreen #folders-controls > Button { margin-right: 2; }
    FoldersScreen #folders-table { height: 1fr; border: round $frame-dim; }
    FoldersScreen #folder-detail, FoldersScreen #newfolder-status {
        color: $text-muted; height: auto;
    }
    FoldersScreen Configurator { height: 1fr; }
    FoldersScreen #cfg-companion Input { margin: 1 0; }
    FoldersScreen #cfg-companion Select { margin: 1 0; }
    """

    BINDINGS = [
        Binding("n", "new", "New", show=True),
        Binding("o", "set_output", "Output", show=True),
        Binding("x", "delete", "Delete", show=True),
        Binding("f", "refresh", "Refresh", show=True),
    ]

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._flow: str | None = None  # None = detail; "new" = add form

    def compose(self) -> ComposeResult:
        with Configurator():
            with Vertical(id="cfg-list"):
                with Horizontal(id="folders-controls"):
                    yield Button("New", id="folders-new", variant="primary")
                    yield Button("Set output", id="folders-output")
                    yield Button("Delete", id="folders-delete", variant="error")
                table = DataTable(id="folders-table", cursor_type="row")
                col_keys = table.add_columns(
                    "Name", "Mode", "Out", "Status", "Path")
                self._status_col = col_keys[3]
                yield table
                yield Static(
                    "↑↓ move · n new folder · o pick as job output · x delete "
                    "· f refresh",
                    id="folders-affordance",
                    classes="affordance",
                )
            yield Vertical(Static("", id="folder-detail"), id="cfg-companion")

    def on_mount(self) -> None:
        self._refresh()

    def on_show(self) -> None:
        self._refresh()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "folders-new":
            self.action_new()
        elif bid == "folders-output":
            self.action_set_output()
        elif bid == "folders-delete":
            self.action_delete()
        elif bid == "newfolder-browse":
            self._browse()
        elif bid == "newfolder-create":
            self._submit_new()
        elif bid == "newfolder-cancel":
            self.run_worker(self._rest_companion())

    # ── Actions ─────────────────────────────────────────────────────────

    def action_refresh(self) -> None:
        self._refresh()

    def action_new(self) -> None:
        self._flow = "new"
        self.run_worker(self._show_new_form())

    async def _show_new_form(self) -> None:
        await self.query_one(Configurator).swap_companion(
            Static("New folder", classes="cfg-title"),
            Input(placeholder="name — how kickoff directions refer to it "
                              "(default: the folder's own name)",
                  id="newfolder-name"),
            Input(placeholder="absolute path (or Browse)",
                  id="newfolder-path"),
            Select(_MODE_CHOICES, value="ro", allow_blank=False,
                   id="newfolder-mode"),
            Static("", id="newfolder-status"),
            Horizontal(
                Button("Browse", id="newfolder-browse"),
                Button("Add", id="newfolder-create", variant="primary"),
                Button("Cancel", id="newfolder-cancel"),
                id="newfolder-buttons"),
        )
        try:
            self.query_one("#newfolder-name", Input).focus()
        except Exception:
            pass

    def _browse(self) -> None:
        def _picked(path: "Path | None") -> None:
            if path is None:
                return
            try:
                self.query_one("#newfolder-path", Input).value = str(path)
            except Exception:
                pass

        self.app.push_screen(FolderPickerModal(), _picked)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id in ("newfolder-name", "newfolder-path"):
            self._submit_new()

    def _submit_new(self) -> None:
        name = self.query_one("#newfolder-name", Input).value.strip()
        path = self.query_one("#newfolder-path", Input).value.strip()
        mode = self.query_one("#newfolder-mode", Select).value
        if not path:
            self._set_new_status("[bold red]A folder path is required.[/]")
            return
        self._on_new_folder((name, path, str(mode)))

    def _set_new_status(self, text: str) -> None:
        try:
            self.query_one("#newfolder-status", Static).update(text)
        except Exception:
            pass

    def _on_new_folder(self, result: "tuple[str, str, str] | None") -> None:
        """Validate + persist a new folder record. Mirrors the USE-time floor
        (``folder_grant_roots``) so nothing registrable would be silently
        dropped at grant time."""
        if result is None:
            return
        name, raw_path, mode = result
        p = Path(raw_path)
        if not p.is_absolute():
            self._set_new_status(
                "[bold red]The path must be absolute (a mounted share is "
                "registered by its mount point).[/]")
            return
        p = p.resolve()
        if not config.probe_folder(str(p)):
            self._set_new_status(
                f"[bold red]{escape(str(p))} is not a reachable directory — "
                "mount the share / create the folder first.[/]")
            return
        from modulatio import delivery, vault
        from modulatio.leader_gate import dangerous_widen_root

        reason = dangerous_widen_root(
            str(p),
            blocked_subtrees=[str(vault.VAULT_ROOT),
                              str(delivery.delivery_root())],
        )
        if reason is not None:
            self._set_new_status(f"[bold red]{escape(reason)}[/]")
            return
        name = name.strip() or p.name
        folders = config.list_folders()
        if any(r["name"].lower() == name.lower() for r in folders):
            self._set_new_status(
                f"[bold red]A folder named '{escape(name)}' already exists.[/]")
            return
        if any(Path(r["path"]) == p for r in folders):
            self._set_new_status(
                f"[bold red]{escape(str(p))} is already registered "
                f"('{escape(next(r['name'] for r in folders if Path(r['path']) == p))}').[/]")
            return
        folders.append(
            {"name": name, "path": str(p), "mode": mode, "kind": "path"})
        config.save_folders(folders)
        self.notify(f"Registered folder '{name}' ({_MODE_WORDS[mode]}).",
                    severity="information")
        was_form = self._flow == "new"
        self._flow = None
        self._refresh()
        if was_form:
            # Dismiss the add form back to the resting detail card (only when
            # the form was actually up — the companion already rests otherwise).
            self.run_worker(self._rest_companion())

    def action_set_output(self) -> None:
        name = self._selected_name()
        if name:
            self._set_output(name)

    def _set_output(self, name: str) -> None:
        """Pick (or toggle off) the job-output folder — output mode only."""
        rec = next(
            (r for r in config.list_folders() if r["name"] == name), None)
        if rec is None:
            return
        if rec["mode"] != "output":
            self.notify(
                f"'{name}' is {_MODE_WORDS[rec['mode']]} — only an "
                "output-mode folder can receive the job's finished product.",
                severity="warning")
            return
        if config.get_job_output_folder() == name:
            config.set_job_output_folder(None)
            self.notify("Job output back to the default location.",
                        severity="information")
        else:
            config.set_job_output_folder(name)
            self.notify(f"Job output will be delivered to '{name}'.",
                        severity="information")
        self._refresh()

    def action_delete(self) -> None:
        name = self._selected_name()
        if not name:
            return
        self.app.push_screen(
            ConfirmModal(
                f"Remove folder '{name}' from the registry?\n\n"
                "The folder itself is NOT touched — only Modulatio's "
                "registration is removed.",
            ),
            lambda ok: self._do_delete(name) if ok else None,
        )

    def _do_delete(self, name: str) -> None:
        folders = [r for r in config.list_folders() if r["name"] != name]
        config.save_folders(folders)
        if config._load_defaults().get("job_output_folder") == name:
            config.set_job_output_folder(None)
        self.notify(f"Removed '{name}' (the folder on disk is untouched).",
                    severity="warning")
        self._refresh()

    # ── detail companion ────────────────────────────────────────────────

    def on_data_table_row_highlighted(
        self, event: DataTable.RowHighlighted
    ) -> None:
        if self._flow == "new":
            return
        if event.row_key is not None and event.row_key.value:
            self._render_detail(event.row_key.value)

    async def _rest_companion(self) -> None:
        self._flow = None
        await self.query_one(Configurator).swap_companion(
            Static("", id="folder-detail"))
        name = self._selected_name()
        if name:
            self._render_detail(name)

    def _render_detail(self, name: str) -> None:
        rec = next(
            (r for r in config.list_folders() if r["name"] == name), None)
        if rec is None:
            return
        picked = config.get_job_output_folder() == name
        lines = [
            name,
            f"{_MODE_WORDS[rec['mode']]}"
            + (" · ● job output" if picked else ""),
            "─" * 26,
            rec["path"],
            "",
        ]
        if rec["mode"] == "output":
            lines.append("o pick as job output" if not picked
                         else "o un-pick (back to default)")
        lines += ["x remove from registry (folder on disk untouched)",
                  "n new folder", "",
                  "Reference it in kickoff directions by name, e.g. "
                  f"“process the files in folder {name}”."]
        try:
            self.query_one("#folder-detail", Static).update(
                escape("\n".join(lines)))
        except Exception:
            pass

    # ── Internal ────────────────────────────────────────────────────────

    def _selected_name(self) -> "str | None":
        try:
            table = self.query_one("#folders-table", DataTable)
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
            table = self.query_one("#folders-table", DataTable)
        except Exception:
            return
        table.clear()
        picked = config.get_job_output_folder()
        for rec in config.list_folders():
            table.add_row(
                escape(rec["name"]),
                _MODE_WORDS[rec["mode"]],
                "●" if rec["name"] == picked else "",
                "…",
                escape(rec["path"]),
                key=rec["name"],
            )
        if table.row_count > 0:
            # Reachability probes run OFF the UI thread — a dead network mount
            # must never freeze the tab (probe_folder itself also bounds each
            # stat with a timeout).
            self.run_worker(self._probe_and_apply, thread=True)
            if self._flow != "new":
                first = list(table.rows.keys())[0].value
                if first:
                    self._render_detail(first)

    def _probe_statuses(self) -> "dict[str, bool]":
        return {
            rec["name"]: config.probe_folder(rec["path"])
            for rec in config.list_folders()
        }

    def _probe_and_apply(self) -> None:
        statuses = self._probe_statuses()
        self.app.call_from_thread(self._apply_statuses, statuses)

    def _apply_statuses(self, statuses: "dict[str, bool]") -> None:
        try:
            table = self.query_one("#folders-table", DataTable)
        except Exception:
            return
        for name, ok in statuses.items():
            try:
                table.update_cell(
                    name, self._status_col, "ok" if ok else "unreachable")
            except Exception:
                pass


__all__ = ["FoldersScreen"]
