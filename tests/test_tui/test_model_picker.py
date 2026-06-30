"""Tests for the Configuration tab's ModelPicker (add-model flow, step 3)."""
from __future__ import annotations

from textual.app import App, ComposeResult
from textual.widgets import Input, OptionList

from modulatio import provider_catalog as pc
from modulatio.tui.widgets.model_picker import ModelPicker

FIXTURE = [
    pc.CatalogModel(id="openrouter/free", name="free", provider_id="openrouter",
                    is_free=True, modality="text"),
    pc.CatalogModel(id="anthropic/claude-opus-4.8", name="opus",
                    provider_id="openrouter", modality="text"),
    pc.CatalogModel(id="black-forest/flux-image", name="flux",
                    provider_id="openrouter", modality="image"),
]


class _Host(App):
    def __init__(self, provider, **kw) -> None:
        super().__init__()
        self._provider = provider
        self._kw = kw
        self.chosen: list[tuple] = []

    def get_css_variables(self) -> dict[str, str]:
        v = super().get_css_variables()
        v.setdefault("frame", "#6cb6e4")
        v.setdefault("frame-dim", "#3f6d8c")
        return v

    def compose(self) -> ComposeResult:
        yield ModelPicker(self._provider, id="mp", **self._kw)

    def on_model_picker_model_chosen(self, e: ModelPicker.ModelChosen) -> None:
        self.chosen.append((e.provider_id, e.model_id))


async def _wait_options(pilot, app) -> OptionList:
    ol = app.query_one("#mp-list", OptionList)
    for _ in range(60):
        await pilot.pause(0.05)
        if ol.option_count:
            break
    return ol


async def test_lists_text_models_free_flagged_image_filtered(monkeypatch):
    monkeypatch.setattr(pc, "fetch_models", lambda p, **k: FIXTURE)
    app = _Host(pc.OPENROUTER)
    async with app.run_test() as pilot:
        await pilot.pause()
        ol = await _wait_options(pilot, app)
        labels = {
            ol.get_option_at_index(i).id: str(ol.get_option_at_index(i).prompt)
            for i in range(ol.option_count)
        }
        assert "openrouter/free" in labels
        assert "anthropic/claude-opus-4.8" in labels
        assert "black-forest/flux-image" not in labels  # image not in text picker
        assert "[FREE]" in labels["openrouter/free"]
        assert "[FREE]" not in labels["anthropic/claude-opus-4.8"]


async def test_selecting_a_model_posts_model_chosen(monkeypatch):
    monkeypatch.setattr(pc, "fetch_models", lambda p, **k: FIXTURE)
    app = _Host(pc.OPENROUTER)
    async with app.run_test() as pilot:
        await pilot.pause()
        ol = await _wait_options(pilot, app)
        ol.focus()
        ol.highlighted = 0
        await pilot.press("enter")
        await pilot.pause()
        assert len(app.chosen) == 1
        assert app.chosen[0][0] == "openrouter"


async def test_search_filters_to_matches(monkeypatch):
    monkeypatch.setattr(pc, "fetch_models", lambda p, **k: FIXTURE)
    app = _Host(pc.OPENROUTER)
    async with app.run_test() as pilot:
        await pilot.pause()
        ol = await _wait_options(pilot, app)
        app.query_one("#mp-search", Input).value = "opus"
        await pilot.pause()
        ids = {ol.get_option_at_index(i).id for i in range(ol.option_count)}
        assert ids == {"anthropic/claude-opus-4.8"}


async def test_local_server_down_yields_no_models(monkeypatch):
    monkeypatch.setattr(pc, "fetch_models", lambda p, **k: [])
    app = _Host(pc.OLLAMA_LOCAL)
    async with app.run_test() as pilot:
        await pilot.pause()
        mp = app.query_one("#mp", ModelPicker)
        for _ in range(40):
            await pilot.pause(0.05)
        assert mp._models == []  # empty, no crash; status shows the hint


async def test_custom_types_the_model_id():
    app = _Host(pc.CUSTOM, base_url="https://host/v1")
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one("#mp-custom-id", Input).value = "my-model-v1"
        await pilot.pause()
        await pilot.click("#mp-custom-go")
        await pilot.pause()
        assert app.chosen == [("custom", "my-model-v1")]


def test_listing_key_falls_back_to_oauth_token(monkeypatch):
    """xAI OAuth has no API-key env var, so the picker must reach the live
    /models list via the selected auth strategy's token (the Grok CLI
    bearer) — otherwise the picklist comes back empty."""
    from modulatio import auth_strategies

    monkeypatch.delenv("XAI_API_KEY", raising=False)

    class _Strat:
        def load_token(self):
            return "grok-oauth-tok"

    monkeypatch.setattr(auth_strategies, "build_strategy", lambda *a, **k: _Strat())
    mp = ModelPicker(pc.XAI, env_var=None, auth_type="oauth_xai")
    assert mp._listing_key() == "grok-oauth-tok"


def test_listing_key_prefers_env_api_key(monkeypatch):
    """An API-key provider still reads its env var — OAuth fallback only
    kicks in when no key is present."""
    monkeypatch.setenv("XAI_API_KEY", "xai-realkey")
    mp = ModelPicker(pc.XAI, env_var="XAI_API_KEY", auth_type="api_key")
    assert mp._listing_key() == "xai-realkey"
