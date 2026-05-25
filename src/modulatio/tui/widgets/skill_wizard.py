"""Skill wizard (inline panel) — slice #25.

Same inline-panel pattern as slice #24's AgentWizard. Thin wrapper
around ``skills.create_skill`` with a scope toggle (shared vs
project-local) that maps to the ``project_code`` kwarg.

MVP scope: name, description, prompt_template, capability_tags,
required_capabilities, model_tier, cost_class, scope. Advanced fields
(tool_loadout, standards_domain, executor) are edited in the skill
file directly — adding UI for them is its own slice when concrete.
"""
from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Input, Label, Select, Static, TextArea

from modulatio import skills
from modulatio.skills import Skill


_COST_CHOICES: tuple[tuple[str, str], ...] = (
    ("free-local", "free-local"),
    ("paid-cloud", "paid-cloud"),
    ("premium-cloud", "premium-cloud"),
)

_SCOPE_CHOICES: tuple[tuple[str, str], ...] = (
    ("shared", "shared"),
    ("project", "project"),
)


class SkillWizard(Vertical):
    """Inline wizard for creating a new skill (shared or project-local)."""

    DEFAULT_CSS = """
    SkillWizard {
        padding: 1;
        border: solid $accent;
        height: auto;
    }
    SkillWizard.hidden {
        display: none;
    }
    SkillWizard Input, SkillWizard Select, SkillWizard TextArea {
        margin-bottom: 1;
    }
    SkillWizard TextArea {
        height: 5;
    }
    #skill-wiz-status {
        padding: 1 0;
    }
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._project_code: str = ""
        self.last_created: Skill | None = None

    def compose(self) -> ComposeResult:
        yield Label("Create a new skill")
        yield Label("Name (required)")
        yield Input(placeholder="e.g. researcher", id="skill-wiz-name")
        yield Label("Description (required)")
        yield Input(placeholder="What does this skill do?", id="skill-wiz-description")
        yield Label("Prompt template (required)")
        yield TextArea(id="skill-wiz-prompt")
        yield Label("Capability tags (comma-separated)")
        yield Input(placeholder="writing, research", id="skill-wiz-capabilities")
        yield Label("Required capabilities (executor floor, comma-separated)")
        yield Input(placeholder="reasoning-heavy", id="skill-wiz-required-caps")
        yield Label("Model tier")
        yield Input(placeholder="tactical / strategic / …", id="skill-wiz-model-tier")
        yield Label("Cost class")
        yield Select(
            _COST_CHOICES, value="paid-cloud",
            id="skill-wiz-cost-class", allow_blank=False,
        )
        yield Label("Scope")
        yield Select(
            _SCOPE_CHOICES, value="shared",
            id="skill-wiz-scope", allow_blank=False,
        )
        with Horizontal():
            yield Button("Create", id="skill-wiz-create-btn", variant="primary")
            yield Button("Cancel", id="skill-wiz-cancel-btn")
        yield Static("", id="skill-wiz-status")

    def set_project(self, project_code: str) -> None:
        self._project_code = project_code
        self.last_created = None

    def reset_fields(self) -> None:
        for field_id in (
            "#skill-wiz-name",
            "#skill-wiz-description",
            "#skill-wiz-capabilities",
            "#skill-wiz-required-caps",
            "#skill-wiz-model-tier",
        ):
            self.query_one(field_id, Input).value = ""
        self.query_one("#skill-wiz-prompt", TextArea).text = ""
        self.query_one("#skill-wiz-cost-class", Select).value = "paid-cloud"
        self.query_one("#skill-wiz-scope", Select).value = "shared"
        self.query_one("#skill-wiz-status", Static).update("")

    def submit(self) -> Skill | None:
        status = self.query_one("#skill-wiz-status", Static)
        self.last_created = None

        name = self.query_one("#skill-wiz-name", Input).value.strip()
        description = self.query_one("#skill-wiz-description", Input).value.strip()
        prompt = self.query_one("#skill-wiz-prompt", TextArea).text

        missing = [
            field for field, value in (
                ("Name", name),
                ("Description", description),
                ("Prompt template", prompt),
            )
            if not value.strip()
        ]
        if missing:
            status.update(
                f"[red]Required field(s) missing or empty: {', '.join(missing)}[/red]"
            )
            return None

        caps_raw = self.query_one("#skill-wiz-capabilities", Input).value
        req_raw = self.query_one("#skill-wiz-required-caps", Input).value
        capability_tags = tuple(c.strip() for c in caps_raw.split(",") if c.strip())
        required_capabilities = tuple(c.strip() for c in req_raw.split(",") if c.strip())

        model_tier = self.query_one("#skill-wiz-model-tier", Input).value.strip() or None
        cost_class = self.query_one("#skill-wiz-cost-class", Select).value or None
        scope = self.query_one("#skill-wiz-scope", Select).value or "shared"
        project_code = self._project_code if scope == "project" else None

        try:
            created = skills.create_skill(
                name=name,
                description=description,
                prompt_template=prompt,
                capability_tags=capability_tags,
                required_capabilities=required_capabilities,
                model_tier=model_tier,
                cost_class=cost_class,
                project_code=project_code,
            )
        except FileExistsError as exc:
            status.update(f"[red]Skill already exists: {exc}[/red]")
            return None
        except Exception as exc:
            status.update(f"[red]Failed to create skill: {exc}[/red]")
            return None

        self.last_created = created
        status.update(f"[green]Created {created.name}[/green]")
        return created


__all__ = ["SkillWizard"]
