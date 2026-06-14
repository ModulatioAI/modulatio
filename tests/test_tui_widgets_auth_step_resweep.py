"""Re-sweep regression for AuthStep._render_body (Finding 1, LOW/race).

_render_body awaits body.remove_children() (yielding the event loop) and then
mounts a body that includes id=auth-key. The old code issued the mounts
un-awaited, so two RadioSet.Changed events firing in quick succession could
both pass the remove and both mount id=auth-key -> DuplicateIds. The fix
collects the children and awaits a single body.mount(*widgets) after the
remove, keeping the rebuild atomic with respect to handler re-entry.

These tests are kept out of the existing test_tui/test_auth_step.py module so
the re-sweep can't collide with the standing suite.
"""
from __future__ import annotations

import asyncio

from textual.app import App, ComposeResult
from textual.widgets import Input

from modulatio import config
from modulatio import provider_catalog as pc
from modulatio import provider_keys
from modulatio.tui.widgets.auth_step import AuthStep


class _Host(App):
    def __init__(self, provider: pc.Provider) -> None:
        super().__init__()
        self.provider = provider

    def get_css_variables(self) -> dict[str, str]:
        v = super().get_css_variables()
        v.setdefault("frame", "#6cb6e4")
        v.setdefault("frame-dim", "#3f6d8c")
        return v

    def compose(self) -> ComposeResult:
        yield AuthStep(self.provider, id="auth")


def _isolate_keys(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_keys, "LABELS_FILE", tmp_path / "labels.json")
    monkeypatch.setattr(provider_keys, "PINS_FILE", tmp_path / "pins.json")


async def test_concurrent_render_body_never_duplicates_auth_key(tmp_path, monkeypatch):
    """Two interleaved _render_body() calls must leave exactly one #auth-key.

    Without the fix the second coroutine passes the awaited remove_children()
    while the first's un-awaited mounts are still pending, so both mount
    id=auth-key -> two widgets (a DuplicateIds hazard). With the fix the rebuild
    after the remove is a single awaited mount, so the body has one auth-key.
    """
    _isolate_keys(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "set_env_secret", lambda n, v: None)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    # Anthropic exposes oauth + api_key, so the auth-method RadioSet exists and
    # the api_key branch mounts id=auth-key.
    app = _Host(pc.ANTHROPIC)
    async with app.run_test() as pilot:
        await pilot.pause()
        step = app.query_one("#auth", AuthStep)
        # select the api_key option (the one that mounts #auth-key)
        api_idx = next(
            i for i, a in enumerate(step.provider.auth_options)
            if a.auth_type == "api_key"
        )
        step._selected = step.provider.auth_options[api_idx]
        # fire two rebuilds concurrently — they interleave at the awaited remove
        await asyncio.gather(step._render_body(), step._render_body())
        await pilot.pause()
        keys = step.query("#auth-key")
        assert len(keys) == 1, f"expected one #auth-key, found {len(keys)}"


async def test_render_body_still_builds_the_api_key_field(tmp_path, monkeypatch):
    """The collect-then-mount refactor must preserve the rendered fields."""
    _isolate_keys(tmp_path, monkeypatch)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    app = _Host(pc.OPENROUTER)
    async with app.run_test() as pilot:
        await pilot.pause()
        # empty pool, single api_key option -> key field present and mountable
        assert len(app.query("#auth-key")) == 1
        app.query_one("#auth-key", Input).value = "sk-x"
        assert app.query_one("#auth-key", Input).value == "sk-x"
