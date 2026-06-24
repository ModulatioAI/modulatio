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
        # Unified list: team entries appear as a LAYER=team row.
        table = app.query_one("#memory-table", DataTable)
        assert table.row_count == 1
        layer_cell = str(table.get_row_at(0)[0])
        assert "team" in layer_cell


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


# ─── Unified list + detail + delete (Feng-Tui MEMORY overhaul) ──────────────


@pytest.mark.asyncio
async def test_unified_list_tags_each_entry_with_its_layer():
    """Episodic, semantic, and team entries share ONE table, each tagged with
    its LAYER (glyph + word)."""
    from textual.widgets import DataTable, TabbedContent

    agent_memory.add_episodic("writer-a", "an episode", project_code=PROJECT_CODE)
    agent_memory.add_semantic("writer-a", "a durable fact", project_code=PROJECT_CODE)
    team_memory.write(writer_id="qc-1", writer_tier="qc", body="team note",
                      project_code=PROJECT_CODE, artifact_kind="report")
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        app.query_one(TabbedContent).active = "tab-memory"
        await pilot.pause()
        screen = app.query_one("MemoryScreen")
        screen.focus_agent("writer-a")
        await pilot.pause()
        table = app.query_one("#memory-table", DataTable)
        layers = {str(table.get_row_at(i)[0]).split()[-1]
                  for i in range(table.row_count)}
        assert {"episodic", "semantic", "team"} <= layers


@pytest.mark.asyncio
async def test_delete_removes_an_agent_entry():
    """Deleting an agent entry removes it from its layer."""
    from textual.widgets import TabbedContent

    e = agent_memory.add_episodic("writer-a", "to be deleted", project_code=PROJECT_CODE)
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        app.query_one(TabbedContent).active = "tab-memory"
        await pilot.pause()
        screen = app.query_one("MemoryScreen")
        screen.focus_agent("writer-a")
        await pilot.pause()
        screen._do_delete("episodic", "writer-a", e.id)
        await pilot.pause()
        assert agent_memory.get_episodic("writer-a", project_code=PROJECT_CODE) == []


@pytest.mark.asyncio
async def test_delete_refuses_team_entries():
    """Team entries are QC-curated — action_delete on a team row is a no-op."""
    from textual.widgets import DataTable, TabbedContent

    team_memory.write(writer_id="qc-1", writer_tier="qc", body="team stays",
                      project_code=PROJECT_CODE, artifact_kind="report")
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        app.query_one(TabbedContent).active = "tab-memory"
        await pilot.pause()
        screen = app.query_one("MemoryScreen")
        app.query_one("#memory-table", DataTable).move_cursor(row=0)  # team row
        screen.action_delete()
        await pilot.pause()
        assert team_memory.list_entries(PROJECT_CODE)  # untouched


@pytest.mark.asyncio
async def test_export_writes_markdown_for_the_focused_agent():
    from textual.widgets import TabbedContent

    agent_memory.add_semantic("writer-a", "exported fact", project_code=PROJECT_CODE)
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        app.query_one(TabbedContent).active = "tab-memory"
        await pilot.pause()
        screen = app.query_one("MemoryScreen")
        screen.focus_agent("writer-a")
        await pilot.pause()
        screen.action_export()
        await pilot.pause()
        dest = vault.project_dir(PROJECT_CODE) / "memory-writer-a.md"
        assert dest.exists()
        assert "exported fact" in dest.read_text()


@pytest.mark.asyncio
async def test_edit_updates_an_agent_entry_in_place():
    from textual.widgets import TabbedContent

    e = agent_memory.add_semantic("writer-a", "old fact", project_code=PROJECT_CODE)
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        app.query_one(TabbedContent).active = "tab-memory"
        await pilot.pause()
        screen = app.query_one("MemoryScreen")
        screen.focus_agent("writer-a")
        await pilot.pause()
        screen._do_edit("semantic", "writer-a", e.id, "new fact")
        await pilot.pause()
        sem = agent_memory.get_semantic("writer-a", project_code=PROJECT_CODE)
        assert [s.content for s in sem] == ["new fact"]


@pytest.mark.asyncio
async def test_add_appends_a_semantic_entry_for_the_agent():
    from textual.widgets import TabbedContent

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        app.query_one(TabbedContent).active = "tab-memory"
        await pilot.pause()
        screen = app.query_one("MemoryScreen")
        screen.focus_agent("writer-a")
        await pilot.pause()
        screen._do_add("writer-a", "a fresh operator note")
        await pilot.pause()
        sem = agent_memory.get_semantic("writer-a", project_code=PROJECT_CODE)
        assert any(s.content == "a fresh operator note" for s in sem)


@pytest.mark.asyncio
async def test_editing_a_team_entry_creates_a_proposal_not_a_mutation():
    from textual.widgets import TabbedContent

    from modulatio.memory import team_memory as tm

    tm.write(writer_id="qc-1", writer_tier="qc", body="original team fact",
             project_code=PROJECT_CODE, artifact_kind="report")
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        app.query_one(TabbedContent).active = "tab-memory"
        await pilot.pause()
        screen = app.query_one("MemoryScreen")
        screen._do_propose_revision("revised team fact")
        await pilot.pause()
        # the team entry is unchanged; a pending proposal now exists
        bodies = [e.body for e in tm.list_entries(PROJECT_CODE)]
        assert bodies == ["original team fact"]
        proposals = tm.list_proposals(PROJECT_CODE)
        assert any(p.body == "revised team fact" for p in proposals)


@pytest.mark.asyncio
async def test_memory_has_controls_row_and_search_filters(monkeypatch):
    """MEMORY carries the shared ControlsRow (counts + search) like every other
    list screen (Lovecraft cadre finding), and search filters the unified list."""
    from textual.widgets import DataTable, Static, TabbedContent

    from modulatio.tui.widgets.controls_row import ControlsRow

    vault.init_project(PROJECT_CODE, "x", "o")
    agent_memory.add_semantic("writer-a", "ozempic dosing note", project_code=PROJECT_CODE)
    agent_memory.add_semantic("writer-a", "unrelated build fact", project_code=PROJECT_CODE)
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        app.query_one(TabbedContent).active = "tab-memory"
        await pilot.pause()
        screen = app.query_one("MemoryScreen")
        screen.focus_agent("writer-a")
        await pilot.pause()
        assert screen.query_one(ControlsRow)  # the shared strip is present
        screen._query = "ozempic"
        screen._refresh_views()
        await pilot.pause()
        assert screen.query_one("#memory-table", DataTable).row_count == 1
        counts = str(screen.query_one("#controls-counts", Static).render())
        assert "filtered" in counts
