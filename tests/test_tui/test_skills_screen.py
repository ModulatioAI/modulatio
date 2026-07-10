"""Tests for slice #25 — Skills tab + add wizard.

Consumes slice #18's ``skills.create_skill``. Lists skills (shared +
project-local) in a DataTable with scope column; Add button reveals
an inline wizard that creates either scope via the same API. Reuses
the wizard pattern established in slice #24.

MVP scope: core fields only (name, description, prompt_template,
capability_tags, required_capabilities, model_tier, cost_class, scope).
Advanced fields (tool_loadout, standards_domain, executor) are edited
directly in the skill file — extendable in a polish slice if the need
is concrete.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from modulatio import skills, vault
from rich.errors import MarkupError
from textual.app import App, ComposeResult
from textual.widgets import DataTable
from modulatio.tui.screens.skills import SkillsScreen


PROJECT_CODE = "SKL"


@pytest.fixture
def tui_vault_with_skills(tmp_path: Path, monkeypatch):
    """Pre-seed the vault + two skills: one shared, one project-local."""
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path / "projects")
    monkeypatch.setattr(skills, "_SKILLS_ROOT", tmp_path / "shared")
    # Isolate from package-bundled seed skills (coding, code-review)
    # so tests that assert on counts/lists see only the fixture's
    # contributions. Production users still see the seed via the
    # loader's fallback chain.
    monkeypatch.setattr(skills, "_SEED_SKILLS_ROOT", tmp_path / "no-seed")
    vault.init_project(PROJECT_CODE, "Skill fixture", "obj")

    skills.create_skill(
        name="drafter",
        description="Drafts artifacts",
        prompt_template="Draft body stub.",
        capability_tags=("writing",),
        model_tier="tactical",
        cost_class="paid-cloud",
    )
    skills.create_skill(
        name="local-only",
        description="Project-specific helper",
        prompt_template="Local body stub.",
        capability_tags=("research",),
        project_code=PROJECT_CODE,
    )
    return tmp_path


# ─── Skills tab replaces placeholder + lists skills ─────────────────────────


async def test_skills_tab_replaces_placeholder(tui_vault_with_skills):
    from textual.widgets import DataTable, TabbedContent

    from modulatio.tui.app import ModulatioApp

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()
        app.query_one(TabbedContent).active = "tab-skills"
        await pilot.pause()
        assert app.query_one("#skills-table", DataTable) is not None


async def test_skills_table_lists_shared_and_project_skills(tui_vault_with_skills):
    """Both pre-seeded skills show. Columns: Name / Description /
    Capability Tags / Project-Local?."""
    from textual.widgets import DataTable, TabbedContent

    from modulatio.tui.app import ModulatioApp

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()
        app.query_one(TabbedContent).active = "tab-skills"
        await pilot.pause()
        table = app.query_one("#skills-table", DataTable)
        assert table.row_count == 2
        assert len(table.columns) == 4


# ─── Wizard: open / submit / cancel ─────────────────────────────────────────


async def test_add_wizard_hidden_by_default(tui_vault_with_skills):
    from textual.widgets import TabbedContent

    from modulatio.tui.app import ModulatioApp

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()
        app.query_one(TabbedContent).active = "tab-skills"
        await pilot.pause()
        assert app.query_one("#skill-wizard-panel").has_class("hidden")


async def test_clicking_add_reveals_wizard(tui_vault_with_skills):
    """Add button opens the wizard; expected fields are present."""
    from textual.widgets import TabbedContent

    from modulatio.tui.app import ModulatioApp

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()
        app.query_one(TabbedContent).active = "tab-skills"
        await pilot.pause()
        await pilot.click("#skills-add-btn")
        await pilot.pause()
        assert not app.query_one("#skill-wizard-panel").has_class("hidden")
        for field_id in (
            "#skill-wiz-name",
            "#skill-wiz-description",
            "#skill-wiz-prompt",
            "#skill-wiz-capabilities",
            "#skill-wiz-required-caps",
            "#skill-wiz-model-tier",
            "#skill-wiz-cost-class",
            "#skill-wiz-scope",
        ):
            assert app.query_one(field_id) is not None


async def test_wizard_submit_creates_shared_skill(tui_vault_with_skills, tmp_path):
    """Submit with scope=shared → writes to shared skills root."""
    from textual.widgets import Input, Select, TabbedContent, TextArea

    from modulatio.tui.app import ModulatioApp

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()
        app.query_one(TabbedContent).active = "tab-skills"
        await pilot.pause()
        await pilot.click("#skills-add-btn")
        await pilot.pause()

        app.query_one("#skill-wiz-name", Input).value = "new-shared"
        app.query_one("#skill-wiz-description", Input).value = "A new shared skill"
        app.query_one("#skill-wiz-prompt", TextArea).text = "Prompt body."
        app.query_one("#skill-wiz-capabilities", Input).value = "writing, research"
        app.query_one("#skill-wiz-required-caps", Input).value = "reasoning-heavy"
        app.query_one("#skill-wiz-model-tier", Input).value = "tactical"
        app.query_one("#skill-wiz-cost-class", Select).value = "paid-cloud"
        app.query_one("#skill-wiz-scope", Select).value = "shared"
        await pilot.pause()
        await pilot.click("#skill-wiz-create-btn")
        await pilot.pause()

        assert app.query_one("#skill-wizard-panel").has_class("hidden")

    # Shared scope → file under shared root.
    shared_path = tmp_path / "shared" / "new-shared.md"
    assert shared_path.exists()


async def test_wizard_submit_creates_project_local_skill(tui_vault_with_skills, tmp_path):
    """Submit with scope=project → writes under project vault."""
    from textual.widgets import Input, Select, TabbedContent, TextArea

    from modulatio.tui.app import ModulatioApp

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()
        app.query_one(TabbedContent).active = "tab-skills"
        await pilot.pause()
        await pilot.click("#skills-add-btn")
        await pilot.pause()

        app.query_one("#skill-wiz-name", Input).value = "project-helper"
        app.query_one("#skill-wiz-description", Input).value = "Per-project helper"
        app.query_one("#skill-wiz-prompt", TextArea).text = "Local body."
        app.query_one("#skill-wiz-scope", Select).value = "project"
        await pilot.pause()
        await pilot.click("#skill-wiz-create-btn")
        await pilot.pause()

    project_path = tmp_path / "projects" / PROJECT_CODE.lower() / "skills" / "project-helper.md"
    assert project_path.exists()


async def test_wizard_rejects_empty_required_fields(tui_vault_with_skills):
    from textual.widgets import TabbedContent

    from modulatio.tui.app import ModulatioApp

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()
        app.query_one(TabbedContent).active = "tab-skills"
        await pilot.pause()
        await pilot.click("#skills-add-btn")
        await pilot.pause()
        await pilot.click("#skill-wiz-create-btn")
        await pilot.pause()
        assert not app.query_one("#skill-wizard-panel").has_class("hidden")
        rendered = str(app.query_one("#skill-wiz-status").render()).lower()
        assert "required" in rendered or "missing" in rendered or "empty" in rendered


async def test_wizard_surfaces_duplicate_error(tui_vault_with_skills):
    """Dup skill name at the same scope → FileExistsError → status."""
    from textual.widgets import Input, Select, TabbedContent, TextArea

    from modulatio.tui.app import ModulatioApp

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()
        app.query_one(TabbedContent).active = "tab-skills"
        await pilot.pause()
        await pilot.click("#skills-add-btn")
        await pilot.pause()

        app.query_one("#skill-wiz-name", Input).value = "drafter"  # dup shared
        app.query_one("#skill-wiz-description", Input).value = "Dup attempt"
        app.query_one("#skill-wiz-prompt", TextArea).text = "dup"
        app.query_one("#skill-wiz-scope", Select).value = "shared"
        await pilot.pause()
        await pilot.click("#skill-wiz-create-btn")
        await pilot.pause()

        assert not app.query_one("#skill-wizard-panel").has_class("hidden")
        rendered = str(app.query_one("#skill-wiz-status").render()).lower()
        assert "exists" in rendered or "already" in rendered


async def test_cancel_hides_wizard(tui_vault_with_skills):
    from textual.widgets import TabbedContent

    from modulatio.tui.app import ModulatioApp

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()
        app.query_one(TabbedContent).active = "tab-skills"
        await pilot.pause()
        await pilot.click("#skills-add-btn")
        await pilot.pause()
        assert not app.query_one("#skill-wizard-panel").has_class("hidden")
        await pilot.click("#skill-wiz-cancel-btn")
        await pilot.pause()
        assert app.query_one("#skill-wizard-panel").has_class("hidden")


# ─── Controls row + affordance (Feng-Tui overhaul) ──────────────────────────


async def test_skills_has_controls_row_with_counts_and_search(tui_vault_with_skills):
    """The list yields a ControlsRow (counts + search) atop the table; counts
    reports the visible skill total."""
    from textual.widgets import Static, TabbedContent

    from modulatio.tui.app import ModulatioApp
    from modulatio.tui.screens.skills import SkillsScreen
    from modulatio.tui.widgets.controls_row import ControlsRow

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()
        app.query_one(TabbedContent).active = "tab-skills"
        await pilot.pause()
        row = app.query_one(SkillsScreen).query_one(ControlsRow)
        assert row.query("#controls-counts")
        assert row.query("#controls-search")
        counts = str(row.query_one("#controls-counts", Static).render())
        assert "2 skills" in counts


async def test_skills_search_filters_rows(tui_vault_with_skills):
    """Typing a query filters the table to matching rows (name/description/tags)
    and flags the counts as filtered."""
    from textual.widgets import DataTable, Static, TabbedContent

    from modulatio.tui.app import ModulatioApp
    from modulatio.tui.screens.skills import SkillsScreen

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()
        app.query_one(TabbedContent).active = "tab-skills"
        await pilot.pause()
        screen = app.query_one(SkillsScreen)
        screen._query = "local"  # matches only the project-local skill
        screen._refresh()
        await pilot.pause()
        assert screen.query_one("#skills-table", DataTable).row_count == 1
        counts = str(screen.query_one("#controls-counts", Static).render())
        assert "filtered" in counts


async def test_skills_affordance_present(tui_vault_with_skills):
    """The list carries an affordance line that names searching + adding."""
    from textual.widgets import Static, TabbedContent

    from modulatio.tui.app import ModulatioApp

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()
        app.query_one(TabbedContent).active = "tab-skills"
        await pilot.pause()
        text = str(app.query_one("#skills-affordance", Static).render())
        assert "search" in text.lower()
        assert "add" in text.lower()


# ─── Delete wiring (Feng-Tui SKILLS overhaul) ───────────────────────────────


async def test_d_delete_removes_a_skill(tui_vault_with_skills):
    from textual.widgets import TabbedContent

    from modulatio import skills as skills_mod
    from modulatio.tui.app import ModulatioApp
    from modulatio.tui.screens.skills import SkillsScreen

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()
        app.query_one(TabbedContent).active = "tab-skills"
        await pilot.pause()
        screen = app.query_one(SkillsScreen)
        # the fixture's shared skill is "drafter"
        screen._do_delete("drafter", is_local=False)
        await pilot.pause()
        assert "drafter" not in skills_mod.list_skills(PROJECT_CODE)


# ═══ fold: test_tui_screens_skills_preship.py ═══
# Pre-ship regression: Skills tab DataTable must survive bracketed skill
# names/descriptions/capability-tags without a MarkupError at paint.
#
# Skill descriptions and capability tags are LLM/user-authored capability
# documentation and can carry bracket sequences like ``out[/idx]`` or
# ``[bold``. Textual's DataTable runs every ``str`` cell through
# ``Text.from_markup`` at paint time, which raises ``rich.errors.MarkupError``
# on an unmatched closing tag. The fix escapes each visible cell with
# ``rich.markup.escape``; the row *key* stays the raw name so the bind flow's
# ``coordinate_to_cell_key`` lookup still resolves the real skill.


# A name/description/capability each carrying a markup-closer that would
# explode Text.from_markup if left unescaped.
_BAD_NAME = "web[/idx] research"
_BAD_DESC = "summarize out[/idx] for each source [bold"
_BAD_CAP = "writing[/v2]"


class _Harness(App):
    def compose(self) -> ComposeResult:
        yield SkillsScreen()

    @property
    def project_code(self) -> str:  # SkillsScreen reads app.project_code
        return "demo"


def _fake_load(name, project_code=None):
    return skills.Skill(
        name=_BAD_NAME,
        description=_BAD_DESC,
        prompt_template="",
        capability_tags=(_BAD_CAP, "other"),
    )


@pytest.mark.asyncio
async def test_skills_table_survives_bracketed_cells(monkeypatch):
    monkeypatch.setattr(skills, "list_skills", lambda code: [_BAD_NAME])
    monkeypatch.setattr(skills, "load_with_metadata", _fake_load)

    app = _Harness()
    async with app.run_test() as pilot:
        screen = app.query_one(SkillsScreen)
        # Should not raise while populating the table with bracketed strings.
        screen._refresh()
        table = app.query_one("#skills-table", DataTable)
        assert table.row_count == 1
        # Row key must remain the RAW name so the bind-flow lookup resolves.
        assert list(table.rows.keys())[0].value == _BAD_NAME
        # Force a paint cycle — this is where the unescaped MarkupError fired.
        await pilot.pause()


def test_escape_prevents_markup_error_directly():
    """Tight unit guard independent of the Textual app lifecycle."""
    from rich.markup import escape
    from rich.text import Text

    with pytest.raises(MarkupError):
        Text.from_markup(_BAD_DESC)
    # Escaped form is inert.
    Text.from_markup(escape(_BAD_DESC))
