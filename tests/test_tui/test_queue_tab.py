"""Slice 6: Queue tab + /queue command tests."""

from __future__ import annotations

import pytest

from modulatio import config, heartbeat, setup_state, vault
from modulatio.tui import commands as cmd_mod
from modulatio.tui.app import ModulatioApp


PROJECT_CODE = "QTAB"


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "config"
    monkeypatch.setattr(config, "CONFIG_DIR", cfg_dir)
    monkeypatch.setattr(config, "DEFAULTS_FILE", cfg_dir / "defaults.json")
    monkeypatch.setattr(setup_state, "SETUP_STATE_FILE", cfg_dir / "setup-state.json")
    config.save_defaults({"vault_root": str(tmp_path / "vault")})
    config.reload()
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path / "vault")
    yield


# === /queue command (was deferred, now active in slice 6) ===

def test_queue_command_returns_switch_tab_side_effect():
    result = cmd_mod.dispatch("/queue")
    assert result.ok is True
    assert result.side_effect == "switch_tab:queue"


def test_queue_command_with_status_filter_arg():
    result = cmd_mod.dispatch("/queue pending")
    assert result.ok is True
    assert result.side_effect == "switch_tab:queue:pending"


def test_queue_no_longer_in_deferred_category():
    """Slice 6 promoted /queue out of the Deferred category."""
    cmds = cmd_mod.list_commands()
    queue_cmd = next(c for c in cmds if c.shortcut == "/queue")
    assert queue_cmd.category != "Deferred"


# === Queue tab present in TabbedContent ===

@pytest.mark.asyncio
async def test_queue_tab_present_in_tabbed_content():
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        from textual.widgets import TabbedContent
        tabbed = app.query_one(TabbedContent)
        tabbed.active = "tab-queue"
        await pilot.pause()
        assert tabbed.active == "tab-queue"


@pytest.mark.asyncio
async def test_queue_tab_lists_pending_tasks():
    heartbeat.add_task(description="visible task", project_code=PROJECT_CODE, objective="x")
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        from textual.widgets import TabbedContent, DataTable
        tabbed = app.query_one(TabbedContent)
        tabbed.active = "tab-queue"
        await pilot.pause()
        table = app.query_one("#queue-table", DataTable)
        assert table.row_count == 1


@pytest.mark.asyncio
async def test_queue_command_routes_to_queue_tab():
    """`/queue` typed in the prompt input switches to the Queue tab."""
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        from textual.widgets import TabbedContent
        app._handle_slash_command("/queue")
        await pilot.pause()
        tabbed = app.query_one(TabbedContent)
        assert tabbed.active == "tab-queue"
