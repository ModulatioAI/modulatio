# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""ModelPicker — step 3 of the Configuration tab's add-model flow.

The payoff: pick a model and the register step has everything (base_url,
api_format, auth, model id) — the operator never typed any of it except the
key. For cloud providers it fetches the live list on a worker thread; for
locals it probes the running server (empty + a hint if it's down); for custom
the operator types the id directly.

The list is the curated default (pinned → flagship families by recency) with a
search box that reaches the whole catalog, and **free models flagged** with the
provider's truthful rate-limit caveat. Selecting one posts ``ModelChosen``.
"""
from __future__ import annotations

import os

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.message import Message
from textual.widgets import Button, Input, OptionList, Static
from textual.widgets.option_list import Option

from modulatio import provider_catalog as pc


class ModelPicker(Vertical):
    """Searchable model list (or manual id for custom) → ModelChosen."""

    DEFAULT_CSS = """
    ModelPicker { height: 1fr; padding: 1; border: round $frame; }
    ModelPicker .mp-title { text-style: bold; color: $primary; }
    ModelPicker #mp-list { height: 1fr; border: round $frame-dim; }
    ModelPicker #mp-free-note, ModelPicker #mp-status { color: $text-muted; height: auto; }
    ModelPicker Input { margin: 1 0; }
    """

    class ModelChosen(Message):
        def __init__(self, provider_id: str, model_id: str) -> None:
            self.provider_id = provider_id
            self.model_id = model_id
            super().__init__()

    def __init__(
        self,
        provider: pc.Provider,
        *,
        env_var: str | None = None,
        base_url: str | None = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.provider = provider
        self._env_var = env_var
        self._base_url = base_url  # custom override
        self._models: list[pc.CatalogModel] = []

    @property
    def is_custom(self) -> bool:
        return self.provider.models_source.kind == "custom"

    def compose(self) -> ComposeResult:
        yield Static(f"Pick a model · {self.provider.name}", classes="mp-title")
        if self.is_custom:
            yield Input(placeholder="model id, e.g. my-model-v1", id="mp-custom-id")
            yield Button("Use this model", id="mp-custom-go", variant="primary")
        else:
            yield Input(placeholder="search models…", id="mp-search")
            yield OptionList(id="mp-list")
            if self.provider.free_note:
                yield Static(f"free: {self.provider.free_note}", id="mp-free-note")
            yield Static("loading…", id="mp-status")

    def on_mount(self) -> None:
        if self.is_custom:
            self.query_one("#mp-custom-id", Input).focus()
        else:
            self._load()

    # ── fetch (worker thread) ───────────────────────────────────────────

    @work(thread=True, exclusive=True)
    def _load(self) -> None:
        key = os.environ.get(self._env_var) if self._env_var else None
        try:
            models = pc.fetch_models(self.provider, api_key=key)
        except Exception:
            models = []
        self.app.call_from_thread(self._on_loaded, models)

    def _on_loaded(self, models: list[pc.CatalogModel]) -> None:
        # The fetch runs on a worker thread; the operator may have navigated
        # away (picker unmounted) before it completes. Touching query_one on a
        # detached widget raises NoMatches on the main thread — bail quietly.
        if not self.is_mounted:
            return
        # role-relevant text models drive the picker; note any other modalities.
        self._models = pc.of_modality(models, "text")
        others = len(models) - len(self._models)
        status = self.query_one("#mp-status", Static)
        if not self._models:
            status.update(
                "no models found — is the local server running?"
                if self.provider.models_source.kind == "local_probe"
                else "no models returned (check the key/endpoint)"
            )
        else:
            tail = f"  ·  {others} image/video/voice model(s) also available" if others else ""
            status.update(f"{len(self._models)} models{tail}")
        self._populate()

    # ── render + filter ─────────────────────────────────────────────────

    def _populate(self, query: str = "") -> None:
        listing = (
            pc.search(self._models, query)
            if query
            else pc.curated_default(self.provider, self._models, limit=40)
        )
        opt_list = self.query_one("#mp-list", OptionList)
        opt_list.clear_options()
        # re-sweep: the listing aggregates multiple sources (models_source +
        # extra_sources), which can carry the same id twice. OptionList raises
        # DuplicateID on a repeat id — dedup by first occurrence (preserving
        # order) so a noisy feed can't crash the picker.
        seen: set[str] = set()
        for m in listing:
            if m.id in seen:
                continue
            seen.add(m.id)
            opt_list.add_option(Option(self._label(m), id=m.id))

    def _label(self, m: pc.CatalogModel) -> Text:
        # Feng-Tui: theme-tracked tiers (base id, accent for the free marker).
        th = self.app.current_theme
        line = Text()
        line.append(m.id, style=th.foreground or "#E0E0E0")
        if m.is_free:
            line.append("  [FREE]", style=f"bold {th.primary}")
        return line

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "mp-search":
            self._populate(event.value.strip())

    def on_option_list_option_selected(
        self, event: OptionList.OptionSelected
    ) -> None:
        if event.option.id:
            self.post_message(self.ModelChosen(self.provider.id, event.option.id))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "mp-custom-go":
            model_id = self.query_one("#mp-custom-id", Input).value.strip()
            if not model_id:
                self.query_one("#mp-custom-id", Input).focus()
                return
            self.post_message(self.ModelChosen(self.provider.id, model_id))


__all__ = ["ModelPicker"]
