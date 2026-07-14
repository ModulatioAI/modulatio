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
import asyncio


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
    """A beta OAuth method may be signed-in yet non-functional — the body
    must show its caveat, never a misleading '✓ signed in — ready'. (Pinned
    against a synthetic beta provider: no CATALOG option is beta today — the
    xai one graduated when Modulatio grew its own sign-in — but the guard
    protects the next beta method.)"""
    import dataclasses as _dc

    from textual.widgets import RadioButton, Static

    beta_oauth = _dc.replace(
        pc.XAI.auth_options[1], beta=True,
        oauth_hint="Beta — not functional yet; use the api key path.",
    ) if _dc.is_dataclass(pc.AuthOption) else None
    if beta_oauth is None:  # AuthOption is a pydantic model
        beta_oauth = pc.XAI.auth_options[1].model_copy(update={
            "beta": True,
            "oauth_hint": "Beta — not functional yet; use the api key path.",
        })
        provider = pc.XAI.model_copy(update={
            "auth_options": [pc.XAI.auth_options[0], beta_oauth]})
    else:
        provider = _dc.replace(
            pc.XAI, auth_options=[pc.XAI.auth_options[0], beta_oauth])

    # pretend signed in so the old code path would have shown "ready"
    monkeypatch.setattr(pc, "auth_status", lambda a, **k: (True, ""))
    app = _Host(provider)  # options: [api_key, oauth(beta, synthetic)]
    async with app.run_test() as pilot:
        await pilot.pause()
        list(app.query(RadioButton))[1].value = True  # select the beta OAuth option
        await pilot.pause()
        body = " ".join(
            str(s.render()) for s in app.query("#auth-body").first().query(Static)
        ).lower()
        assert "ready" not in body
        assert "api key" in body  # steers to the working path


# ═══ fold: test_tui_widgets_auth_step_resweep.py ═══
# Re-sweep regression for AuthStep._render_body (Finding 1, LOW/race).
#
# _render_body awaits body.remove_children() (yielding the event loop) and then
# mounts a body that includes id=auth-key. The old code issued the mounts
# un-awaited, so two RadioSet.Changed events firing in quick succession could
# both pass the remove and both mount id=auth-key -> DuplicateIds. The fix
# collects the children and awaits a single body.mount(*widgets) after the
# remove, keeping the rebuild atomic with respect to handler re-entry.
#
# These tests are kept out of the existing test_tui/test_auth_step.py module so
# the re-sweep can't collide with the standing suite.




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
