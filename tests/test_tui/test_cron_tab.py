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


# === Cron detail pane (Feng-Tui overhaul, Group B) ===========================


def test_format_cron_detail_jt_bound_shows_jt_and_params():
    """The detail card for a JT-bound job names the JT and lists its params."""
    from modulatio.tui.screens.cron import _format_cron_detail

    job = {
        "id": "cron-abc123", "name": "weekly digest", "project_code": "STA",
        "schedule": "weekly mon 09:00", "enabled": True,
        "next_run": "2026-07-01T09:00:00", "last_status": "ok",
        "last_run": "2026-06-24T09:00:00", "objective": "",
        "jt_id": "weekly-digest", "jt_params": {"topic": "AI", "depth": "deep"},
    }
    out = _format_cron_detail(job)
    assert "weekly-digest" in out          # the bound JT
    assert "topic" in out and "AI" in out   # a param key + value
    assert "depth" in out and "deep" in out


def test_format_cron_detail_objective_job_shows_objective_and_keys():
    """A raw-objective job shows its objective and the real action keys."""
    from modulatio.tui.screens.cron import _format_cron_detail

    job = {
        "id": "cron-xyz", "name": "nightly", "project_code": "STA",
        "schedule": "daily 02:00", "enabled": False, "next_run": "",
        "last_status": "failed", "last_run": "2026-06-23T02:00:00",
        "objective": "summarize the day's commits", "jt_id": None,
        "jt_params": None,
    }
    out = _format_cron_detail(job)
    assert "summarize the day's commits" in out
    # truthful affordance: the actual cron keybindings, not the mockup's.
    assert "e" in out and "enable" in out.lower()
    assert "x" in out and "remove" in out.lower()


@pytest.mark.asyncio
async def test_cron_detail_pane_renders_on_select():
    """Selecting a job populates the right detail pane with its objective."""
    from textual.widgets import Static, TabbedContent

    cron.add(name="nightly", schedule="daily 02:00", project_code="STA",
             objective="summarize commits")
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        tabbed = app.query_one(TabbedContent)
        tabbed.active = "tab-cron"
        await pilot.pause()
        from modulatio.tui.screens.cron import CronScreen
        screen = app.query_one(CronScreen)
        detail = screen.query_one("#cron-detail", Static)
        assert "summarize commits" in str(detail.render())
