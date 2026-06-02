# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""KeySelector — pick which of a provider's keys to use, never showing values.

When a provider has more than one key configured, this renders a radio list:
``Key #1 · text`` / ``Key #2 · images`` / … — the operator picks one without
ever seeing the secret. #1 is pre-selected (the default). With one key (or
none) it renders nothing and resolves to #1 automatically.

Used by the auth step (add-model flow) and the agent builder, so a text agent
can ride key #1 while an image model rides key #2 and the vendor meters each
separately. No cap on the number of keys.
"""
from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import RadioButton, RadioSet

from modulatio import provider_keys


class KeySelector(Vertical):
    """Radio of a provider's keys (#1/#2/#N, labelled, redacted). #1 default."""

    DEFAULT_CSS = "KeySelector { height: auto; }"

    def __init__(self, base_env_var: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self.base_env_var = base_env_var
        self._slots = provider_keys.list_keys(base_env_var)

    @property
    def has_choice(self) -> bool:
        """True only when there's more than one key (so a picker is shown)."""
        return len(self._slots) > 1

    def compose(self) -> ComposeResult:
        if self.has_choice:
            with RadioSet(id="key-radio"):
                for i, slot in enumerate(self._slots):
                    label = f"Key #{slot['index']}"
                    if slot["label"]:
                        label += f"  ·  {slot['label']}"
                    yield RadioButton(label, value=(i == 0))  # #1 default

    @property
    def chosen_env_var(self) -> str:
        """The env var of the selected key — #1 (the base var) when there's no
        picker (≤1 key)."""
        if not self.has_choice:
            return (
                self._slots[0]["env_var"]
                if self._slots
                else provider_keys.default_env_var(self.base_env_var)
            )
        try:
            radio = self.query_one("#key-radio", RadioSet)
            idx = radio.pressed_index if radio.pressed_index >= 0 else 0
        except Exception:
            idx = 0
        return self._slots[idx]["env_var"]


__all__ = ["KeySelector"]
