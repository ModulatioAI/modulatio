# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""ProviderPicker — step 1 of the Configuration tab's add-model flow.

Lists every provider in the catalog with at-a-glance badges: how you auth
(key / OAuth / none / custom), whether it has free models, and whether it's
ready right now (``ready ✓``) or needs setup. Selecting one posts
``ProviderChosen`` so the screen can advance to the auth + model steps.

The point of the whole configurator: pick a provider + a model and everything
auto-fills — the operator types only the key. This is where that starts.
"""
from __future__ import annotations

from rich.text import Text
from textual.message import Message
from textual.widgets import OptionList
from textual.widgets.option_list import Option

from modulatio import provider_catalog as pc


def _auth_word(provider: pc.Provider) -> str:
    """A compact label for how this provider authenticates."""
    types = {a.auth_type for a in provider.auth_options}
    if provider.models_source.kind == "custom":
        return "custom"
    if types == {"none"}:
        return "local"
    has_key = "api_key" in types
    has_oauth = any(t.startswith("oauth") for t in types)
    if has_key and has_oauth:
        return "key / OAuth"
    if has_oauth:
        return "OAuth"
    return "key"


def _provider_ready(provider: pc.Provider) -> bool:
    """True if any of the provider's auth options is usable right now."""
    return any(pc.auth_status(a)[0] for a in provider.auth_options)


class ProviderPicker(OptionList):
    """Catalog providers as a selectable, badge-annotated list."""

    DEFAULT_CSS = """
    ProviderPicker {
        height: 1fr;
        border: round $frame;
        background: $background;
        scrollbar-color: $frame-dim;
    }
    """

    class ProviderChosen(Message):
        """Posted when the operator picks a provider."""

        def __init__(self, provider_id: str) -> None:
            self.provider_id = provider_id
            super().__init__()

    def on_mount(self) -> None:
        self.populate()

    def populate(self) -> None:
        """(Re)build the list — auth-readiness is re-evaluated each time."""
        self.clear_options()
        for p in pc.list_providers():
            self.add_option(Option(self._label(p), id=p.id))

    def _label(self, p: pc.Provider) -> Text:
        line = Text()
        line.append(f"{p.name:18}", style="bold #ffb000")
        line.append(f" {_auth_word(p):11}", style="#b08858")
        # free badge
        if p.free_detect != "none":
            line.append(" free", style="bold #ff6b35")
        else:
            line.append("     ", style="#b08858")
        # readiness — beta OAuth providers note it
        beta = any(a.beta for a in p.auth_options)
        if p.models_source.kind == "custom":
            line.append("  set up", style="#b08858")  # always manual entry
        elif _provider_ready(p):
            line.append("  ready ✓", style="#6cb6e4")
        elif p.models_source.kind == "local_probe":
            line.append("  set up", style="#b08858")
        else:
            line.append("  needs key", style="#b08858")
        if beta:
            line.append("  (oauth beta)", style="#b08858")
        return line

    def on_option_list_option_selected(
        self, event: OptionList.OptionSelected
    ) -> None:
        if event.option.id:
            self.post_message(self.ProviderChosen(event.option.id))


__all__ = ["ProviderPicker"]
