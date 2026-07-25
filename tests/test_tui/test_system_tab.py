# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""CONFIG → SYSTEM renders the engine's own state, and switches autonomy the
only way it can be switched: by submitting the command to converse."""
from __future__ import annotations

import pytest
from textual.widgets import Static

from modulatio import config, setup_state, vault
from modulatio.tui.app import ModulatioApp
from modulatio.tui.screens.system import SystemScreen

PROJECT_CODE = "SYS"


@pytest.fixture
def _isolate(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "config"
    monkeypatch.setattr(config, "CONFIG_DIR", cfg_dir)
    monkeypatch.setattr(config, "DEFAULTS_FILE", cfg_dir / "defaults.json")
    monkeypatch.setattr(setup_state, "SETUP_STATE_FILE",
                        cfg_dir / "setup-state.json")
    config.save_defaults({"vault_root": str(tmp_path / "vault")})
    config.reload()
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path / "vault")
    yield


def _text(widget: Static) -> str:
    rendered = widget.render()
    return rendered.plain if hasattr(rendered, "plain") else str(rendered)


@pytest.mark.asyncio
async def test_every_pane_renders_its_seam(_isolate):
    """All four read-outs paint: the mode pill with both status rows, the
    access card, the budget caps, and the diagnostics report."""
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()
        screen = app.query_one(SystemScreen)
        screen.refresh_all()
        await pilot.pause()

        autonomy = _text(screen.query_one("#system-autonomy", Static))
        assert "AUTONOMY" in autonomy
        # The substrate posture is reported independently of the mode, so a
        # permissive mode can never present as a confined one.
        assert "Access" in autonomy and "Sandbox" in autonomy
        assert "BUDGET" in _text(screen.query_one("#system-budget", Static))
        assert "ACCESS" in _text(screen.query_one("#system-access", Static))
        assert "DOCTOR" in _text(screen.query_one("#system-doctor", Static))


@pytest.mark.asyncio
async def test_one_failing_seam_does_not_blank_the_others(_isolate,
                                                          monkeypatch):
    """A pane whose seam raises reports itself unavailable; the rest still
    paint — a single unreadable surface is not a blank tab."""
    from modulatio import diagnostics

    monkeypatch.setattr(diagnostics, "collect",
                        lambda: (_ for _ in ()).throw(OSError("no report")))
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()
        screen = app.query_one(SystemScreen)
        screen.refresh_all()
        await pilot.pause()

        assert "unavailable" in _text(screen.query_one("#system-doctor", Static))
        assert "AUTONOMY" in _text(screen.query_one("#system-autonomy", Static))
        assert "BUDGET" in _text(screen.query_one("#system-budget", Static))


@pytest.mark.asyncio
async def test_mode_button_submits_the_command_to_converse(_isolate):
    """The mode is set by the operator's leading /-command in converse and
    nowhere else, so the button types that command into the console rather
    than writing the session mode directly."""
    from modulatio.tui.screens.prompt import PromptScreen
    from textual.widgets import Button, TabbedContent

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()
        prompt = app.query_one(PromptScreen)
        sent: list = []
        monkey = prompt._send_message

        def _capture():
            from modulatio.tui.widgets.chat_input import ChatInput
            sent.append(prompt.query_one("#prompt-input", ChatInput).text)

        prompt._send_message = _capture  # type: ignore[assignment]
        screen = app.query_one(SystemScreen)
        screen.query_one("#mode-yolo", Button).press()
        await pilot.pause()
        prompt._send_message = monkey  # type: ignore[assignment]

        assert sent == ["/yolo"]
        # The acknowledgement lands in the console, so that is where the
        # operator is taken.
        assert app.query_one("#app-tabs", TabbedContent).active == "tab-prompt"
