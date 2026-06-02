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
from modulatio import provider_keys
from modulatio.tui.widgets.key_selector import KeySelector


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

    async def on_mount(self) -> None:
        await self._render_body()

    async def on_radio_set_changed(self, event: RadioSet.Changed) -> None:
        # only the auth-method radio drives a re-render; ignore the key picker's
        if event.radio_set.id != "auth-method":
            return
        self._selected = self.provider.auth_options[event.radio_set.pressed_index]
        await self._render_body()

    async def _render_body(self) -> None:
        body = self.query_one("#auth-body", Vertical)
        await body.remove_children()
        a = self._selected
        is_custom = self.provider.models_source.kind == "custom"
        if is_custom:
            body.mount(Input(
                placeholder="base_url, e.g. https://host/v1", id="auth-baseurl",
            ))
        if a.auth_type == "api_key":
            if a.env_var is None:  # custom — name the env var + enter the key
                body.mount(Input(
                    placeholder="env var name, e.g. MYPROVIDER_API_KEY",
                    id="auth-envvar"))
                body.mount(Input(
                    password=True, placeholder="paste your API key",
                    id="auth-key"))
            else:
                slots = provider_keys.list_keys(a.env_var)
                if len(slots) > 1:  # pick which of N keys (redacted)
                    body.mount(Static("Which key?"))
                    body.mount(KeySelector(a.env_var, id="auth-keysel"))
                elif slots:
                    body.mount(Static(
                        f"✓ a key is set ({a.env_var}). Use it, or add "
                        "another below."))
                # add a key — optional when one exists, required when none.
                # A label lets you track it (e.g. text / images / web search).
                body.mount(Input(
                    placeholder="label this key (optional), e.g. images",
                    id="auth-keylabel"))
                hint = (
                    "paste a NEW API key, or leave blank to use the selected key"
                    if slots
                    else f"paste your API key  →  saved as {a.env_var}"
                )
                body.mount(Input(password=True, placeholder=hint, id="auth-key"))
                if slots:  # let the operator prune a stale/extra key
                    body.mount(Button(
                        "Remove selected key", id="auth-removekey",
                        variant="warning",
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

    async def _remove_selected_key(self) -> None:
        a = self._selected
        if a.env_var is None:
            return
        try:
            env_var = self.query_one("#auth-keysel", KeySelector).chosen_env_var
        except Exception:
            env_var = a.env_var  # only one key → the base var
        provider_keys.remove_key(env_var)
        await self._render_body()
        self._status(f"Removed key '{env_var}'.")

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "auth-removekey":
            await self._remove_selected_key()
            return
        if event.button.id != "auth-continue":
            return
        a = self._selected
        env_var = a.env_var
        if a.auth_type == "api_key":
            key = self.query_one("#auth-key", Input).value.strip()
            if a.env_var is None:  # custom — operator-named env var
                env_var = self.query_one("#auth-envvar", Input).value.strip() or None
                if not key:
                    self._status("Enter your API key to continue.")
                    return
                if not env_var:
                    self._status("Name the env var to store the key under.")
                    return
                config.set_env_secret(env_var, key)
            else:
                slots = provider_keys.list_keys(a.env_var)
                if key:  # store as a new numbered key (with its label)
                    label = self.query_one("#auth-keylabel", Input).value.strip()
                    slot = provider_keys.add_key(a.env_var, key, label or None)
                    env_var = slot["env_var"]
                elif slots:  # use the selected existing key
                    try:
                        env_var = self.query_one(
                            "#auth-keysel", KeySelector
                        ).chosen_env_var
                    except Exception:
                        env_var = a.env_var  # only one key → no picker
                else:
                    self._status("Enter your API key to continue.")
                    return
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
