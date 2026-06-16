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
