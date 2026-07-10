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
from rich.errors import MarkupError as RichMarkupError
from textual.widgets._data_table import default_cell_formatter
import json
import pytest


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


async def test_registry_list_stays_mounted_during_the_add_flow(tmp_path, monkeypatch):
    """Configurator archetype: the registry list (the doorway) persists while
    the add flow runs in the companion — the old _swap() wiped the whole body,
    so the model list vanished mid-flow. It must NOT vanish now."""
    monkeypatch.setattr(model_presets, "PRESETS_FILE", tmp_path / "p.json")
    app = _Host()
    async with app.run_test(size=(120, 50)) as pilot:
        await pilot.pause()
        screen = app.query_one(ConfigScreen)
        await pilot.click("#cfg-add")
        await pilot.pause()
        assert app.query("#cfg-pp")       # the flow opened in the companion
        assert app.query("#cfg-models")   # …and the registry list is STILL there
        await screen.on_provider_picker_provider_chosen(
            ProviderPicker.ProviderChosen("openrouter"))
        await pilot.pause()
        assert app.query("#cfg-auth")
        assert app.query("#cfg-models")   # still mounted a step deeper


async def test_cancel_resets_accumulated_flow_state(tmp_path, monkeypatch):
    """Jenny F3: cancelling a half-entered add flow clears every accumulated
    wizard input, so it can't leak into the next flow."""
    monkeypatch.setattr(model_presets, "PRESETS_FILE", tmp_path / "p.json")
    app = _Host()
    async with app.run_test(size=(120, 50)) as pilot:
        await pilot.pause()
        screen = app.query_one(ConfigScreen)
        await pilot.click("#cfg-add")
        await pilot.pause()
        await screen.on_provider_picker_provider_chosen(
            ProviderPicker.ProviderChosen("openrouter"))
        await pilot.pause()
        assert screen._provider_id == "openrouter"  # state accumulated
        await pilot.click("#cfg-cancel")
        await pilot.pause()
        assert screen._provider_id is None           # …and wiped on cancel
        assert screen._auth_type is None


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
        await screen._remove_provider_key()   # opens the confirm guard
        await pilot.pause()
        await pilot.click("#confirm-yes")
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
        await pilot.click("#cfg-remove")     # opens the confirm guard
        await pilot.pause()
        await pilot.click("#confirm-yes")
        await pilot.pause()
        assert model_presets.get_preset("dead_one") is None
        assert model_presets.get_preset("op_free") is not None
        assert app.query_one("#cfg-models", DataTable).row_count == 1


# ═══ fold: test_tui_screens_configuration_preship.py ═══
# Regression: Configuration·Models list DataTable must not raise MarkupError
# on an operator-authored custom model id / preset key containing markup-closer
# brackets (preship sweep, configuration.py:118).
#
# ``_fill_table`` populates a textual ``DataTable``. Textual's
# ``default_cell_formatter`` runs every ``str`` cell through ``Text.from_markup``,
# which raises ``rich.errors.MarkupError`` on sequences like ``[/v2]`` at paint
# time and crashes the TUI. The custom-provider flow lets the operator type an
# arbitrary model id / preset key, so the painted cells (key, model, auth_type)
# are escaped before they reach the table.
#
# We exercise the real ``_fill_table`` against a fake table that captures the
# cells it would paint, then run each captured cell through the exact formatter
# the DataTable uses — proving the escape keeps paint from raising while the
# literal brackets survive.


class _CapturingTable:
    """Stand-in for DataTable that records the cells ``add_row`` is given —
    no active-app context required (real DataTable.add_row needs one)."""

    def __init__(self) -> None:
        self.rows: list[tuple] = []

    def add_row(self, *cells, key=None):  # noqa: D401 - mimics DataTable
        self.rows.append(cells)


def _render_cell(value: object) -> object:
    """Render a cell the way DataTable paints it — raising MarkupError if the
    cell's markup is invalid."""
    return default_cell_formatter(value)


def test_default_cell_formatter_raises_on_unescaped_bracket_closer():
    """Sanity: the hazard is real — a raw bracket-closer string crashes the
    formatter the DataTable uses to paint."""
    try:
        _render_cell("my-model[/v2]")
    except RichMarkupError:
        return
    raise AssertionError("expected MarkupError on unescaped bracket closer")


def test_fill_table_escapes_bracketed_custom_model(monkeypatch):
    """The fix: a custom preset with bracket-bearing key / model / auth_type
    is escaped, so every painted cell survives the formatter that crashes on
    raw markup-closers — and the literal brackets are preserved."""
    bad_presets = {
        "my-key[/v2]": {
            "model": "vendor/model[/x]",
            "auth_type": "api_key[/oops]",
        }
    }
    monkeypatch.setattr(model_presets, "load_presets", lambda: bad_presets)
    monkeypatch.setattr(model_presets, "is_available", lambda key: False)

    table = _CapturingTable()
    # Bind the unbound method without constructing the full screen/app.
    ConfigScreen._fill_table(object.__new__(ConfigScreen), table)

    assert len(table.rows) == 1
    for cell in table.rows[0]:
        if isinstance(cell, str):
            _render_cell(cell)  # must not raise

    # Literal brackets preserved (escaped, not parsed away as markup).
    assert "my-key[/v2]" in str(_render_cell(table.rows[0][0]))
    assert "vendor/model[/x]" in str(_render_cell(table.rows[0][1]))


# ═══ fold: test_tui_screens_configuration_r2_audit.py ═══
# Regression tests for the configuration TUI screen (round-2 audit).
#
# Covers the `register()` error-handling defect: a ValueError raised by
# ``model_presets.add_preset`` for a *validation/security* reason (bad
# api_format/auth_type, or the secret-leak keel) was being swallowed and
# re-interpreted as "already registered -> update in place", masking a genuine
# rejection and routing it onto the update path (which skips the secret keel).
#
# The fix: only re-route to ``update_preset`` when the entry actually exists;
# otherwise re-raise so the rejection surfaces.


def _make_screen():
    """Build a ConfigScreen without the Textual app/mount machinery.

    `register()` only reads plain instance attributes + calls into
    model_presets / provider_catalog, so a bare instance with the relevant
    attrs set is enough to exercise it in isolation.
    """
    screen = object.__new__(ConfigScreen)
    # A real catalog provider with a standard api-key auth path.
    screen._provider_id = "openai"
    screen._auth_type = "api_key"
    screen._env_var = "OPENAI_API_KEY"
    screen._base_url = None
    screen._pool = False
    return screen


@pytest.fixture()
def isolated_presets(tmp_path, monkeypatch):
    monkeypatch.setattr(model_presets, "PRESETS_FILE", tmp_path / "model_presets.json")
    return tmp_path


def test_register_reraises_validation_error_for_nonexistent_key(
    isolated_presets, monkeypatch
):
    """A validation/security ValueError on a key that does NOT exist must
    surface, not be masked as a successful in-place update."""
    screen = _make_screen()

    update_calls = []

    def fake_add_preset(key, **kwargs):
        # Simulate the secret-leak keel / format-validation rejection: the key
        # does not yet exist, so this is a genuine rejection.
        raise ValueError("auth_config may not carry raw secret value(s)")

    def fake_update_preset(key, **kwargs):
        update_calls.append(key)
        return {}

    monkeypatch.setattr(model_presets, "add_preset", fake_add_preset)
    monkeypatch.setattr(model_presets, "update_preset", fake_update_preset)

    with pytest.raises(ValueError, match="raw secret"):
        screen.register("gpt-4o")

    # Crucially: the rejection was NOT silently re-routed to update_preset.
    assert update_calls == []


def test_register_routes_to_update_when_key_already_exists(isolated_presets):
    """When the entry genuinely already exists, register() still updates it in
    place (the intended pre-existing behavior is preserved)."""
    screen = _make_screen()

    # First registration creates the entry on disk.
    key = screen.register("gpt-4o")
    assert key is not None
    assert model_presets.get_preset(key) is not None
    first = model_presets.get_preset(key)

    # Second registration hits the "already exists" ValueError -> update in
    # place. It must succeed (no raise) and the entry must remain present.
    key2 = screen.register("gpt-4o")
    assert key2 == key
    assert model_presets.get_preset(key) is not None
    # Sanity: the label-bearing add path and the label-stripped update path both
    # leave a coherent entry.
    second = model_presets.get_preset(key)
    assert second["model"] == first["model"]


def test_register_does_not_create_entry_when_add_rejected(
    isolated_presets, monkeypatch
):
    """A rejected registration must not leave any preset persisted on disk."""
    screen = _make_screen()

    def fake_add_preset(key, **kwargs):
        raise ValueError("api_format must be one of ...")

    monkeypatch.setattr(model_presets, "add_preset", fake_add_preset)

    with pytest.raises(ValueError):
        screen.register("gpt-4o")

    presets_file = isolated_presets / "model_presets.json"
    if presets_file.exists():
        assert json.loads(presets_file.read_text()) == {}
