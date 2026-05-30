# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Agent wizard (inline panel) — slice #24.

Same inline-panel pattern as slice #23's ExportDialog. Hidden by
default; reveal by removing the ``hidden`` class. Fields mirror
``roster.add_agent``'s kwargs so Submit is a thin wrapper.

Scope: Add only. Edit-via-prepopulation is deferred — slice #18's
add_agent raises on duplicates and has no update variant for agents
(only add_model). A future slice can introduce that semantics cleanly.
"""
from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Input, Label, Select, Static, TextArea

from modulatio import roster
from modulatio.roster import Agent


# Skills-first model: a team member is a model + the skills it holds.
# "producer" is the default — it holds work skills and the team routes
# matching tasks to it. "leader" and "qc" are the two structural roles.
# (A prior standalone "coordinator" role was removed engine-side and is
# no longer offered here.)
_ROLE_CHOICES: tuple[tuple[str, str], ...] = (
    ("skill-holder (holds skills, gets routed work)", "producer"),
    ("qc (verifier)", "qc"),
    ("leader (plans + decides)", "leader"),
)


class AgentWizard(Vertical):
    """Inline wizard for creating a new roster agent."""

    DEFAULT_CSS = """
    AgentWizard {
        padding: 1;
        border: solid $accent;
        height: auto;
    }
    AgentWizard.hidden {
        display: none;
    }
    AgentWizard Input, AgentWizard Select, AgentWizard TextArea {
        margin-bottom: 1;
    }
    AgentWizard TextArea {
        height: 4;
    }
    #agent-wiz-status {
        padding: 1 0;
    }
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._project_code: str = ""
        #: Set to the newly-created Agent after a successful submit, or
        #: None if the last action was cancel/empty/dup. Hosts can use
        #: this to drive UI refreshes + post-submit side effects.
        self.last_created: Agent | None = None

    def compose(self) -> ComposeResult:
        yield Label("Add a team member (a model + the skills it holds)")
        yield Label("ID (required)")
        yield Input(placeholder="e.g. writer-c", id="agent-wiz-id")
        yield Label("Display name (required)")
        yield Input(placeholder="e.g. Writer C", id="agent-wiz-name")
        yield Label("Identity")
        yield TextArea(id="agent-wiz-identity")
        yield Label("Skills (comma-separated) — tasks route here by matching skill")
        yield Input(placeholder="research, code, write", id="agent-wiz-skills")
        yield Label("Model id")
        yield Input(placeholder="<preset-key> or stub", id="agent-wiz-model")
        yield Label("Role")
        yield Select(_ROLE_CHOICES, value="producer", id="agent-wiz-tier", allow_blank=False)
        with Horizontal():
            yield Button("Create", id="agent-wiz-create-btn", variant="primary")
            yield Button("Cancel", id="agent-wiz-cancel-btn")
        yield Static("", id="agent-wiz-status")

    def set_project(self, project_code: str) -> None:
        """Bind the wizard to a project so Submit knows where to write."""
        self._project_code = project_code
        self.last_created = None

    def reset_fields(self) -> None:
        self.query_one("#agent-wiz-id", Input).value = ""
        self.query_one("#agent-wiz-name", Input).value = ""
        self.query_one("#agent-wiz-identity", TextArea).text = ""
        self.query_one("#agent-wiz-skills", Input).value = ""
        self.query_one("#agent-wiz-model", Input).value = ""
        sel = self.query_one("#agent-wiz-tier", Select)
        sel.value = "producer"
        self.query_one("#agent-wiz-status", Static).update("")

    def submit(self) -> Agent | None:
        """Read fields, validate, dispatch to ``roster.add_agent``. Returns
        the created Agent on success, None otherwise. Status label carries
        the error message on failure."""
        status = self.query_one("#agent-wiz-status", Static)
        self.last_created = None

        agent_id = self.query_one("#agent-wiz-id", Input).value.strip()
        name = self.query_one("#agent-wiz-name", Input).value.strip()
        identity = self.query_one("#agent-wiz-identity", TextArea).text
        skills_raw = self.query_one("#agent-wiz-skills", Input).value.strip()
        model = self.query_one("#agent-wiz-model", Input).value.strip() or None
        tier = self.query_one("#agent-wiz-tier", Select).value or "producer"

        missing = [
            field for field, value in (
                ("Agent ID", agent_id),
                ("Display name", name),
            )
            if not value
        ]
        if missing:
            status.update(
                f"[red]Required field(s) missing or empty: {', '.join(missing)}[/red]"
            )
            return None

        skills_list = [s.strip() for s in skills_raw.split(",") if s.strip()]

        try:
            agent = roster.add_agent(
                project_code=self._project_code,
                agent_id=agent_id,
                name=name,
                identity=identity,
                skills=skills_list,
                model=model,
                tier=tier,
            )
        except FileExistsError as exc:
            status.update(f"[red]Agent already exists: {exc}[/red]")
            return None
        except Exception as exc:
            status.update(f"[red]Failed to create agent: {exc}[/red]")
            return None

        self.last_created = agent
        status.update(f"[green]Created {agent.id}[/green]")
        return agent


__all__ = ["AgentWizard"]
