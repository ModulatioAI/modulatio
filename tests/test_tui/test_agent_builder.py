"""Tests for the Configuration tab's AgentBuilderScreen (AGENTS side, slice 5)."""
from __future__ import annotations

import pytest
from textual.app import App, ComposeResult
from textual.widgets import DataTable, Input, OptionList, RadioSet

from modulatio import model_presets, roster, vault
from modulatio.tui.screens.agent_builder import AgentBuilderScreen

CODE = "AGT"


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    vault.init_project(CODE, "agent builder", "obj")
    roster.add_agent(project_code=CODE, agent_id="leader", name="Boss",
                     identity="leads", skills=[], model=None, tier="leader")
    roster.add_agent(project_code=CODE, agent_id="qc", name="Checker",
                     identity="qc", skills=[], model=None, tier="qc")
    roster.add_agent(project_code=CODE, agent_id="marlow", name="Marlow",
                     identity="prod", skills=[], model=None, tier="producer")
    monkeypatch.setattr(model_presets, "PRESETS_FILE", tmp_path / "presets.json")
    model_presets.add_preset(
        "op_free", label="OR Free", base_url="https://openrouter.ai/api/v1",
        api_format="openai", auth_type="api_key", model="openrouter/free",
        auth_config={"env_var": "OPENROUTER_API_KEY"},
    )
    return CODE


class _Host(App):
    def __init__(self, code: str) -> None:
        super().__init__()
        self.project_code = code

    def get_css_variables(self) -> dict[str, str]:
        v = super().get_css_variables()
        v.setdefault("frame", "#6cb6e4")
        v.setdefault("frame-dim", "#3f6d8c")
        return v

    def compose(self) -> ComposeResult:
        yield AgentBuilderScreen(id="ab")


async def test_lists_the_roster(project):
    app = _Host(project)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.query_one("#agt-table", DataTable).row_count == 3


async def test_change_model_assigns_a_preset(project):
    app = _Host(project)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.query_one(AgentBuilderScreen)
        await screen._show_change_model("marlow")
        await pilot.pause()
        ol = app.query_one("#agt-presets", OptionList)
        ol.focus()
        ol.highlighted = 0
        await pilot.press("enter")
        await pilot.pause()
        assert roster.load("marlow", project).model == "op_free"


async def test_add_producer_creates_an_agent(project):
    app = _Host(project)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.query_one(AgentBuilderScreen)
        await screen._show_add_agent()  # default role = Producer
        await pilot.pause()
        app.query_one("#agt-newname", Input).value = "Kurtz"
        ol = app.query_one("#agt-presets", OptionList)
        ol.focus()
        ol.highlighted = 0
        await pilot.press("enter")
        await pilot.pause()
        ids = {a.id for a in roster.list_agents(project)}
        assert "kurtz" in ids
        assert roster.load("kurtz", project).model == "op_free"


async def test_remove_producer(project):
    app = _Host(project)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.query_one(AgentBuilderScreen)
        table = app.query_one("#agt-table", DataTable)
        # roster lists sorted by id: leader, marlow, qc → marlow is row 1
        table.move_cursor(row=1)
        await pilot.pause()
        assert screen._selected_agent_id() == "marlow"
        await screen._remove_selected()
        await pilot.pause()
        assert "marlow" not in {a.id for a in roster.list_agents(project)}


async def test_remove_and_readd_the_leader(project):
    app = _Host(project)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.query_one(AgentBuilderScreen)
        table = app.query_one("#agt-table", DataTable)
        # roster sorted by id: leader, marlow, qc → leader is row 0
        table.move_cursor(row=0)
        await pilot.pause()
        assert screen._selected_agent_id() == "leader"
        await screen._remove_selected()  # Leader IS removable now
        await pilot.pause()
        assert roster.load("leader", project) is None

        # re-add a Leader via the role picker
        await screen._show_add_agent()
        await pilot.pause()
        app.query_one("#agt-newname", Input).value = "NewBoss"
        rs = app.query_one("#agt-role", RadioSet)
        rs.focus()
        await pilot.press("down")   # highlight "Leader" (index 1)
        await pilot.press("enter")  # select it
        await pilot.pause()
        ol = app.query_one("#agt-presets", OptionList)
        ol.focus()
        ol.highlighted = 0
        await pilot.press("enter")
        await pilot.pause()
        leader = roster.load("leader", project)
        assert leader is not None
        assert leader.tier == "leader"
        assert leader.name == "NewBoss"
