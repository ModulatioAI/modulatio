# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""AuthStep — step 2 of the Configuration tab's add-model flow.

After a provider is chosen, this collects whatever auth it needs and nothing
more — the operator types only the key:

  - **api_key**   → a masked key field (saved to the vault .env as the
    provider's env var), with the signup URL to get one. Skipped if the key is
    already set.
  - **oauth**     → a readiness line (``signed in ✓`` or the setup hint, e.g.
    "run `claude login`") + a Recheck button. Beta options say so.
  - **none**      → local server, nothing to enter.
  - **custom**    → a base_url field (the operator supplies the endpoint).

A provider with more than one option (key / OAuth) shows a method selector.
On success it posts ``AuthConfigured`` so the screen advances to the model
picker.
"""
from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.message import Message
from textual.widgets import Button, Input, RadioButton, RadioSet, Static

from modulatio import config
from modulatio import provider_catalog as pc


class AuthStep(Vertical):
    """Collect a provider's auth, then post AuthConfigured."""

    DEFAULT_CSS = """
    AuthStep { height: auto; padding: 1; border: round $frame; }
    AuthStep .auth-title { text-style: bold; color: $primary; }
    AuthStep #auth-body { height: auto; padding: 1 0; }
    AuthStep #auth-status { color: $text-muted; height: auto; }
    AuthStep Input { margin: 1 0; }
    """

    class AuthConfigured(Message):
        """Auth satisfied — advance to the model picker."""

        def __init__(
            self,
            provider_id: str,
            auth_type: str,
            env_var: str | None = None,
            base_url: str | None = None,
        ) -> None:
            self.provider_id = provider_id
            self.auth_type = auth_type
            self.env_var = env_var
            self.base_url = base_url
            super().__init__()

    def __init__(self, provider: pc.Provider, **kwargs) -> None:
        super().__init__(**kwargs)
        self.provider = provider
        self._selected = provider.auth_options[0]

    def compose(self) -> ComposeResult:
        yield Static(f"Authenticate · {self.provider.name}", classes="auth-title")
        if len(self.provider.auth_options) > 1:
            with RadioSet(id="auth-method"):
                for i, a in enumerate(self.provider.auth_options):
                    label = a.label + ("  (beta)" if a.beta else "")
                    yield RadioButton(label, value=(i == 0))
        yield Vertical(id="auth-body")
        yield Button("Continue", id="auth-continue", variant="primary")
        yield Static("", id="auth-status")

    def on_mount(self) -> None:
        self._render_body()

    def on_radio_set_changed(self, event: RadioSet.Changed) -> None:
        self._selected = self.provider.auth_options[event.radio_set.pressed_index]
        self._render_body()

    def _render_body(self) -> None:
        body = self.query_one("#auth-body", Vertical)
        body.remove_children()
        a = self._selected
        is_custom = self.provider.models_source.kind == "custom"
        if is_custom:
            body.mount(Input(
                placeholder="base_url, e.g. https://host/v1", id="auth-baseurl",
            ))
        if a.auth_type == "api_key":
            ready, _ = pc.auth_status(a)
            if ready:
                body.mount(Static(f"✓ {a.env_var} is already set — using it. "
                                  "Enter a new key only to replace it."))
            if a.env_var is None:  # custom: also name the env var
                body.mount(Input(
                    placeholder="env var name, e.g. MYPROVIDER_API_KEY",
                    id="auth-envvar",
                ))
            tail = f"saved as {a.env_var}" if a.env_var else "saved to your .env"
            body.mount(Input(
                password=True,
                placeholder=f"paste your API key  →  {tail}",
                id="auth-key",
            ))
            if self.provider.signup_url:
                body.mount(Static(f"Need one? {self.provider.signup_url}"))
        elif a.auth_type.startswith("oauth"):
            ready, hint = pc.auth_status(a)
            body.mount(Static("✓ signed in — ready." if ready
                              else f"Not signed in. {hint}"))
        elif a.auth_type == "none" and not is_custom:
            body.mount(Static("No auth needed — local server."))

    def _status(self, text: str) -> None:
        self.query_one("#auth-status", Static).update(text)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id != "auth-continue":
            return
        a = self._selected
        env_var = a.env_var
        if a.auth_type == "api_key":
            key = self.query_one("#auth-key", Input).value.strip()
            if env_var is None:  # custom — the operator named the env var
                env_var = self.query_one("#auth-envvar", Input).value.strip() or None
            ready, _ = pc.auth_status(a)
            if not key and not ready:
                self._status("Enter your API key to continue.")
                return
            if key and not env_var:
                self._status("Name the env var to store the key under.")
                return
            if key and env_var:
                config.set_env_secret(env_var, key)
        elif a.auth_type.startswith("oauth"):
            ready, hint = pc.auth_status(a)
            if not ready:
                self._status(f"Sign in first — {hint}")
                return
        base_url = None
        if self.provider.models_source.kind == "custom":
            base_url = self.query_one("#auth-baseurl", Input).value.strip()
            if not base_url:
                self._status("Enter a base_url for the custom provider.")
                return
        self.post_message(self.AuthConfigured(
            self.provider.id, a.auth_type, env_var, base_url,
        ))


__all__ = ["AuthStep"]
