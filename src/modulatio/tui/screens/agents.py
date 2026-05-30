# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Agents tab — slice #24.

DataTable of the project roster + inline AgentWizard for Add. Submits
route through ``roster.add_agent`` (slice #18); the list refreshes on
successful create so the new agent shows immediately.
"""
from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Button, DataTable, Label

from modulatio import roster
from modulatio.tui.widgets.agent_wizard import AgentWizard


class AgentsScreen(Vertical):
    """Agents tab content."""

    DEFAULT_CSS = """
    AgentsScreen {
        padding: 1;
    }
    """

    def compose(self) -> ComposeResult:
        yield Label("Team — skill-holders work is routed to")
        table = DataTable(id="agents-table", cursor_type="row")
        table.add_columns("ID", "Name", "Role", "Model", "Skills")
        yield table
        yield Button("Add team member", id="agents-add-btn", variant="primary")
        yield AgentWizard(id="agent-wizard-panel", classes="hidden")

    def on_mount(self) -> None:
        self._refresh()

    def on_show(self) -> None:
        self._refresh()

    def _refresh(self) -> None:
        table = self.query_one("#agents-table", DataTable)
        table.clear()
        code = self.app.project_code  # type: ignore[attr-defined]
        for agent in roster.list_agents(code):
            table.add_row(
                agent.id,
                agent.name,
                agent.tier,
                agent.model or "",
                ", ".join(agent.skills),
                key=agent.id,
            )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        wizard = self.query_one("#agent-wizard-panel", AgentWizard)
        if event.button.id == "agents-add-btn":
            wizard.set_project(self.app.project_code)  # type: ignore[attr-defined]
            wizard.reset_fields()
            wizard.remove_class("hidden")
        elif event.button.id == "agent-wiz-cancel-btn":
            wizard.add_class("hidden")
        elif event.button.id == "agent-wiz-create-btn":
            created = wizard.submit()
            if created is not None:
                wizard.add_class("hidden")
                self._refresh()


def build_agents_panel() -> AgentsScreen:
    return AgentsScreen()


__all__ = ["AgentsScreen", "build_agents_panel"]
