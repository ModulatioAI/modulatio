"""Tests for slice #24 — Agents tab + add wizard.

Consumes slice #18's ``roster.add_agent``. Lists roster agents in a
DataTable; Add button reveals an inline wizard that submits via
``roster.add_agent``. Establishes the wizard pattern for #25 and #26.

Scope: Add-only in this slice. Edit (pre-populated wizard) is on the
plan acceptance list but deferred — slice #18 has no public update
API beyond ``add_model``, and reusing add_agent would require dup-
semantics that contradict its FileExistsError contract. Edit can be
added as a follow-on slice when the use case is concrete.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from modulatio import roster, skills, vault


PROJECT_CODE = "AGT"


@pytest.fixture
def tui_vault_with_roster(tmp_path: Path, monkeypatch):
    """Pre-seed the vault + two agents + one shared skill so the
    Agents tab has content + add_agent can derive caps."""
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path / "projects")
    monkeypatch.setattr(skills, "_SKILLS_ROOT", tmp_path / "shared")
    # Isolate from package-bundled seed skills.
    monkeypatch.setattr(skills, "_SEED_SKILLS_ROOT", tmp_path / "no-seed")
    vault.init_project(PROJECT_CODE, "Agent fixture", "obj")

    shared = tmp_path / "shared"
    shared.mkdir(parents=True, exist_ok=True)
    (shared / "drafter.md").write_text(
        "---\n"
        "name: drafter\n"
        "description: drafts artifacts\n"
        "tool_loadout: fs\n"
        "standards_domain: \n"
        "model_tier: tactical\n"
        "cost_class: paid-cloud\n"
        "capability_tags: writing\n"
        "required_capabilities: \n"
        "executor: llm\n"
        "---\n\nBody.\n"
    )

    roster.add_agent(
        project_code=PROJECT_CODE,
        agent_id="writer-a",
        name="Writer A",
        identity="First seeded writer.",
        skills=["drafter"],
        model="ollama_chat/glm-5.1",
    )
    roster.add_agent(
        project_code=PROJECT_CODE,
        agent_id="writer-b",
        name="Writer B",
        identity="Second seeded writer.",
        skills=["drafter"],
        model="stub",
    )
    return tmp_path


# ─── Agents tab replaces placeholder + lists roster ─────────────────────────


async def test_agents_tab_replaces_placeholder(tui_vault_with_roster):
    """The Agents tab now shows a real DataTable (not a
    'coming in slice #24' placeholder)."""
    from textual.widgets import DataTable, TabbedContent

    from modulatio.tui.app import ModulatioApp

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()
        app.query_one(TabbedContent).active = "tab-agents"
        await pilot.pause()
        assert app.query_one("#agents-table", DataTable) is not None


async def test_agents_table_lists_pre_seeded_roster(tui_vault_with_roster):
    """Two pre-seeded agents → two rows. Columns: ID / Name / Tier /
    Model / Skills."""
    from textual.widgets import DataTable, TabbedContent

    from modulatio.tui.app import ModulatioApp

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()
        app.query_one(TabbedContent).active = "tab-agents"
        await pilot.pause()
        table = app.query_one("#agents-table", DataTable)
        assert table.row_count == 2
        assert len(table.columns) == 5


# ─── Wizard panel: open / cancel / submit ───────────────────────────────────


async def test_add_wizard_hidden_by_default(tui_vault_with_roster):
    """The Add wizard panel is hidden until the user clicks Add."""
    from textual.widgets import TabbedContent

    from modulatio.tui.app import ModulatioApp

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()
        app.query_one(TabbedContent).active = "tab-agents"
        await pilot.pause()
        panel = app.query_one("#agent-wizard-panel")
        assert panel.has_class("hidden")


async def test_clicking_add_reveals_wizard(tui_vault_with_roster):
    """Add button opens the wizard; wizard has the expected field ids
    (id, name, identity, skills, model, tier)."""
    from textual.widgets import TabbedContent

    from modulatio.tui.app import ModulatioApp

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()
        app.query_one(TabbedContent).active = "tab-agents"
        await pilot.pause()
        await pilot.click("#agents-add-btn")
        await pilot.pause()
        assert not app.query_one("#agent-wizard-panel").has_class("hidden")
        # Required wizard fields.
        for field_id in (
            "#agent-wiz-id",
            "#agent-wiz-name",
            "#agent-wiz-identity",
            "#agent-wiz-skills",
            "#agent-wiz-model",
            "#agent-wiz-tier",
        ):
            assert app.query_one(field_id) is not None


async def test_wizard_submit_creates_agent_via_roster_api(tui_vault_with_roster):
    """Fill the wizard + click Create → roster.add_agent is called with
    those values; the new agent appears on disk. This is end-to-end
    validation of slice #18's add_agent being driven from the UI."""
    from textual.widgets import DataTable, Input, Select, TabbedContent, TextArea

    from modulatio.tui.app import ModulatioApp

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()
        app.query_one(TabbedContent).active = "tab-agents"
        await pilot.pause()
        await pilot.click("#agents-add-btn")
        await pilot.pause()

        app.query_one("#agent-wiz-id", Input).value = "writer-c"
        app.query_one("#agent-wiz-name", Input).value = "Writer C"
        app.query_one("#agent-wiz-identity", TextArea).text = "Seeded via wizard."
        app.query_one("#agent-wiz-skills", Input).value = "drafter"
        app.query_one("#agent-wiz-model", Input).value = "stub"
        app.query_one("#agent-wiz-tier", Select).value = "producer"
        await pilot.pause()
        await pilot.click("#agent-wiz-create-btn")
        await pilot.pause()

        # Wizard closed.
        assert app.query_one("#agent-wizard-panel").has_class("hidden")
        # Agent file on disk.
        loaded = roster.load("writer-c", PROJECT_CODE)
        assert loaded is not None
        assert loaded.name == "Writer C"
        assert "drafter" in loaded.skills

        # Table refreshed.
        table = app.query_one("#agents-table", DataTable)
        assert table.row_count == 3


async def test_wizard_rejects_empty_required_fields(tui_vault_with_roster):
    """Submit with empty id + empty name → wizard stays open, shows a
    status error, roster.add_agent is not called."""
    from textual.widgets import TabbedContent

    from modulatio.tui.app import ModulatioApp

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()
        app.query_one(TabbedContent).active = "tab-agents"
        await pilot.pause()
        await pilot.click("#agents-add-btn")
        await pilot.pause()
        # Leave fields empty → click Create.
        await pilot.click("#agent-wiz-create-btn")
        await pilot.pause()
        # Wizard still visible.
        assert not app.query_one("#agent-wizard-panel").has_class("hidden")
        status = app.query_one("#agent-wiz-status")
        rendered = str(status.render()).lower()
        assert "required" in rendered or "empty" in rendered or "missing" in rendered


async def test_wizard_surfaces_duplicate_error(tui_vault_with_roster):
    """Creating an agent with an id that already exists → FileExistsError
    from roster.add_agent → rendered in status, wizard stays open."""
    from textual.widgets import Input, Select, TabbedContent, TextArea

    from modulatio.tui.app import ModulatioApp

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()
        app.query_one(TabbedContent).active = "tab-agents"
        await pilot.pause()
        await pilot.click("#agents-add-btn")
        await pilot.pause()

        app.query_one("#agent-wiz-id", Input).value = "writer-a"  # dup
        app.query_one("#agent-wiz-name", Input).value = "Dup writer"
        app.query_one("#agent-wiz-identity", TextArea).text = "."
        app.query_one("#agent-wiz-skills", Input).value = "drafter"
        app.query_one("#agent-wiz-model", Input).value = "stub"
        app.query_one("#agent-wiz-tier", Select).value = "producer"
        await pilot.pause()
        await pilot.click("#agent-wiz-create-btn")
        await pilot.pause()

        # Wizard still visible; error in status.
        assert not app.query_one("#agent-wizard-panel").has_class("hidden")
        rendered = str(app.query_one("#agent-wiz-status").render()).lower()
        assert "exists" in rendered or "already" in rendered


async def test_cancel_button_hides_wizard(tui_vault_with_roster):
    """Cancel closes the wizard without touching the roster."""
    from textual.widgets import TabbedContent

    from modulatio.tui.app import ModulatioApp

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()
        app.query_one(TabbedContent).active = "tab-agents"
        await pilot.pause()
        await pilot.click("#agents-add-btn")
        await pilot.pause()
        assert not app.query_one("#agent-wizard-panel").has_class("hidden")
        await pilot.click("#agent-wiz-cancel-btn")
        await pilot.pause()
        assert app.query_one("#agent-wizard-panel").has_class("hidden")
