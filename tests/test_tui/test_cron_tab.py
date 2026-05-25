"""Slice 7: Cron tab + /cron command tests."""

from __future__ import annotations

import pytest

from modulatio import config, cron, setup_state, vault
from modulatio.tui import commands as cmd_mod
from modulatio.tui.app import ModulatioApp


PROJECT_CODE = "CRTAB"


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


# === /cron command (was deferred, now active in slice 7) ===

def test_cron_command_returns_switch_tab_side_effect():
    result = cmd_mod.dispatch("/cron")
    assert result.ok is True
    assert result.side_effect == "switch_tab:cron"


def test_cron_command_with_project_filter_arg():
    result = cmd_mod.dispatch("/cron STA")
    assert result.ok is True
    assert result.side_effect == "switch_tab:cron:STA"


def test_cron_no_longer_in_deferred_category():
    cmds = cmd_mod.list_commands()
    cron_cmd = next(c for c in cmds if c.shortcut == "/cron")
    assert cron_cmd.category != "Deferred"


# === Cron tab present ===

@pytest.mark.asyncio
async def test_cron_tab_present_in_tabbed_content():
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        from textual.widgets import TabbedContent
        tabbed = app.query_one(TabbedContent)
        tabbed.active = "tab-cron"
        await pilot.pause()
        assert tabbed.active == "tab-cron"


@pytest.mark.asyncio
async def test_cron_tab_lists_jobs():
    cron.add(name="weekly-report", schedule="weekly mon 09:00", project_code="STA", objective="x")
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        from textual.widgets import TabbedContent, DataTable
        tabbed = app.query_one(TabbedContent)
        tabbed.active = "tab-cron"
        await pilot.pause()
        table = app.query_one("#cron-table", DataTable)
        assert table.row_count == 1


@pytest.mark.asyncio
async def test_cron_command_routes_to_tab():
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        from textual.widgets import TabbedContent
        app._handle_slash_command("/cron")
        await pilot.pause()
        tabbed = app.query_one(TabbedContent)
        assert tabbed.active == "tab-cron"
