# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""CONFIG → SETTINGS tab (Feng-Tui refinement arc, W6).

Operator-adjustable engine knobs — per-role context windows, task/goal retry
budgets ("loops before QC fixes" / "leader verify attempts"), QC-fixer and
codification toggles — persisted as defaults.json["env_overrides"] and pushed
into os.environ via config.apply_env_overrides (shell/.env keys stay
read-only-honest).
"""

from __future__ import annotations

import os

import pytest

from modulatio import config, setup_state, vault
from modulatio.tui.app import ModulatioApp

PROJECT_CODE = "SETTAB"


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "config"
    monkeypatch.setattr(config, "CONFIG_DIR", cfg_dir)
    monkeypatch.setattr(config, "DEFAULTS_FILE", cfg_dir / "defaults.json")
    monkeypatch.setattr(setup_state, "SETUP_STATE_FILE", cfg_dir / "setup-state.json")
    config.save_defaults({"vault_root": str(tmp_path / "vault")})
    config.reload()
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path / "vault")
    monkeypatch.setattr(config, "_ENV_OVERRIDES_SET", set())
    for k in ("MODULATIO_TASK_MAX_RETRIES", "MODULATIO_CTX_BUDGET_QC"):
        monkeypatch.delenv(k, raising=False)
    yield


async def _open_settings(app, pilot):
    from textual.widgets import TabbedContent
    app.query_one("#app-tabs", TabbedContent).active = "tab-config"
    app.query_one("#config-flip", TabbedContent).active = "config-settings"
    await pilot.pause()
    from modulatio.tui.screens.settings import SettingsScreen
    return app.query_one(SettingsScreen)


@pytest.mark.asyncio
async def test_settings_tab_lists_knobs_with_sources():
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        screen = await _open_settings(app, pilot)
        keys = list(screen._by_row.values())
        assert "MODULATIO_TASK_MAX_RETRIES" in keys
        assert "MODULATIO_CTX_BUDGET_QC" in keys
        assert "MODULATIO_QC_FIXER" in keys


@pytest.mark.asyncio
async def test_save_persists_and_applies_live():
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        screen = await _open_settings(app, pilot)
        ok = screen._save_value("MODULATIO_TASK_MAX_RETRIES", "1")
        await pilot.pause()
        assert ok
        assert config._load_defaults()["env_overrides"][
            "MODULATIO_TASK_MAX_RETRIES"] == "1"
        assert os.environ["MODULATIO_TASK_MAX_RETRIES"] == "1"


@pytest.mark.asyncio
async def test_validation_refuses_out_of_range():
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        screen = await _open_settings(app, pilot)
        assert not screen._save_value("MODULATIO_TASK_MAX_RETRIES", "99")
        assert not screen._save_value("MODULATIO_CTX_BUDGET_QC", "999999")
        assert not screen._save_value("MODULATIO_TASK_CONTEXT_CAP_PCT", "1.5")
        assert "env_overrides" not in config._load_defaults() or not \
            config._load_defaults()["env_overrides"]


@pytest.mark.asyncio
async def test_clear_override_restores_default():
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        screen = await _open_settings(app, pilot)
        assert screen._save_value("MODULATIO_CTX_BUDGET_QC", "48000")
        assert os.environ["MODULATIO_CTX_BUDGET_QC"] == "48000"
        screen._clear_value("MODULATIO_CTX_BUDGET_QC")
        await pilot.pause()
        assert "MODULATIO_CTX_BUDGET_QC" not in os.environ
        assert "MODULATIO_CTX_BUDGET_QC" not in (
            config._load_defaults().get("env_overrides") or {})


@pytest.mark.asyncio
async def test_shell_owned_key_is_read_only(monkeypatch):
    monkeypatch.setenv("MODULATIO_QC_FIXER", "0")  # shell export
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        screen = await _open_settings(app, pilot)
        assert screen._source_of("MODULATIO_QC_FIXER") == "shell/.env"
        assert not screen._save_value("MODULATIO_QC_FIXER", "1")
        assert os.environ["MODULATIO_QC_FIXER"] == "0"
