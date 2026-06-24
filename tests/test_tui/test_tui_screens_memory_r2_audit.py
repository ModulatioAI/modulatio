"""R2 audit regression: Memory tab must not crash (MarkupError) on agent/team
memory content containing rich-markup bracket sequences like ``[/]`` or ``x[/2]``.

Textual's DataTable.default_cell_formatter feeds raw ``str`` cells through
``Text.from_markup``, which raises ``MarkupError`` on closing-tag-like bracket
sequences. Memory bodies/content are arbitrary agent/LLM-authored text that
routinely contains such brackets (code, regex, artifact excerpts), so the
Memory tab must wrap cells in Rich ``Text`` to render them verbatim.

Ledger: 2026-06-13 Cowboy-Opus-fulldebug-r2 — memory.py:230 (MEDIUM/error-path).
"""

from __future__ import annotations

import pytest
from rich.text import Text

from modulatio import config, setup_state, vault
from modulatio.memory import agent_memory, team_memory
from modulatio.tui.app import ModulatioApp
from modulatio.tui.screens.memory import _cell


PROJECT_CODE = "MEMR2"

# Bracket sequences that crash rich's Text.from_markup if left as raw str cells.
BRACKET_BODY = "fixed: arr[/idx]=1 and x[/2] in the patch"


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


def test_cell_wraps_bracket_text_without_markup_error():
    """The cell helper must yield a Rich Text (renderable, markup-bypassing),
    NOT a raw str — Text instances skip default_cell_formatter's from_markup."""
    from textual.widgets._data_table import default_cell_formatter

    cell = _cell(BRACKET_BODY)
    assert isinstance(cell, Text)
    # default_cell_formatter returns a Text obj verbatim for a renderable, and
    # MUST NOT raise MarkupError (which it would for the equivalent raw str).
    rendered = default_cell_formatter(cell)
    assert isinstance(rendered, Text)
    assert "arr[/idx]=1" in rendered.plain
    # Prove the raw-str path is the one that would crash (guards the fix's value).
    with pytest.raises(Exception):
        Text.from_markup(BRACKET_BODY)


@pytest.mark.asyncio
async def test_memory_tab_renders_team_entry_with_bracket_markup():
    """Opening the Memory tab with bracket-bearing team-memory must not crash."""
    team_memory.write(
        writer_id="qc-1", writer_tier="qc",
        body=BRACKET_BODY,
        project_code=PROJECT_CODE,
        artifact_kind="code",
    )
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        from textual.widgets import TabbedContent, DataTable
        tabbed = app.query_one(TabbedContent)
        tabbed.active = "tab-memory"
        await pilot.pause()
        from textual.widgets._data_table import default_cell_formatter

        table = app.query_one("#memory-table", DataTable)
        assert table.row_count == 1
        # Force a render pass over the cells — this is where from_markup would
        # have crashed for a raw str cell.
        for row in range(table.row_count):
            for cell in table.get_row_at(row):
                default_cell_formatter(cell)


@pytest.mark.asyncio
async def test_memory_tab_renders_agent_episodic_semantic_with_bracket_markup():
    """Per-agent episodic + semantic tables must render bracket content."""
    agent_memory.add_episodic(
        "writer-a", BRACKET_BODY,
        project_code=PROJECT_CODE, source="chat",
    )
    agent_memory.add_semantic(
        "writer-a", "learned: regex token[/group] survives render",
        project_code=PROJECT_CODE,
    )
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        from textual.widgets import DataTable
        from textual.widgets._data_table import default_cell_formatter

        screen = app.query_one("MemoryScreen")
        screen.focus_agent("writer-a")
        await pilot.pause()

        # Unified list holds the agent's episodic + semantic rows (+ any team).
        table = app.query_one("#memory-table", DataTable)
        assert table.row_count >= 2
        for row in range(table.row_count):
            for cell in table.get_row_at(row):
                default_cell_formatter(cell)
