"""Tests for the Configuration tab's ConfigScreen (add-model flow, slice 4)."""
from __future__ import annotations

import os

from textual.app import App, ComposeResult
from textual.widgets import DataTable, OptionList

from modulatio import config
from modulatio import model_presets
from modulatio import provider_catalog as pc
from modulatio import provider_keys
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

        await screen.on_provider_picker_provider_chosen(
            ProviderPicker.ProviderChosen("ollama_local"))
        await pilot.pause()
        assert app.query("#cfg-auth")  # AuthStep
        assert app.query("#cfg-cancel")  # Cancel is offered at each step

        await screen.on_auth_step_auth_configured(
            AuthStep.AuthConfigured("ollama_local", "none", None, None))
        await pilot.pause()
        assert app.query("#cfg-mp")  # ModelPicker
        ol = app.query_one("#mp-list", OptionList)
        for _ in range(60):
            await pilot.pause(0.05)
            if ol.option_count:
                break

        await screen.on_model_picker_model_chosen(
            ModelPicker.ModelChosen("ollama_local", "llama3.3:8b"))
        await pilot.pause()
        # back to the list, and the preset is registered
        assert app.query("#cfg-models")
        presets = model_presets.load_presets()
        assert any(p["model"] == "llama3.3:8b" for p in presets.values())


async def test_cancel_returns_to_the_list_mid_flow(tmp_path, monkeypatch):
    """Cancel at any add-model step bails back to the model list."""
    monkeypatch.setattr(model_presets, "PRESETS_FILE", tmp_path / "p.json")
    app = _Host()
    async with app.run_test(size=(120, 50)) as pilot:
        await pilot.pause()
        screen = app.query_one(ConfigScreen)

        await pilot.click("#cfg-add")
        await pilot.pause()
        assert app.query("#cfg-pp")      # in the flow (ProviderPicker)
        assert app.query("#cfg-cancel")  # Cancel offered

        # advance a step, then cancel
        await screen.on_provider_picker_provider_chosen(
            ProviderPicker.ProviderChosen("openrouter"))
        await pilot.pause()
        assert app.query("#cfg-auth")

        await pilot.click("#cfg-cancel")
        await pilot.pause()
        # back to the list; the flow widgets are gone
        assert app.query("#cfg-models")
        assert not app.query("#cfg-auth")
        assert not app.query("#cfg-cancel")


async def test_pin_key_to_model_removes_it_from_the_pool(tmp_path, monkeypatch):
    """The optional lever: pin a key to a model → the preset points at that one
    key (no pool flag) and the key leaves the shared pool. 'Use pool' reverts."""
    monkeypatch.setattr(model_presets, "PRESETS_FILE", tmp_path / "p.json")
    monkeypatch.setattr(provider_keys, "LABELS_FILE", tmp_path / "labels.json")
    monkeypatch.setattr(provider_keys, "PINS_FILE", tmp_path / "pins.json")
    monkeypatch.setenv("OPENROUTER_API_KEY", "k1")
    monkeypatch.setenv("OPENROUTER_API_KEY_2", "k2")
    model_presets.add_preset(
        "orfree", label="x", base_url="https://openrouter.ai/api/v1",
        api_format="openai", auth_type="api_key", model="openrouter/free",
        auth_config={"env_var": "OPENROUTER_API_KEY", "pool": True},
    )
    app = _Host()
    async with app.run_test(size=(120, 50)) as pilot:
        await pilot.pause()
        screen = app.query_one(ConfigScreen)
        app.query_one("#cfg-models", DataTable).move_cursor(row=0)
        await pilot.pause()
        await pilot.click("#cfg-pinkey")
        await pilot.pause()
        assert app.query("#cfg-pinlist")  # the pin manager opened

        # pin key #2 to this model
        screen._km_selected_key = "OPENROUTER_API_KEY_2"
        await screen._pin_selected_to_model()
        await pilot.pause()
        preset = model_presets.get_preset("orfree")
        assert preset["auth_config"] == {"env_var": "OPENROUTER_API_KEY_2"}
        assert provider_keys.pool_env_vars("OPENROUTER_API_KEY") == [
            "OPENROUTER_API_KEY"]  # #2 left the pool

        # revert to the shared pool
        await screen._use_pool_for_model()
        await pilot.pause()
        preset = model_presets.get_preset("orfree")
        assert preset["auth_config"] == {
            "env_var": "OPENROUTER_API_KEY", "pool": True}
        assert provider_keys.pool_env_vars("OPENROUTER_API_KEY") == [
            "OPENROUTER_API_KEY", "OPENROUTER_API_KEY_2"]  # #2 back in the pool


async def test_provider_key_manager_removes_a_key_and_repoints_pins(
    tmp_path, monkeypatch
):
    """The standalone key manager (no model needed): drill into a provider,
    remove a key. A pinned key's model is repointed to the pool, never left
    dangling on a dead var."""
    monkeypatch.setattr(model_presets, "PRESETS_FILE", tmp_path / "p.json")
    monkeypatch.setattr(provider_keys, "LABELS_FILE", tmp_path / "labels.json")
    monkeypatch.setattr(provider_keys, "PINS_FILE", tmp_path / "pins.json")
    monkeypatch.setattr(
        config, "remove_env_secret",
        lambda n: os.environ.pop(n, None) is not None)
    monkeypatch.setenv("OPENROUTER_API_KEY", "k1")
    monkeypatch.setenv("OPENROUTER_API_KEY_2", "k2")
    # a model pinned to key #2
    model_presets.add_preset(
        "orpin", label="x", base_url="https://openrouter.ai/api/v1",
        api_format="openai", auth_type="api_key", model="openrouter/free",
        auth_config={"env_var": "OPENROUTER_API_KEY_2"},
    )
    provider_keys.pin_key("OPENROUTER_API_KEY_2", "orpin")
    app = _Host()
    async with app.run_test(size=(120, 50)) as pilot:
        await pilot.pause()
        screen = app.query_one(ConfigScreen)
        assert app.query("#cfg-provlist")  # the manager section is on the list
        await screen._show_provider_keys("openrouter")  # drill in
        await pilot.pause()
        assert app.query("#cfg-provkeylist")

        screen._prov_selected_key = "OPENROUTER_API_KEY_2"
        await screen._remove_provider_key()
        await pilot.pause()
        # the key is gone from Modulatio entirely…
        assert "OPENROUTER_API_KEY_2" not in os.environ
        assert provider_keys.pool_env_vars("OPENROUTER_API_KEY") == [
            "OPENROUTER_API_KEY"]
        # …and the model it was pinned to fell back to the shared pool
        assert model_presets.get_preset("orpin")["auth_config"] == {
            "env_var": "OPENROUTER_API_KEY", "pool": True}


async def test_back_from_key_manager_returns_to_list(tmp_path, monkeypatch):
    """Back out of the key manager → the model list, with no DuplicateIds
    crash (the bug: the manager's #cfg-status collided with the list's when
    show_list mounted before the old one finished removing)."""
    from textual.widgets import Button

    monkeypatch.setattr(model_presets, "PRESETS_FILE", tmp_path / "p.json")
    monkeypatch.setattr(provider_keys, "LABELS_FILE", tmp_path / "labels.json")
    monkeypatch.setattr(provider_keys, "PINS_FILE", tmp_path / "pins.json")
    monkeypatch.setenv("OPENROUTER_API_KEY", "k1")
    app = _Host()
    async with app.run_test(size=(120, 50)) as pilot:
        await pilot.pause()
        screen = app.query_one(ConfigScreen)
        await screen._show_provider_keys("openrouter")
        await pilot.pause()
        assert app.query("#cfg-provkeylist")
        # Back (cfg-cancel) → the list
        await screen.on_button_pressed(
            Button.Pressed(app.query_one("#cfg-cancel", Button)))
        await pilot.pause()
        assert app.query("#cfg-models")        # back on the list
        assert not app.query("#cfg-provkeylist")  # manager torn down cleanly


async def test_remove_deletes_the_selected_preset(tmp_path, monkeypatch):
    monkeypatch.setattr(model_presets, "PRESETS_FILE", tmp_path / "p.json")
    model_presets.add_preset(
        "op_free", label="x", base_url="https://openrouter.ai/api/v1",
        api_format="openai", auth_type="api_key", model="openrouter/free",
        auth_config={"env_var": "OPENROUTER_API_KEY"},
    )
    model_presets.add_preset(
        "dead_one", label="y", base_url="https://ollama.com/v1",
        api_format="openai", auth_type="api_key",
        model="deepseek-v4-pro:cloud",  # the stale local-suffix kind
        auth_config={"env_var": "OLLAMA_API_KEY"},
    )
    app = _Host()
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.query_one(ConfigScreen)
        table = app.query_one("#cfg-models", DataTable)
        # presets sorted by key: dead_one, op_free → row 0 is dead_one
        table.move_cursor(row=0)
        await pilot.pause()
        assert screen._selected_preset_key() == "dead_one"
        await pilot.click("#cfg-remove")
        await pilot.pause()
        assert model_presets.get_preset("dead_one") is None
        assert model_presets.get_preset("op_free") is not None
        assert app.query_one("#cfg-models", DataTable).row_count == 1
