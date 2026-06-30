# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""AuthStep — step 2 of the Configuration tab's add-model flow.

After a provider is chosen, this collects whatever auth it needs and nothing
more — the operator types only the key:

  - **api_key**   → the model uses the provider's shared key pool. If the pool
    already has a key, nothing to enter — just Continue; you can optionally add
    another key to the pool. If the provider has no key yet, a masked field
    collects the first one (with the signup URL). Pinning a key to a specific
    model is a separate, optional action in the Keys manager.
  - **oauth**     → a readiness line (``signed in ✓`` or the setup hint, e.g.
    "run `claude login`") + a Recheck button. Beta options say so.
  - **none**      → local server, nothing to enter.
  - **custom**    → a base_url field (the operator supplies the endpoint).

A provider with more than one option (key / OAuth) shows a method selector.
On success it posts ``AuthConfigured`` so the screen advances to the model
picker.
"""
from __future__ import annotations

import asyncio

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.message import Message
from textual.widgets import (
    Button,
    Input,
    RadioButton,
    RadioSet,
    Static,
)

from modulatio import config
from modulatio import provider_catalog as pc
from modulatio import provider_keys


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
            pool: bool = False,
        ) -> None:
            self.provider_id = provider_id
            self.auth_type = auth_type
            self.env_var = env_var
            self.base_url = base_url
            self.pool = pool
            super().__init__()

    def __init__(self, provider: pc.Provider, **kwargs) -> None:
        super().__init__(**kwargs)
        self.provider = provider
        self._selected = provider.auth_options[0]
        # re-sweep (Finding 1): serialize body rebuilds. remove_children() +
        # mount() can't be made atomic on their own (each awaits, yielding the
        # loop), so two fast RadioSet.Changed events could interleave and mount
        # duplicate ids (auth-key, ...) -> DuplicateIds. The lock makes each
        # rebuild run to completion before the next starts.
        self._render_lock = asyncio.Lock()

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
        # re-sweep (Finding 1): serialize so a rebuild runs to completion before
        # the next begins. remove_children() + mount() each yield the loop, so
        # without the lock two fast RadioSet.Changed events interleave and mount
        # duplicate ids -> DuplicateIds. Collect the children first, then under
        # the lock do remove + a single awaited batch mount.
        a = self._selected
        is_custom = self.provider.models_source.kind == "custom"
        widgets: list = []
        if is_custom:
            widgets.append(Input(
                placeholder="base_url, e.g. https://host/v1", id="auth-baseurl",
            ))
        if a.auth_type == "api_key":
            if a.env_var is None:  # custom — name the env var + enter the key
                widgets.append(Input(
                    placeholder="env var name, e.g. MYPROVIDER_API_KEY",
                    id="auth-envvar"))
                widgets.append(Input(
                    password=True, placeholder="paste your API key",
                    id="auth-key"))
            else:
                # The model draws from the provider's SHARED POOL (rotate +
                # 429 failover). The pool = the provider's set, unpinned keys.
                pool = [s for s in provider_keys.list_keys(a.env_var)
                        if s["is_set"] and not s["pinned_to"]]
                if pool:
                    widgets.append(Static(
                        f"✓ {len(pool)} key(s) in this provider's shared pool — "
                        "this model uses them (rotate + failover). Just "
                        "Continue, or add another key below."))
                else:
                    any_keys = bool(provider_keys.list_keys(a.env_var))
                    widgets.append(Static(
                        "This provider's keys are all pinned to other models — "
                        "add a key for the shared pool:" if any_keys else
                        f"Add an API key for this provider  →  saved as "
                        f"{a.env_var}:"))
                # add a key — optional when the pool has one, required when not.
                widgets.append(Input(
                    placeholder="label this key (optional), e.g. backup",
                    id="auth-keylabel"))
                hint = (
                    "paste a NEW key to add to the pool, or leave blank to use it"
                    if pool
                    else f"paste your API key  →  saved as {a.env_var}"
                )
                widgets.append(Input(password=True, placeholder=hint, id="auth-key"))
            if self.provider.signup_url:
                widgets.append(Static(f"Need one? {self.provider.signup_url}"))
        elif a.auth_type.startswith("oauth"):
            ready, hint = pc.auth_status(a)
            if a.beta:
                # A beta OAuth method can be signed-in yet not actually usable
                # (e.g. Grok: the CLI token isn't accepted by the xAI API) —
                # show its caveat, never a misleading "signed in — ready".
                widgets.append(Static(a.oauth_hint or "Beta — not functional yet."))
            else:
                widgets.append(Static("✓ signed in — ready." if ready
                                      else f"Not signed in. {hint}"))
        elif a.auth_type == "none" and not is_custom:
            widgets.append(Static("No auth needed — local server."))
        async with self._render_lock:
            body = self.query_one("#auth-body", Vertical)
            await body.remove_children()
            if widgets:
                await body.mount(*widgets)

    def _status(self, text: str) -> None:
        self.query_one("#auth-status", Static).update(text)

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id != "auth-continue":
            return
        a = self._selected
        env_var = a.env_var
        pool = False
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
                # The model uses the provider's SHARED POOL. Adding a key here
                # just grows that pool; a blank field means "use what's there".
                pool_now = [s for s in provider_keys.list_keys(a.env_var)
                            if s["is_set"] and not s["pinned_to"]]
                if key:  # add a new key to the pool (optional label)
                    label = self.query_one("#auth-keylabel", Input).value.strip()
                    provider_keys.add_key(a.env_var, key, label or None)
                elif not pool_now:
                    self._status("Add an API key to continue.")
                    return
                env_var = a.env_var  # the base var anchors the provider's pool
                pool = True
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
            self.provider.id, a.auth_type, env_var, base_url, pool,
        ))


__all__ = ["AuthStep"]
