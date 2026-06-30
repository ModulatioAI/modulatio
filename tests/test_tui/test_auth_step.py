"""Tests for the Configuration tab's AuthStep (add-model flow, step 2).

Post-pool-default: an api_key model uses the provider's SHARED POOL. If the
pool already has a key, you just Continue (no key prompt — the original gripe);
if it's empty you add the first key. Pinning a key to a model is a separate
action in the Keys manager (tested in test_configuration.py), not here.
"""
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
        self.last_event = e


def _isolate_keys(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_keys, "LABELS_FILE", tmp_path / "labels.json")
    monkeypatch.setattr(provider_keys, "PINS_FILE", tmp_path / "pins.json")


async def test_first_api_key_adds_to_the_pool_and_advances(tmp_path, monkeypatch):
    _isolate_keys(tmp_path, monkeypatch)
    saved: dict[str, str] = {}
    monkeypatch.setattr(config, "set_env_secret", lambda n, v: saved.update({n: v}))
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    app = _Host(pc.OPENROUTER)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one("#auth-key", Input).value = "sk-test"
        await pilot.click("#auth-continue")
        await pilot.pause()
        # the first key is saved as the base var; the model uses the pool
        assert saved == {"OPENROUTER_API_KEY": "sk-test"}
        assert app.configured == [
            ("openrouter", "api_key", "OPENROUTER_API_KEY", None)
        ]
        assert app.last_event.pool is True


async def test_existing_pool_advances_with_no_new_key(tmp_path, monkeypatch):
    """The original gripe, fixed: a key already exists → no prompt, just use it."""
    _isolate_keys(tmp_path, monkeypatch)
    monkeypatch.setenv("GEMINI_API_KEY", "already-here")
    app = _Host(pc.GOOGLE)
    async with app.run_test() as pilot:
        await pilot.pause()
        # leave the key field blank — the provider already has a pooled key
        await pilot.click("#auth-continue")
        await pilot.pause()
        assert app.configured == [("google", "api_key", "GEMINI_API_KEY", None)]
        assert app.last_event.pool is True


async def test_api_key_blocks_when_pool_empty_and_no_key(tmp_path, monkeypatch):
    _isolate_keys(tmp_path, monkeypatch)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    app = _Host(pc.OPENROUTER)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.click("#auth-continue")
        await pilot.pause()
        assert app.configured == []  # empty pool, no key → blocked


async def test_adding_a_key_to_an_existing_pool_grows_it(tmp_path, monkeypatch):
    _isolate_keys(tmp_path, monkeypatch)
    monkeypatch.setenv("GEMINI_API_KEY", "key-1")  # pool already has one
    added: dict = {}
    monkeypatch.setattr(
        provider_keys, "add_key",
        lambda base, value, label=None: added.update(
            base=base, value=value, label=label)
        or {"index": 2, "env_var": f"{base}_2", "label": label, "is_set": True,
            "pinned_to": []},
    )
    app = _Host(pc.GOOGLE)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one("#auth-keylabel", Input).value = "backup"
        app.query_one("#auth-key", Input).value = "key-2"
        await pilot.click("#auth-continue")
        await pilot.pause()
        assert added == {"base": "GEMINI_API_KEY", "value": "key-2",
                         "label": "backup"}
        # still a pool model anchored on the base var
        assert app.configured == [("google", "api_key", "GEMINI_API_KEY", None)]
        assert app.last_event.pool is True


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


async def test_oauth_not_signed_in_blocks_with_hint(monkeypatch):
    # force the Anthropic OAuth option to report not-ready
    monkeypatch.setattr(pc, "auth_status", lambda a, **k: (False, "run `claude login`"))
    app = _Host(pc.ANTHROPIC)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.click("#auth-continue")
        await pilot.pause()
        assert app.configured == []  # not signed in → blocked


async def test_beta_oauth_body_shows_caveat_not_misleading_ready(monkeypatch):
    """A beta OAuth method (Grok) may be signed-in yet non-functional — the
    body must show its caveat, never a misleading '✓ signed in — ready'."""
    from textual.widgets import RadioButton, Static

    # pretend signed in so the old code path would have shown "ready"
    monkeypatch.setattr(pc, "auth_status", lambda a, **k: (True, ""))
    app = _Host(pc.XAI)  # options: [api_key, oauth_xai(beta)]
    async with app.run_test() as pilot:
        await pilot.pause()
        list(app.query(RadioButton))[1].value = True  # select the beta OAuth option
        await pilot.pause()
        body = " ".join(
            str(s.render()) for s in app.query("#auth-body").first().query(Static)
        ).lower()
        assert "ready" not in body
        assert "api key" in body  # steers to the working path
