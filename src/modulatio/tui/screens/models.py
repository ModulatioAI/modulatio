# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Models tab — slice #26.

Per-agent model config viewer. One row per roster agent showing
current model + tier + cost_class. Update button opens an inline
ModelWizard that dispatches to ``roster.add_model`` — the wizard is
update-only since a new model belongs to an existing agent (create
an agent via the Agents tab first).
"""
from __future__ import annotations

from rich.markup import escape
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, DataTable, Label, Static

from modulatio import roster
from modulatio.tui.widgets.model_wizard import ModelWizard


class ModelsScreen(Vertical):
    """Models tab content."""

    DEFAULT_CSS = """
    ModelsScreen {
        padding: 1;
    }
    ModelsScreen #models-buttons {
        height: 3;
    }
    ModelsScreen #models-buttons Button {
        margin-right: 1;
    }
    """

    def compose(self) -> ComposeResult:
        yield Label("Agent models")
        table = DataTable(id="models-table", cursor_type="row")
        table.add_columns("Agent ID", "Name", "Model", "Tier", "Cost Class")
        yield table
        with Horizontal(id="models-buttons"):
            yield Button(
                "Update model…", id="models-update-btn", variant="primary"
            )
            yield Button(
                "Clear model", id="models-clear-btn", variant="warning"
            )
        yield Static("", id="models-status")
        yield ModelWizard(id="model-wizard-panel", classes="hidden")

    def on_mount(self) -> None:
        self._refresh()

    def on_show(self) -> None:
        self._refresh()

    def _refresh(self) -> None:
        table = self.query_one("#models-table", DataTable)
        table.clear()
        code = self.app.project_code  # type: ignore[attr-defined]
        for agent in roster.list_agents(code):
            table.add_row(
                agent.id,
                agent.name,
                agent.model or "",
                agent.model_tier or "",
                agent.cost_class or "",
                key=agent.id,
            )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        wizard = self.query_one("#model-wizard-panel", ModelWizard)
        if event.button.id == "models-update-btn":
            wizard.set_project(self.app.project_code)  # type: ignore[attr-defined]
            wizard.reset_fields()
            wizard.remove_class("hidden")
        elif event.button.id == "models-clear-btn":
            self._clear_selected_model()
        elif event.button.id == "model-wiz-cancel-btn":
            wizard.add_class("hidden")
        elif event.button.id == "model-wiz-update-btn":
            if wizard.submit() is not None:
                wizard.add_class("hidden")
                self._refresh()

    def _clear_selected_model(self) -> None:
        """Clear the cursor row's agent's model assignment via
        ``roster.clear_model``. Refreshes the table and the status line."""
        table = self.query_one("#models-table", DataTable)
        status = self.query_one("#models-status", Static)
        if table.row_count == 0:
            status.update("[dim]no agents to clear[/]")
            return
        try:
            cell_key = table.coordinate_to_cell_key(table.cursor_coordinate)
            agent_id = cell_key.row_key.value
        except Exception:
            status.update("[dim]select a row first[/]")
            return
        if not agent_id:
            status.update("[dim]select a row first[/]")
            return
        try:
            roster.clear_model(
                project_code=self.app.project_code,  # type: ignore[attr-defined]
                agent_id=agent_id,
            )
        except FileNotFoundError as exc:
            status.update(f"[bold red]Clear failed:[/] {escape(str(exc))}")
            return
        status.update(
            f"[bold green]✓ Cleared[/] [dim]model assignment for[/]"
            f" {escape(agent_id)}"
        )
        self._refresh()


def build_models_panel() -> ModelsScreen:
    return ModelsScreen()


__all__ = ["ModelsScreen", "build_models_panel"]
