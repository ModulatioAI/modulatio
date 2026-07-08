# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Cowboy Claude and Clifton Knox.
"""CONFIG → SETTINGS — the Install-WebOS button. One click installs the opt-in
`[web]` extra via the shared helper; the label reflects installed state.
"""

from __future__ import annotations

import pytest
from textual.widgets import Button

from modulatio import config, setup_state, vault
from modulatio.tui.app import ModulatioApp
from modulatio.web import install

PROJECT_CODE = "SETWEB"

_BTN = "#settings-install-webos"


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


async def _open_settings(app, pilot):
    from textual.widgets import TabbedContent

    app.query_one("#app-tabs", TabbedContent).active = "tab-config"
    app.query_one("#config-flip", TabbedContent).active = "config-settings"
    await pilot.pause()
    from modulatio.tui.screens.settings import SettingsScreen

    return app.query_one(SettingsScreen)


@pytest.mark.asyncio
async def test_button_shows_installed_state(monkeypatch):
    monkeypatch.setattr(install, "is_installed", lambda: True)
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await _open_settings(app, pilot)
        btn = app.query_one(_BTN, Button)
        assert "installed" in str(btn.label).lower()
        assert btn.disabled


@pytest.mark.asyncio
async def test_button_offers_install_when_absent(monkeypatch):
    monkeypatch.setattr(install, "is_installed", lambda: False)
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await _open_settings(app, pilot)
        btn = app.query_one(_BTN, Button)
        assert "install" in str(btn.label).lower()
        assert not btn.disabled


@pytest.mark.asyncio
async def test_install_done_notifies_and_relabels(monkeypatch):
    """The post-install callback notifies the outcome and refreshes the button
    to its now-installed state."""
    monkeypatch.setattr(install, "is_installed", lambda: False)
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        screen = await _open_settings(app, pilot)
        notes: list[str] = []
        monkeypatch.setattr(app, "notify", lambda msg, **k: notes.append(msg))
        monkeypatch.setattr(install, "is_installed", lambda: True)
        screen._webos_install_done(True, "Modulatio WebOS installed")
        await pilot.pause()
        assert any("installed" in n.lower() for n in notes)
        btn = app.query_one(_BTN, Button)
        assert btn.disabled


@pytest.mark.asyncio
async def test_install_failure_notifies_manual_command(monkeypatch):
    monkeypatch.setattr(install, "is_installed", lambda: False)
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        screen = await _open_settings(app, pilot)
        notes: list[str] = []
        monkeypatch.setattr(app, "notify", lambda msg, **k: notes.append(msg))
        screen._webos_install_done(False, "boom — install manually")
        await pilot.pause()
        assert any("manual" in n.lower() for n in notes)
