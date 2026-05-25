"""Slice 5: Memory tab tests.

Covers MemoryScreen state handling + read-only inspection of agent_memory
+ team_memory. Uses Textual's run_test fixture.
"""

from __future__ import annotations

import pytest

from modulatio import config, setup_state, vault
from modulatio.memory import agent_memory, team_memory
from modulatio.tui.app import ModulatioApp


PROJECT_CODE = "MEMTAB"


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "config"
    monkeypatch.setattr(config, "CONFIG_DIR", cfg_dir)
    monkeypatch.setattr(config, "DEFAULTS_FILE", cfg_dir / "defaults.json")
    monkeypatch.setattr(setup_state, "SETUP_STATE_FILE", cfg_dir / "setup-state.json")
    config.save_defaults({
        "vault_root": str(tmp_path / "vault"),
        "cache_root": str(tmp_path / "cache"),
    })
    config.reload()
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path / "vault")
    yield


@pytest.mark.asyncio
async def test_memory_tab_present_in_tabbed_content():
    """MemoryScreen should be one of the tab panes after Phase 2.5 slice 5."""
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        from textual.widgets import TabbedContent
        tabbed = app.query_one(TabbedContent)
        # Just check the memory tab id is reachable
        tabbed.active = "tab-memory"
        await pilot.pause()
        assert tabbed.active == "tab-memory"


@pytest.mark.asyncio
async def test_memory_tab_team_only_default_shows_team_entries():
    """With no agent focus, team-memory entries still show."""
    team_memory.write(
        writer_id="qc-1", writer_tier="qc",
        body="Test team-memory entry for tab.",
        project_code=PROJECT_CODE,
        artifact_kind="report",
    )
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        from textual.widgets import TabbedContent, DataTable
        tabbed = app.query_one(TabbedContent)
        tabbed.active = "tab-memory"
        await pilot.pause()
        team = app.query_one("#memory-team-table", DataTable)
        assert team.row_count == 1


@pytest.mark.asyncio
async def test_memory_tab_focus_agent_via_slash_command():
    """`/memory writer-a` should switch to memory tab + focus the agent."""
    agent_memory.add_episodic(
        "writer-a", "noted: project x ships Friday",
        project_code=PROJECT_CODE,
    )
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        from textual.widgets import TabbedContent
        # Direct handler invocation — exercises the slash-command routing
        # without relying on click-event timing.
        app._handle_slash_command("/memory writer-a")
        await pilot.pause()
        tabbed = app.query_one(TabbedContent)
        assert tabbed.active == "tab-memory"


@pytest.mark.asyncio
async def test_first_launch_banner_when_setup_state_missing(tmp_path):
    """When ~/.config/modulatio/setup-state.json is absent, the response area
    surfaces a one-time banner pointing at `modulatio setup`."""
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()
        # The on_mount banner should land in last_summary_text
        assert "modulatio setup" in app.last_summary_text


@pytest.mark.asyncio
async def test_first_launch_banner_absent_when_setup_completed(tmp_path):
    """When setup_state exists, no banner."""
    from modulatio import setup_state
    setup_state.mark_completed(version="2.0.0")
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()
        # Either empty or a non-banner string
        assert "First-launch" not in app.last_summary_text
