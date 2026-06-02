"""Tests for the Configuration tab's ConfigScreen (add-model flow, slice 4)."""
from __future__ import annotations

from textual.app import App, ComposeResult
from textual.widgets import OptionList

from modulatio import model_presets
from modulatio import provider_catalog as pc
from modulatio.tui.screens.configuration import ConfigScreen
from modulatio.tui.widgets.auth_step import AuthStep
from modulatio.tui.widgets.model_picker import ModelPicker
from modulatio.tui.widgets.provider_picker import ProviderPicker


class _Host(App):
    def get_css_variables(self) -> dict[str, str]:
        v = super().get_css_variables()
        v.setdefault("frame", "#6cb6e4")
        v.setdefault("frame-dim", "#3f6d8c")
        return v

    def compose(self) -> ComposeResult:
        yield ConfigScreen(id="cfg")


async def test_register_builds_a_preset_from_the_catalog(tmp_path, monkeypatch):
    monkeypatch.setattr(model_presets, "PRESETS_FILE", tmp_path / "p.json")
    app = _Host()
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.query_one(ConfigScreen)
        screen._provider_id = "openrouter"
        screen._auth_type = "api_key"
        screen._env_var = "OPENROUTER_API_KEY"
        key = screen.register("openrouter/free")
        assert key
        p = model_presets.get_preset(key)
        assert p is not None
        assert p["base_url"] == "https://openrouter.ai/api/v1"
        assert p["model"] == "openrouter/free"
        assert p["auth_type"] == "api_key"
        assert p["auth_config"] == {"env_var": "OPENROUTER_API_KEY"}


async def test_custom_register_uses_the_typed_base_url(tmp_path, monkeypatch):
    monkeypatch.setattr(model_presets, "PRESETS_FILE", tmp_path / "p.json")
    app = _Host()
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.query_one(ConfigScreen)
        screen._provider_id = "custom"
        screen._auth_type = "none"
        screen._base_url = "https://my-endpoint/v1"
        key = screen.register("my-model")
        p = model_presets.get_preset(key)
        assert p["base_url"] == "https://my-endpoint/v1"
        assert p["model"] == "my-model"


async def test_full_flow_provider_auth_model_registers(tmp_path, monkeypatch):
    monkeypatch.setattr(model_presets, "PRESETS_FILE", tmp_path / "p.json")
    monkeypatch.setattr(pc, "fetch_models", lambda p, **k: [
        pc.CatalogModel(id="llama3.3:8b", name="l", provider_id="ollama_local",
                        is_free=True, modality="text"),
    ])
    app = _Host()
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.query_one(ConfigScreen)

        await pilot.click("#cfg-add")
        await pilot.pause()
        assert app.query("#cfg-pp")  # ProviderPicker

        screen.on_provider_picker_provider_chosen(
            ProviderPicker.ProviderChosen("ollama_local"))
        await pilot.pause()
        assert app.query("#cfg-auth")  # AuthStep

        screen.on_auth_step_auth_configured(
            AuthStep.AuthConfigured("ollama_local", "none", None, None))
        await pilot.pause()
        assert app.query("#cfg-mp")  # ModelPicker
        ol = app.query_one("#mp-list", OptionList)
        for _ in range(60):
            await pilot.pause(0.05)
            if ol.option_count:
                break

        screen.on_model_picker_model_chosen(
            ModelPicker.ModelChosen("ollama_local", "llama3.3:8b"))
        await pilot.pause()
        # back to the list, and the preset is registered
        assert app.query("#cfg-models")
        presets = model_presets.load_presets()
        assert any(p["model"] == "llama3.3:8b" for p in presets.values())
