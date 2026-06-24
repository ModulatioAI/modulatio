# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Skills tab — the skill library (a JIT floating pool).

DataTable of shared + project-local skills. New skills are authored via the
inline SkillWizard (or built in conversation with the Leader). Skills are NOT
bound to agents — they are a **just-in-time floating pool**: the engine
capability-matches a task to a skill and loads it onto whatever best-available
producer runs it (no fixed roles — producers ARE their skills). The old
add-to-agent binding was removed accordingly.
"""
from __future__ import annotations

from rich.markup import escape
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, DataTable, Input, Markdown, Static

from modulatio import skills
from modulatio.tui.widgets.confirm_modal import ConfirmModal
from modulatio.tui.widgets.controls_row import ControlsRow
from modulatio.tui.widgets.master_detail import MasterDetail
from modulatio.tui.widgets.skill_wizard import SkillWizard
from modulatio.vault import project_dir


class SkillsScreen(Vertical):
    """Skills tab content — the JIT skill pool + an author flow."""

    BINDINGS = [
        Binding("d", "delete", "Delete", show=True),
    ]

    DEFAULT_CSS = """
    SkillsScreen { padding: 1; }
    SkillsScreen #skills-table { height: 1fr; }
    SkillsScreen #skills-actions { height: 3; }
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._query: str = ""

    def compose(self) -> ComposeResult:
        # Master-detail: the pool on the left, the selected skill on the right.
        with MasterDetail():
            with Vertical(id="md-list"):
                yield ControlsRow(
                    counts=True, search=True, search_placeholder="/ search skills…"
                )
                table = DataTable(id="skills-table", cursor_type="row")
                table.add_columns(
                    "Name", "Description", "Capability Tags", "Project-Local?")
                yield table
                with Horizontal(id="skills-actions"):
                    yield Button("Add skill", id="skills-add-btn", variant="primary")
                yield SkillWizard(id="skill-wizard-panel", classes="hidden")
                # Affordance last so it never pushes the (tall) wizard panel
                # off-screen when it expands.
                yield Static(
                    "↑↓ move · type to search · d delete · Add skill — a JIT "
                    "pool, not bound to agents",
                    id="skills-affordance",
                    classes="affordance",
                )
            with VerticalScroll(id="md-detail"):
                yield Markdown("_Select a skill to view it._", id="skill-detail-md")

    def on_mount(self) -> None:
        self._refresh()

    def on_show(self) -> None:
        self._refresh()

    def _refresh(self) -> None:
        table = self.query_one("#skills-table", DataTable)
        table.clear()
        code = self.app.project_code  # type: ignore[attr-defined]
        project_skills_dir = project_dir(code) / "skills"
        q = self._query.lower()
        for name in skills.list_skills(code):
            s = skills.load_with_metadata(name, project_code=code)
            tags = ", ".join(s.capability_tags)
            # Client-side search: match name / description / tags.
            if q and q not in f"{s.name} {s.description or ''} {tags}".lower():
                continue
            is_local = (project_skills_dir / f"{name}.md").exists()
            table.add_row(
                escape(s.name),
                escape(s.description[:60] if s.description else ""),
                escape(tags),
                "yes" if is_local else "no",
                key=s.name,
            )
        self._set_counts(table.row_count)
        if table.row_count > 0:
            first = list(table.rows.keys())[0].value
            if first:
                self._render_detail(first)
        else:
            self._set_detail(
                "_No skills yet._  Author one with **Add skill**, or ask the "
                "Leader to create one in the LEADER tab.")

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "controls-search":
            self._query = event.value.strip()
            self._refresh()

    def _set_counts(self, shown: int) -> None:
        try:
            label = f"{shown} skills" + (" (filtered)" if self._query else "")
            self.query_one(ControlsRow).set_counts(label)
        except Exception:
            pass

    # ── Delete ──────────────────────────────────────────────────────────────

    def _selected_skill(self) -> str | None:
        try:
            table = self.query_one("#skills-table", DataTable)
            if table.row_count == 0:
                return None
            return table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        except Exception:
            return None

    def action_delete(self) -> None:
        name = self._selected_skill()
        if not name:
            return
        code = self.app.project_code  # type: ignore[attr-defined]
        is_local = (project_dir(code) / "skills" / f"{name}.md").exists()
        scope = "project-local" if is_local else "shared"
        self.app.push_screen(
            ConfirmModal(
                f"Delete the {scope} skill '{name}'?"
                + ("" if is_local else "\n\n(A bundled seed skill can't be "
                   "deleted — only your shared/codified copy.)")),
            lambda ok: self._do_delete(name, is_local) if ok else None,
        )

    def _do_delete(self, name: str, is_local: bool) -> None:
        code = self.app.project_code  # type: ignore[attr-defined]
        try:
            deleted = skills.delete_skill(name, project_code=code if is_local else None)
        except Exception as exc:  # noqa: BLE001 — surface, never crash the screen
            self.app.notify(f"Couldn't delete: {exc}", severity="error")
            return
        self.app.notify(
            f"Deleted skill '{name}'." if deleted
            else f"'{name}' has no deletable copy (bundled seed).")
        self._refresh()

    # ── Detail pane ─────────────────────────────────────────────────────────

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.row_key is not None and event.row_key.value:
            self._render_detail(event.row_key.value)

    def _render_detail(self, name: str) -> None:
        code = self.app.project_code  # type: ignore[attr-defined]
        try:
            s = skills.load_with_metadata(name, project_code=code)
        except Exception:
            self._set_detail(f"_Could not load skill '{escape(name)}'._")
            return
        lines = [f"# {escape(s.name)}", ""]
        if s.description:
            lines += [escape(s.description), ""]
        lines.append("**capability tags:** " + (", ".join(s.capability_tags) or "—"))
        req = getattr(s, "required_capabilities", ()) or ()
        lines.append("**requires:** " + (", ".join(req) or "—"))
        tools = getattr(s, "tool_loadout", ()) or ()
        lines.append("**tool loadout:** " + (", ".join(tools) or "none — pure prose"))
        meta = []
        if getattr(s, "executor", None):
            meta.append(f"executor `{s.executor}`")
        if getattr(s, "freshness_class", None):
            meta.append(f"freshness `{s.freshness_class}`")
        if getattr(s, "version", None):
            meta.append(f"version `{s.version}`")
        if meta:
            lines += ["", "  ·  ".join(meta)]
        lines += [
            "",
            "_routing: capability-match · checked out just-in-time from the pool._",
        ]
        body = getattr(s, "prompt_template", "") or ""
        if body:
            lines += ["", "---", "", body]
        self._set_detail("\n".join(lines))

    def _set_detail(self, source: str) -> None:
        try:
            self.query_one("#skill-detail-md", Markdown).update(source)
        except Exception:
            pass

    # ── Button routing ──────────────────────────────────────────────────────

    def on_button_pressed(self, event: Button.Pressed) -> None:
        wizard = self.query_one("#skill-wizard-panel", SkillWizard)
        if event.button.id == "skills-add-btn":
            wizard.set_project(self.app.project_code)  # type: ignore[attr-defined]
            wizard.reset_fields()
            wizard.remove_class("hidden")
        elif event.button.id == "skill-wiz-cancel-btn":
            wizard.add_class("hidden")
        elif event.button.id == "skill-wiz-create-btn":
            if wizard.submit() is not None:
                wizard.add_class("hidden")
                self._refresh()


def build_skills_panel() -> SkillsScreen:
    return SkillsScreen()


__all__ = ["SkillsScreen", "build_skills_panel"]
