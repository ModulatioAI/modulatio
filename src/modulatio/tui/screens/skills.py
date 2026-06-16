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
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, DataTable, Label

from modulatio import skills
from modulatio.tui.widgets.skill_wizard import SkillWizard
from modulatio.vault import project_dir


class SkillsScreen(Vertical):
    """Skills tab content — the JIT skill pool + an author flow."""

    DEFAULT_CSS = """
    SkillsScreen { padding: 1; }
    SkillsScreen #skills-table { height: 1fr; }
    SkillsScreen #skills-actions { height: 3; }
    """

    def compose(self) -> ComposeResult:
        yield Label("Skills registry — a just-in-time floating pool")
        table = DataTable(id="skills-table", cursor_type="row")
        table.add_columns("Name", "Description", "Capability Tags", "Project-Local?")
        yield table
        with Horizontal(id="skills-actions"):
            yield Button("Add skill", id="skills-add-btn", variant="primary")
        yield SkillWizard(id="skill-wizard-panel", classes="hidden")

    def on_mount(self) -> None:
        self._refresh()

    def on_show(self) -> None:
        self._refresh()

    def _refresh(self) -> None:
        table = self.query_one("#skills-table", DataTable)
        table.clear()
        code = self.app.project_code  # type: ignore[attr-defined]
        project_skills_dir = project_dir(code) / "skills"
        for name in skills.list_skills(code):
            s = skills.load_with_metadata(name, project_code=code)
            is_local = (project_skills_dir / f"{name}.md").exists()
            table.add_row(
                escape(s.name),
                escape(s.description[:60] if s.description else ""),
                escape(", ".join(s.capability_tags)),
                "yes" if is_local else "no",
                key=s.name,
            )

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
