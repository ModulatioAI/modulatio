"""Tests for project switching + the PROJECTS config tab (roadmap #7).

Switching is a live, idle-only rebind on the app; the PROJECTS tab lists
projects and offers switch/delete. Delete is guarded against accidental loss
(active project + job-in-flight refused; the marker check lives in
``backup.delete_project`` / ``vault._is_project_dir``).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from modulatio import config, vault
from modulatio.tui.app import ModulatioApp


@pytest.fixture
def isolated(tmp_path: Path, monkeypatch):
    """Sandbox vault + config so switch/persist never touch the real install."""
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path / "vault")
    cfg = tmp_path / "config"
    monkeypatch.setattr(config, "CONFIG_DIR", cfg)
    monkeypatch.setattr(config, "DEFAULTS_FILE", cfg / "defaults.json")
    config.reload()
    vault.init_project("alpha", "Alpha", "x")
    vault.init_project("beta", "Beta", "y")
    return tmp_path


async def test_switch_project_idle_rebinds_and_persists(isolated):
    app = ModulatioApp(project_code="alpha", stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        ok = app.switch_project("beta")
        assert ok is True
        assert app.project_code == "beta"
        assert app._project is None  # memo invalidated → rebuilds for beta
        assert config.get_default_project_code() == "beta"
        assert "BETA" in app.sub_title


async def test_switch_project_refused_while_job_in_flight(isolated, monkeypatch):
    app = ModulatioApp(project_code="alpha", stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        monkeypatch.setattr(app, "_any_job_in_flight", lambda: True)
        ok = app.switch_project("beta")
        assert ok is False
        assert app.project_code == "alpha"  # unchanged under a running job


async def test_switch_project_unknown_code_no_rebind(isolated):
    app = ModulatioApp(project_code="alpha", stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        ok = app.switch_project("ghost")
        assert ok is False
        assert app.project_code == "alpha"


# ── PROJECTS tab (screen) ───────────────────────────────────────────────


def test_project_command_opens_the_tab():
    """`/project` resolves to the open_projects side-effect (CONFIG → PROJECTS)."""
    from modulatio.tui import commands

    result = commands.dispatch("/project")
    assert result.side_effect == "open_projects"


async def test_projects_tab_lists_projects(isolated):
    from modulatio.tui.screens.projects import ProjectsScreen
    from textual.widgets import DataTable

    app = ModulatioApp(project_code="alpha", stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.query_one(ProjectsScreen)
        table = screen.query_one("#projects-table", DataTable)
        codes = {table.coordinate_to_cell_key((r, 0)).row_key.value for r in range(table.row_count)}
        assert codes == {"alpha", "beta"}


async def test_delete_refused_for_active_project(isolated):
    """[Delete] on the active project never even opens the confirm modal."""
    from modulatio.tui.screens.projects import ProjectsScreen
    from textual.widgets import DataTable

    app = ModulatioApp(project_code="alpha", stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.query_one(ProjectsScreen)
        table = screen.query_one("#projects-table", DataTable)
        table.move_cursor(row=0)  # row 0 = alpha = active (sorted)
        stack_before = len(app.screen_stack)
        screen.action_delete()
        await pilot.pause()
        assert len(app.screen_stack) == stack_before  # no ConfirmModal pushed
        assert vault.project_dir("alpha").exists()


async def test_delete_refused_while_job_in_flight(isolated, monkeypatch):
    from modulatio.tui.screens.projects import ProjectsScreen
    from textual.widgets import DataTable

    app = ModulatioApp(project_code="alpha", stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        monkeypatch.setattr(app, "_any_job_in_flight", lambda: True)
        screen = app.query_one(ProjectsScreen)
        table = screen.query_one("#projects-table", DataTable)
        table.move_cursor(row=1)  # beta (non-active)
        stack_before = len(app.screen_stack)
        screen.action_delete()
        await pilot.pause()
        assert len(app.screen_stack) == stack_before  # refused before the modal
        assert vault.project_dir("beta").exists()
