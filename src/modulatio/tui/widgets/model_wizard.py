# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Model wizard (inline panel) — slice #26.

Thin wrapper around ``roster.add_model``. Unlike AgentWizard /
SkillWizard, this one is update-only: the agent Select is populated
from the live roster (you can't update a non-existent agent) and only
the new model id is free-form.

Same inline-panel template as #24/#25 so the wizard idiom stays
consistent across all three.
"""
from __future__ import annotations

from rich.markup import escape
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Input, Label, Select, Static

from modulatio import roster
from modulatio.roster import Agent


class ModelWizard(Vertical):
    """Inline wizard for updating an existing agent's model id."""

    DEFAULT_CSS = """
    ModelWizard {
        padding: 1;
        border: solid $accent;
        height: auto;
    }
    ModelWizard.hidden {
        display: none;
    }
    ModelWizard Input, ModelWizard Select {
        margin-bottom: 1;
    }
    #model-wiz-status {
        padding: 1 0;
    }
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._project_code: str = ""
        self.last_updated: Agent | None = None

    def compose(self) -> ComposeResult:
        yield Label("Update an agent's model")
        yield Label("Agent")
        yield Select([], id="model-wiz-agent", allow_blank=True)
        yield Label("New model id (required)")
        yield Input(placeholder="<preset-key> or stub", id="model-wiz-model")
        with Horizontal():
            yield Button("Update", id="model-wiz-update-btn", variant="primary")
            yield Button("Cancel", id="model-wiz-cancel-btn")
        yield Static("", id="model-wiz-status")

    def set_project(self, project_code: str) -> None:
        """Bind the wizard to a project and populate the agent Select
        from that project's live roster."""
        self._project_code = project_code
        self.last_updated = None
        agents = roster.list_agents(project_code)
        options = [(a.id, a.id) for a in agents]
        sel = self.query_one("#model-wiz-agent", Select)
        sel.set_options(options)
        if options:
            sel.value = options[0][1]

    def reset_fields(self) -> None:
        self.query_one("#model-wiz-model", Input).value = ""
        self.query_one("#model-wiz-status", Static).update("")

    def submit(self) -> Agent | None:
        status = self.query_one("#model-wiz-status", Static)
        self.last_updated = None

        agent_sel = self.query_one("#model-wiz-agent", Select)
        agent_id = agent_sel.value
        model = self.query_one("#model-wiz-model", Input).value.strip()

        if not agent_id:
            status.update("[red]Required field missing: Agent[/red]")
            return None
        if not model:
            status.update("[red]Required field missing: Model[/red]")
            return None

        try:
            updated = roster.add_model(
                project_code=self._project_code,
                agent_id=agent_id,
                model=model,
            )
        except FileNotFoundError as exc:
            # re-sweep #1: escape — exc embeds the operator-typed agent id
            status.update(f"[red]Agent not found: {escape(str(exc))}[/red]")
            return None
        except Exception as exc:
            status.update(f"[red]Failed to update model: {escape(str(exc))}[/red]")
            return None

        self.last_updated = updated
        # re-sweep #1: updated.model is the free-form operator-typed id; a
        # `[/]`-class value would raise MarkupError in Static.update otherwise.
        status.update(
            f"[green]Updated {escape(str(updated.id))} → "
            f"{escape(str(updated.model))}[/green]"
        )
        return updated


__all__ = ["ModelWizard"]
