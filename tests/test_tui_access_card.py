"""The /access surface: the LIVE effective-capability card reaches the
operator through a real command, rendered from the runtime snapshot — mode,
gate/broker grants including the in-flight once-slate, the active loadout,
and the substrate — through the one shared renderer.
"""
from __future__ import annotations

import pytest

from modulatio import config, setup_state, vault
from modulatio.tui.app import ModulatioApp

PROJECT_CODE = "ACCTUI"


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


def test_access_command_dispatches_show_access_card():
    from modulatio.tui import commands

    result = commands.dispatch("/access")
    assert result.handled and result.ok
    assert result.side_effect == "show_access_card"


@pytest.mark.asyncio
async def test_access_side_effect_renders_live_card_with_session_state():
    """Drive the OPERATOR surface (the side-effect dispatcher), not the
    orchestrator helper: the rendered card carries the current mode plus
    live gate state — including a once grant as 'Allowed this call'."""
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()
        orch = app._conversation_orchestrator()
        assert orch is not None
        gate = orch.leader_gate()
        gate._session.setdefault("path", []).append(
            {"resource": "/tmp/session-root", "actions": ["read"]})
        gate._once.setdefault("path", []).append("/tmp/once-root")
        rendered: list[str] = []
        app._set_response = lambda text: rendered.append(text)  # type: ignore

        app._apply_side_effect("show_access_card")
        await pilot.pause()

        assert rendered, "the surface must render the card"
        card = rendered[0]
        assert "Allowed this call" in card       # the live once-slate
        assert "/tmp/once-root" in card
        assert "/tmp/session-root" in card
        assert "Mode" in card or "mode" in card  # the current autonomy mode


@pytest.mark.asyncio
async def test_access_without_conversation_gives_configured_hint():
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()
        assert getattr(app, "_conv_orch", None) is None
        rendered: list[str] = []
        app._set_response = lambda text: rendered.append(text)  # type: ignore

        app._apply_side_effect("show_access_card")
        await pilot.pause()

        assert rendered and "doctor" in rendered[0]