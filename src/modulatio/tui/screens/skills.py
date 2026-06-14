# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Skills tab — slice #25 + add-to-agent binding flow.

DataTable of shared + project-local skills. Two action buttons:

- **Add skill** (slice #25): inline SkillWizard for creating a new
  skill in either scope.
- **Add to agent**: bind an existing skill to an existing agent. Pick
  a skill row, click the button, choose an eligible agent (one that
  doesn't already hold the skill), Confirm. The roster file is
  rewritten via ``roster.save`` and the Skills tab refreshes.

The binding flow originates here, not on the Agents tab, because the
typical mental model is "I just authored a skill — who needs it?".
The Agents tab keeps showing skills as a read-only column.
"""
from __future__ import annotations

from rich.markup import escape
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, DataTable, Label, Select

from modulatio import roster, skills
from modulatio.tui.widgets.skill_wizard import SkillWizard
from modulatio.vault import project_dir


class SkillsScreen(Vertical):
    """Skills tab content."""

    DEFAULT_CSS = """
    SkillsScreen {
        padding: 1;
    }
    SkillsScreen #skills-actions {
        height: 3;
    }
    SkillsScreen #skills-bind-panel {
        border: round $accent;
        padding: 1;
        height: auto;
        margin-top: 1;
    }
    SkillsScreen #skills-bind-panel.hidden {
        display: none;
    }
    SkillsScreen #skills-bind-buttons {
        height: 3;
    }
    """

    def compose(self) -> ComposeResult:
        yield Label("Skills registry")
        table = DataTable(id="skills-table", cursor_type="row")
        table.add_columns("Name", "Description", "Capability Tags", "Project-Local?")
        yield table
        with Horizontal(id="skills-actions"):
            yield Button("Add skill", id="skills-add-btn", variant="primary")
            yield Button("Add to agent", id="skills-add-to-agent-btn")
        yield SkillWizard(id="skill-wizard-panel", classes="hidden")
        # ── Binding panel (hidden by default) ────────────────────────────
        with Vertical(id="skills-bind-panel", classes="hidden"):
            yield Label(
                "(select a skill, then choose an agent)",
                id="skills-bind-title",
            )
            yield Select(
                options=[],
                id="skills-bind-agent-select",
                allow_blank=True,
                prompt="(pick agent)",
            )
            yield Label("", id="skills-bind-status")
            with Horizontal(id="skills-bind-buttons"):
                yield Button(
                    "Add",
                    id="skills-bind-confirm-btn",
                    variant="primary",
                )
                yield Button("Cancel", id="skills-bind-cancel-btn")

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

    # ── Binding panel helpers ───────────────────────────────────────────────

    def _selected_skill_name(self) -> str | None:
        """Return the skill name under the table cursor, or None."""
        table = self.query_one("#skills-table", DataTable)
        if table.row_count == 0:
            return None
        try:
            cell_key = table.coordinate_to_cell_key(table.cursor_coordinate)
            return cell_key.row_key.value
        except Exception:
            return None

    def _open_bind_panel(self) -> None:
        """Reveal the bind panel, populated with agents that DON'T
        already hold the cursor's skill."""
        skill_name = self._selected_skill_name()
        title = self.query_one("#skills-bind-title", Label)
        status = self.query_one("#skills-bind-status", Label)
        select = self.query_one("#skills-bind-agent-select", Select)
        confirm = self.query_one("#skills-bind-confirm-btn", Button)
        panel = self.query_one("#skills-bind-panel")

        if skill_name is None:
            status.update("Select a skill row first.")
            select.set_options([])
            confirm.disabled = True
            panel.remove_class("hidden")
            return

        title.update(f"Bind '{skill_name}' to agent")

        code = self.app.project_code  # type: ignore[attr-defined]
        eligible = [
            a for a in roster.list_agents(code)
            if skill_name not in a.skills
        ]
        if not eligible:
            status.update(
                f"No eligible agents — every agent already has '{skill_name}'."
            )
            select.set_options([])
            confirm.disabled = True
        else:
            options = [
                (f"{a.id} — {a.name}", a.id) for a in eligible
            ]
            select.set_options(options)
            status.update("")
            confirm.disabled = False

        # Stash the target skill on the screen so Confirm knows what
        # to bind.
        self._bind_target_skill: str | None = skill_name
        panel.remove_class("hidden")

    def _hide_bind_panel(self) -> None:
        self.query_one("#skills-bind-panel").add_class("hidden")
        self._bind_target_skill = None

    def _confirm_bind(self) -> None:
        """Append the target skill to the selected agent and persist."""
        target = getattr(self, "_bind_target_skill", None)
        if not target:
            return
        select = self.query_one("#skills-bind-agent-select", Select)
        agent_id = select.value
        status = self.query_one("#skills-bind-status", Label)
        if agent_id in (Select.BLANK, None, ""):
            status.update("Pick an agent first.")
            return
        code = self.app.project_code  # type: ignore[attr-defined]
        agent = roster.load(agent_id, code)
        if agent is None:
            status.update(f"Agent {agent_id!r} not found.")
            return
        if target in agent.skills:
            # Defensive — UI should have filtered this out, but the
            # roster is the source of truth.
            status.update(
                f"Agent {agent_id} already has '{target}' — nothing to do."
            )
            return
        agent.skills.append(target)
        try:
            roster.save(agent, code)
        except Exception as exc:  # noqa: BLE001 — surface any persistence error
            # re-sweep: escape exc text — an un-escaped [/] in the message
            # would raise MarkupError inside the error handler itself.
            status.update(
                f"Error saving: {type(exc).__name__}: {escape(str(exc))}"
            )
            return
        self._hide_bind_panel()
        self._refresh()

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
        elif event.button.id == "skills-add-to-agent-btn":
            self._open_bind_panel()
        elif event.button.id == "skills-bind-confirm-btn":
            self._confirm_bind()
        elif event.button.id == "skills-bind-cancel-btn":
            self._hide_bind_panel()


def build_skills_panel() -> SkillsScreen:
    return SkillsScreen()


__all__ = ["SkillsScreen", "build_skills_panel"]
