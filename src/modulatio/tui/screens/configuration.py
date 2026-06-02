# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Configuration tab — the MODELS side: list configured models + the add flow.

Assembles the three step-widgets into one wizard:

    LIST → "+ Add model" → ProviderPicker → AuthStep → ModelPicker → register

Register feeds the existing backend: ``provider_catalog.preset_kwargs`` →
``model_presets.add_preset`` — everything (base_url, api_format, auth, model id)
auto-filled from the provider + pick; the operator typed only the key. The new
model lands back in the MODELS list with its live ``ready ✓`` / needs-setup
status. The AGENTS side (assign Leader/QC, producers) is a sibling screen.
"""
from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Button, DataTable, Static

from modulatio import model_presets
from modulatio import provider_catalog as pc
from modulatio.tui.widgets.auth_step import AuthStep
from modulatio.tui.widgets.model_picker import ModelPicker
from modulatio.tui.widgets.provider_picker import ProviderPicker


class ConfigScreen(Vertical):
    """Configuration · Models — the list + the provider→auth→model add flow."""

    DEFAULT_CSS = """
    ConfigScreen { padding: 1; }
    ConfigScreen .cfg-title { text-style: bold; color: $primary; }
    ConfigScreen #cfg-body { height: 1fr; }
    ConfigScreen #cfg-models { height: 1fr; border: round $frame-dim; }
    ConfigScreen #cfg-status { color: $text-muted; height: auto; }
    ConfigScreen Button { margin: 1 0; }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._provider_id: str | None = None
        self._auth_type: str | None = None
        self._env_var: str | None = None
        self._base_url: str | None = None

    def compose(self) -> ComposeResult:
        yield Static("CONFIGURATION · Models", classes="cfg-title")
        yield Vertical(id="cfg-body")

    def on_mount(self) -> None:
        self.show_list()

    def _body(self) -> Vertical:
        return self.query_one("#cfg-body", Vertical)

    def _swap(self, widget) -> None:
        body = self._body()
        body.remove_children()
        body.mount(widget)

    # ── the list ────────────────────────────────────────────────────────

    def show_list(self, message: str = "") -> None:
        body = self._body()
        body.remove_children()
        table = DataTable(id="cfg-models", cursor_type="row")
        table.add_columns("Key", "Model", "Auth", "Status")
        for key, preset in sorted(model_presets.load_presets().items()):
            ready = model_presets.is_available(key)
            table.add_row(
                key,
                preset.get("model", ""),
                preset.get("auth_type", ""),
                "ready ✓" if ready else "needs setup",
            )
        body.mount(table)
        body.mount(Button("+ Add model", id="cfg-add", variant="primary"))
        body.mount(Static(message, id="cfg-status"))

    # ── flow transitions (messages bubble up from the step widgets) ─────

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cfg-add":
            self._provider_id = self._auth_type = None
            self._env_var = self._base_url = None
            self._swap(ProviderPicker(id="cfg-pp"))

    def on_provider_picker_provider_chosen(
        self, event: ProviderPicker.ProviderChosen
    ) -> None:
        self._provider_id = event.provider_id
        provider = pc.get_provider(event.provider_id)
        if provider is not None:
            self._swap(AuthStep(provider, id="cfg-auth"))

    def on_auth_step_auth_configured(
        self, event: AuthStep.AuthConfigured
    ) -> None:
        self._auth_type = event.auth_type
        self._env_var = event.env_var
        self._base_url = event.base_url
        provider = pc.get_provider(event.provider_id)
        if provider is not None:
            self._swap(ModelPicker(
                provider, env_var=event.env_var, base_url=event.base_url,
                id="cfg-mp",
            ))

    def on_model_picker_model_chosen(
        self, event: ModelPicker.ModelChosen
    ) -> None:
        key = self.register(event.model_id)
        self.show_list(f"Added '{key}'." if key else "Could not add the model.")

    # ── register → the existing model_presets backend ───────────────────

    def register(self, model_id: str) -> str | None:
        provider = pc.get_provider(self._provider_id or "")
        if provider is None or self._auth_type is None:
            return None
        model = pc.CatalogModel(
            id=model_id, name=model_id, provider_id=provider.id,
        )
        # resolve the chosen auth (carrying the env var, incl. custom-named)
        auth = pc.AuthOption(
            auth_type=self._auth_type, label="", env_var=self._env_var,
        )
        kwargs = pc.preset_kwargs(provider, model, auth)
        if self._base_url:  # custom endpoint override
            kwargs["base_url"] = self._base_url
        key = kwargs.pop("key")
        try:
            model_presets.add_preset(key, **kwargs)
        except ValueError:
            # already registered → update it in place
            kwargs.pop("label", None)
            model_presets.update_preset(key, **kwargs)
        return key


__all__ = ["ConfigScreen"]
