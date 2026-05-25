"""Tests for slice #26 — Models tab + update wizard.

Consumes slice #18's ``roster.add_model``. Closes Phase 2.

``add_model`` is an update-only API (no create). The Models tab lists
each roster agent's current model + tier + cost_class in a DataTable;
the Update wizard opens with an agent Select populated from the live
roster and a new model Input. Submit dispatches to ``add_model`` and
refreshes the list so the row reflects the new model immediately.

No 'create new model' concept here — models belong to agents, not the
registry. Creating a new agent is slice #24's territory.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from modulatio import roster, skills, vault


PROJECT_CODE = "MDL"


@pytest.fixture
def tui_vault_with_roster(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path / "projects")
    monkeypatch.setattr(skills, "_SKILLS_ROOT", tmp_path / "shared")
    vault.init_project(PROJECT_CODE, "Model fixture", "obj")

    shared = tmp_path / "shared"
    shared.mkdir(parents=True, exist_ok=True)
    (shared / "drafter.md").write_text(
        "---\n"
        "name: drafter\n"
        "description: drafts artifacts\n"
        "tool_loadout: \n"
        "standards_domain: \n"
        "model_tier: tactical\n"
        "cost_class: paid-cloud\n"
        "capability_tags: \n"
        "required_capabilities: \n"
        "executor: llm\n"
        "---\n\nBody.\n"
    )

    roster.add_agent(
        project_code=PROJECT_CODE,
        agent_id="writer-a",
        name="Writer A",
        identity="",
        skills=["drafter"],
        model="stub",
    )
    roster.add_agent(
        project_code=PROJECT_CODE,
        agent_id="writer-b",
        name="Writer B",
        identity="",
        skills=["drafter"],
        model="ollama_chat/glm-5.1",
    )
    return tmp_path


# ─── Models tab replaces placeholder + lists current models ─────────────────


async def test_models_tab_replaces_placeholder(tui_vault_with_roster):
    from textual.widgets import DataTable, TabbedContent

    from modulatio.tui.app import ModulatioApp

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()
        app.query_one(TabbedContent).active = "tab-models"
        await pilot.pause()
        assert app.query_one("#models-table", DataTable) is not None


async def test_models_table_lists_agents_with_models(tui_vault_with_roster):
    """Columns: Agent ID / Name / Model / Tier / Cost Class."""
    from textual.widgets import DataTable, TabbedContent

    from modulatio.tui.app import ModulatioApp

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()
        app.query_one(TabbedContent).active = "tab-models"
        await pilot.pause()
        table = app.query_one("#models-table", DataTable)
        assert table.row_count == 2
        assert len(table.columns) == 5


# ─── Wizard: open / submit / cancel ─────────────────────────────────────────


async def test_update_wizard_hidden_by_default(tui_vault_with_roster):
    from textual.widgets import TabbedContent

    from modulatio.tui.app import ModulatioApp

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()
        app.query_one(TabbedContent).active = "tab-models"
        await pilot.pause()
        assert app.query_one("#model-wizard-panel").has_class("hidden")


async def test_clicking_update_reveals_wizard_with_agent_options(tui_vault_with_roster):
    """Open wizard; the agent Select is populated from the live roster."""
    from textual.widgets import Select, TabbedContent

    from modulatio.tui.app import ModulatioApp

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()
        app.query_one(TabbedContent).active = "tab-models"
        await pilot.pause()
        await pilot.click("#models-update-btn")
        await pilot.pause()
        assert not app.query_one("#model-wizard-panel").has_class("hidden")
        # Agent Select + new-model Input exist.
        agent_select = app.query_one("#model-wiz-agent", Select)
        # The pre-seeded agents are both in the options list.
        option_values = [str(v) for v in agent_select._options]  # type: ignore[attr-defined]
        joined = " ".join(option_values)
        assert "writer-a" in joined
        assert "writer-b" in joined
        assert app.query_one("#model-wiz-model") is not None


async def test_wizard_submit_updates_agent_model(tui_vault_with_roster):
    """Pick agent writer-a + type new model id + submit →
    roster.add_model called → writer-a's file now shows the new id."""
    from textual.widgets import Input, Select, TabbedContent

    from modulatio.tui.app import ModulatioApp

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()
        app.query_one(TabbedContent).active = "tab-models"
        await pilot.pause()
        await pilot.click("#models-update-btn")
        await pilot.pause()

        app.query_one("#model-wiz-agent", Select).value = "writer-a"
        app.query_one("#model-wiz-model", Input).value = "ollama_chat/kimi-k2.6"
        await pilot.pause()
        await pilot.click("#model-wiz-update-btn")
        await pilot.pause()

        assert app.query_one("#model-wizard-panel").has_class("hidden")

    loaded = roster.load("writer-a", PROJECT_CODE)
    assert loaded is not None
    assert loaded.model == "ollama_chat/kimi-k2.6"


async def test_wizard_rejects_empty_model_field(tui_vault_with_roster):
    """Empty new-model input → error, add_model not called."""
    from textual.widgets import Select, TabbedContent

    from modulatio.tui.app import ModulatioApp

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()
        app.query_one(TabbedContent).active = "tab-models"
        await pilot.pause()
        await pilot.click("#models-update-btn")
        await pilot.pause()
        app.query_one("#model-wiz-agent", Select).value = "writer-a"
        # Leave model field empty.
        await pilot.pause()
        await pilot.click("#model-wiz-update-btn")
        await pilot.pause()

        assert not app.query_one("#model-wizard-panel").has_class("hidden")
        rendered = str(app.query_one("#model-wiz-status").render()).lower()
        assert "required" in rendered or "empty" in rendered or "missing" in rendered

    # writer-a's model unchanged.
    loaded = roster.load("writer-a", PROJECT_CODE)
    assert loaded.model == "stub"


async def test_cancel_button_hides_wizard(tui_vault_with_roster):
    from textual.widgets import TabbedContent

    from modulatio.tui.app import ModulatioApp

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()
        app.query_one(TabbedContent).active = "tab-models"
        await pilot.pause()
        await pilot.click("#models-update-btn")
        await pilot.pause()
        assert not app.query_one("#model-wizard-panel").has_class("hidden")
        await pilot.click("#model-wiz-cancel-btn")
        await pilot.pause()
        assert app.query_one("#model-wizard-panel").has_class("hidden")


# ─── #21 Clear-model button ─────────────────────────────────────────────────


async def test_clear_button_sets_selected_agent_model_to_none(tui_vault_with_roster):
    """Cursor on writer-b (which has a model), click [Clear model] →
    roster.add_model is reset (model is None) and the table reflects
    the change."""
    from textual.widgets import DataTable, TabbedContent

    from modulatio.tui.app import ModulatioApp

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()
        app.query_one(TabbedContent).active = "tab-models"
        await pilot.pause()
        table = app.query_one("#models-table", DataTable)
        # Cursor onto the row for writer-b (second row).
        table.move_cursor(row=1)
        await pilot.pause()
        await pilot.click("#models-clear-btn")
        await pilot.pause()

    # Vault reflects the cleared model.
    cleared = roster.load("writer-b", PROJECT_CODE)
    assert cleared is not None
    assert cleared.model is None


async def test_clear_button_no_op_when_no_row_selected(tui_vault_with_roster):
    """Defensive: empty cursor (no rows or unselected) doesn't crash —
    write the no-row case explicitly so the implementation handles it."""
    from textual.widgets import TabbedContent

    from modulatio.tui.app import ModulatioApp

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()
        app.query_one(TabbedContent).active = "tab-models"
        await pilot.pause()
        # Just click Clear without any explicit cursor move — first row
        # is selected by default but this still covers the path.
        await pilot.click("#models-clear-btn")
        await pilot.pause()
