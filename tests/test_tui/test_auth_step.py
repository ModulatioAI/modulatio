"""Tests for the Configuration tab's AuthStep (add-model flow, step 2)."""
from __future__ import annotations

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
        self.configured: list[tuple] = []

    def get_css_variables(self) -> dict[str, str]:
        v = super().get_css_variables()
        v.setdefault("frame", "#6cb6e4")
        v.setdefault("frame-dim", "#3f6d8c")
        return v

    def compose(self) -> ComposeResult:
        yield AuthStep(self.provider, id="auth")

    def on_auth_step_auth_configured(self, e: AuthStep.AuthConfigured) -> None:
        self.configured.append((e.provider_id, e.auth_type, e.env_var, e.base_url))


async def test_api_key_saves_to_env_and_advances(monkeypatch):
    saved: dict[str, str] = {}
    monkeypatch.setattr(config, "set_env_secret", lambda n, v: saved.update({n: v}))
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    app = _Host(pc.OPENROUTER)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one("#auth-key", Input).value = "sk-test"
        await pilot.click("#auth-continue")
        await pilot.pause()
        assert saved == {"OPENROUTER_API_KEY": "sk-test"}
        assert app.configured == [
            ("openrouter", "api_key", "OPENROUTER_API_KEY", None)
        ]


async def test_api_key_blocks_when_missing_and_not_set(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    app = _Host(pc.OPENROUTER)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.click("#auth-continue")
        await pilot.pause()
        assert app.configured == []  # no key, not already set → blocked


async def test_local_none_advances_with_no_input():
    app = _Host(pc.OLLAMA_LOCAL)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.click("#auth-continue")
        await pilot.pause()
        assert app.configured == [("ollama_local", "none", None, None)]


async def test_custom_advances_with_a_base_url():
    app = _Host(pc.CUSTOM)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one("#auth-baseurl", Input).value = "https://host/v1"
        await pilot.pause()
        await pilot.click("#auth-continue")
        await pilot.pause()
        assert app.configured == [("custom", "none", None, "https://host/v1")]


async def test_custom_blocks_without_a_base_url():
    app = _Host(pc.CUSTOM)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.click("#auth-continue")
        await pilot.pause()
        assert app.configured == []  # base_url missing → blocked


async def test_api_key_adds_a_labeled_numbered_key(monkeypatch):
    monkeypatch.setattr(provider_keys, "list_keys", lambda b: [])  # none yet
    added: dict = {}

    def fake_add(base, value, label=None):
        added.update(base=base, value=value, label=label)
        return {"index": 1, "env_var": base, "label": label, "is_set": True}

    monkeypatch.setattr(provider_keys, "add_key", fake_add)
    app = _Host(pc.GOOGLE)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one("#auth-keylabel", Input).value = "images"
        app.query_one("#auth-key", Input).value = "AIza-secret"
        await pilot.click("#auth-continue")
        await pilot.pause()
        assert added == {"base": "GEMINI_API_KEY", "value": "AIza-secret",
                         "label": "images"}
        assert app.configured == [("google", "api_key", "GEMINI_API_KEY", None)]


async def test_multi_key_uses_selected_default_when_no_new_key(monkeypatch):
    monkeypatch.setattr(provider_keys, "list_keys", lambda b: [
        {"index": 1, "env_var": "GEMINI_API_KEY", "label": "text", "is_set": True},
        {"index": 2, "env_var": "GEMINI_API_KEY_2", "label": "images",
         "is_set": True},
    ])
    app = _Host(pc.GOOGLE)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.query("#auth-keysel")  # picker shown for 2 keys
        await pilot.click("#auth-continue")  # blank key → use selected (#1)
        await pilot.pause()
        assert app.configured == [("google", "api_key", "GEMINI_API_KEY", None)]


async def test_oauth_not_signed_in_blocks_with_hint(monkeypatch):
    # force the Anthropic OAuth option to report not-ready
    monkeypatch.setattr(pc, "auth_status", lambda a, **k: (False, "run `claude login`"))
    app = _Host(pc.ANTHROPIC)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.click("#auth-continue")
        await pilot.pause()
        assert app.configured == []  # not signed in → blocked
