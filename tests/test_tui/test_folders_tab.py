"""The FOLDERS config tab — register named operator folders for job runs.

Add/edit/remove named folder records (name + path + mode), pick the
job-output folder, surface reachability. Validation is defense-in-depth
with config.folder_grant_roots: the tab refuses at ADD time what the
grant classifier would drop at USE time (dangerous roots, relative
paths), plus registry-shape rules (duplicate names/paths).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from modulatio import config, vault
from modulatio.tui.app import ModulatioApp


@pytest.fixture
def isolated(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path / "vault")
    cfg = tmp_path / "config"
    monkeypatch.setattr(config, "CONFIG_DIR", cfg)
    monkeypatch.setattr(config, "DEFAULTS_FILE", cfg / "defaults.json")
    config.reload()
    vault.init_project("alpha", "Alpha", "x")
    return tmp_path


def _mk(tmp_path, name):
    d = tmp_path / name
    d.mkdir(exist_ok=True)
    return d


async def _folders_screen(app, pilot):
    from modulatio.tui.screens.folders import FoldersScreen

    app.query_one("#app-tabs").active = "tab-config"
    await pilot.pause()
    app.query_one("#config-flip").active = "config-folders"
    await pilot.pause()
    return app.query_one(FoldersScreen)


async def test_folders_tab_mounts_under_config(isolated):
    app = ModulatioApp(project_code="alpha", stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()
        screen = await _folders_screen(app, pilot)
        assert screen is not None


async def test_add_folder_persists_and_defaults_name(isolated, tmp_path):
    docs = _mk(tmp_path, "contracts")
    app = ModulatioApp(project_code="alpha", stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()
        screen = await _folders_screen(app, pilot)
        # Explicit name.
        screen._on_new_folder(("docs", str(docs), "ro"))
        # Empty name → defaults to the path basename.
        drop = _mk(tmp_path, "deliver-here")
        screen._on_new_folder(("", str(drop), "output"))
        await pilot.pause()

    recs = {r["name"]: r for r in config.list_folders()}
    assert recs["docs"]["path"] == str(docs) and recs["docs"]["mode"] == "ro"
    assert "deliver-here" in recs and recs["deliver-here"]["mode"] == "output"


async def test_add_refusals(isolated, tmp_path):
    """Relative path, dangerous root, duplicate name, duplicate path — all
    refused with a visible reason; the registry stays unchanged."""
    docs = _mk(tmp_path, "docs")
    app = ModulatioApp(project_code="alpha", stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()
        screen = await _folders_screen(app, pilot)
        screen._on_new_folder(("docs", str(docs), "ro"))

        screen._on_new_folder(("rel", "relative/dir", "ro"))
        screen._on_new_folder(("etc", "/etc", "ro"))
        screen._on_new_folder(("DOCS", str(_mk(tmp_path, "other")), "ro"))  # dup name (ci)
        screen._on_new_folder(("again", str(docs), "ro"))                   # dup path
        await pilot.pause()

    assert [r["name"] for r in config.list_folders()] == ["docs"]


async def test_output_pick_only_for_output_mode(isolated, tmp_path):
    docs = _mk(tmp_path, "docs")
    drop = _mk(tmp_path, "drop")
    app = ModulatioApp(project_code="alpha", stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()
        screen = await _folders_screen(app, pilot)
        screen._on_new_folder(("docs", str(docs), "ro"))
        screen._on_new_folder(("drop", str(drop), "output"))

        screen._set_output("docs")   # ro → refused
        assert config.get_job_output_folder() is None
        screen._set_output("drop")   # output → picked
        assert config.get_job_output_folder() == "drop"
        screen._set_output("drop")   # picking again → toggles off
        assert config.get_job_output_folder() is None


async def test_delete_clears_the_output_pick(isolated, tmp_path):
    drop = _mk(tmp_path, "drop")
    app = ModulatioApp(project_code="alpha", stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()
        screen = await _folders_screen(app, pilot)
        screen._on_new_folder(("drop", str(drop), "output"))
        screen._set_output("drop")
        assert config.get_job_output_folder() == "drop"

        screen._do_delete("drop")
        await pilot.pause()

    assert config.list_folders() == []
    assert config.get_job_output_folder() is None


async def test_unreachable_folder_shows_status(isolated, tmp_path, monkeypatch):
    docs = _mk(tmp_path, "docs")
    app = ModulatioApp(project_code="alpha", stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()
        screen = await _folders_screen(app, pilot)
        screen._on_new_folder(("docs", str(docs), "ro"))
        monkeypatch.setattr(config, "probe_folder", lambda *a, **k: False)
        statuses = screen._probe_statuses()
        assert statuses == {"docs": False}
        screen._apply_statuses(statuses)
        await pilot.pause()
        from textual.widgets import DataTable

        table = screen.query_one("#folders-table", DataTable)
        row = [str(c) for c in table.get_row("docs")]
        assert any("unreachable" in c for c in row)
