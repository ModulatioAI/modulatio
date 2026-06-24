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


async def test_new_project_via_screen_creates_and_lists(isolated):
    """The [New] flow creates a marker-complete, team-seeded project that
    immediately appears in the list."""
    from modulatio import config, roster
    from modulatio.tui.screens.projects import ProjectsScreen

    config.save_defaults({"default_models": {"leader": "stub", "producer": "stub", "qc": "stub"}})
    config.reload()
    app = ModulatioApp(project_code="alpha", stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.query_one(ProjectsScreen)
        assert screen.query_one("#projects-new")  # the New button exists
        screen._on_new_project(("freshproj", "do work"))
        await pilot.pause()
        assert "freshproj" in vault.list_projects()
        assert roster.list_agents("freshproj")  # team seeded


async def test_new_project_duplicate_refused(isolated):
    """Creating a project whose folder already exists is refused — no crash,
    no change to the list."""
    from modulatio.tui.screens.projects import ProjectsScreen

    app = ModulatioApp(project_code="alpha", stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.query_one(ProjectsScreen)
        screen._on_new_project(("alpha", ""))  # already exists
        await pilot.pause()
        assert sorted(vault.list_projects()) == ["alpha", "beta"]


async def test_new_action_drives_modal_to_create(isolated):
    """Wiring: action_new opens the modal, and the modal's (code, objective)
    dismissal flows into _on_new_project → create_project. Proves the tuple
    contract between the modal and the handler, not just each part alone."""
    from modulatio import config
    from modulatio.tui.screens.projects import NewProjectModal, ProjectsScreen
    from textual.widgets import Input

    config.save_defaults({"default_models": {"leader": "stub", "producer": "stub", "qc": "stub"}})
    config.reload()
    app = ModulatioApp(project_code="alpha", stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.query_one(ProjectsScreen)
        screen.action_new()
        await pilot.pause()
        modal = app.screen  # the pushed modal is a separate screen on the stack
        assert isinstance(modal, NewProjectModal)
        modal.query_one("#newproj-code", Input).value = "wired"
        modal.query_one("#newproj-objective", Input).value = "via the modal"
        modal._submit()  # → dismiss((code, objective)) → _on_new_project → create
        await pilot.pause()
        assert "wired" in vault.list_projects()
        assert "via the modal" in (vault.project_dir("wired") / "index.md").read_text()


async def test_new_project_unexpected_error_is_caught(isolated, monkeypatch):
    """An unexpected create failure (OSError / permission / malformed config)
    is caught and notified, never crashes the screen with a raw traceback."""
    from modulatio import roster
    from modulatio.tui.screens.projects import ProjectsScreen

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(roster, "create_project", boom)
    app = ModulatioApp(project_code="alpha", stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.query_one(ProjectsScreen)
        screen._on_new_project(("newp", ""))  # must not raise
        await pilot.pause()
        assert "newp" not in vault.list_projects()


async def test_n_keybinding_opens_new_modal(isolated):
    """The 'n' key (binding wiring) on the focused projects table opens the
    New modal — the wiring test only covers the button path."""
    from textual.widgets import DataTable
    from textual.widgets import TabbedContent

    from modulatio.tui.screens.projects import NewProjectModal, ProjectsScreen

    app = ModulatioApp(project_code="alpha", stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one("#app-tabs", TabbedContent).active = "tab-config"
        app.query_one("#config-flip", TabbedContent).active = "config-projects"
        await pilot.pause()
        app.query_one(ProjectsScreen).query_one("#projects-table", DataTable).focus()
        await pilot.pause()
        await pilot.press("n")
        await pilot.pause()
        assert isinstance(app.screen, NewProjectModal)
