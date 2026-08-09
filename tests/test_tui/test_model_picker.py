"""Tests for the Configuration tab's ModelPicker (add-model flow, step 3)."""
from __future__ import annotations

from textual.app import App, ComposeResult
from textual.widgets import Input, OptionList

from modulatio import provider_catalog as pc
from modulatio.tui.widgets.model_picker import ModelPicker
import pytest
from textual.widgets.option_list import DuplicateID

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
    """xAI OAuth has no API-key env var, so the listing resolver (the engine
    seam the picker's fetch rides) must reach the live /models list via the
    selected auth strategy's token — otherwise the picklist comes back
    empty."""
    from modulatio import auth_strategies

    monkeypatch.delenv("XAI_API_KEY", raising=False)

    class _Strat:
        def load_token(self):
            return "grok-oauth-tok"

    monkeypatch.setattr(auth_strategies, "build_strategy", lambda *a, **k: _Strat())
    assert pc.listing_key(env_var=None, auth_type="oauth_xai") == "grok-oauth-tok"


def test_listing_key_prefers_env_api_key(monkeypatch):
    """An API-key provider still reads its env var — OAuth fallback only
    kicks in when no key is present."""
    monkeypatch.setenv("XAI_API_KEY", "xai-realkey")
    assert pc.listing_key(env_var="XAI_API_KEY", auth_type="api_key") == "xai-realkey"


# ═══ fold: test_tui_widgets_model_picker_resweep_r3.py ═══
# Re-sweep R3 regression: ModelPicker must not crash on duplicate model ids.
#
# ``ModelPicker._populate`` added one OptionList
# ``Option`` per model in the listing. The listing aggregates a provider's
# ``models_source`` + ``extra_sources`` (via ``pc.fetch_models``), which can
# carry the same id twice. Textual's ``OptionList.add_option`` raises
# ``DuplicateID`` on a repeated id — crashing the picker on a noisy feed. The fix
# dedups by first occurrence in ``_populate`` (order preserved), which also
# defends the ``search`` / ``curated_default`` outputs.


def _cloud_provider() -> pc.Provider:
    return pc.get_provider("openrouter")


class _PopulatePicker(ModelPicker):
    """Drive ``_populate`` against a real (unmounted) OptionList without
    spinning up a full Textual App — an unmounted OptionList still enforces
    unique ids and raises DuplicateID on a repeat, mirroring the live crash."""

    def __init__(self, provider: pc.Provider) -> None:
        super().__init__(provider)
        self._opt_list = OptionList()

    def query_one(self, selector, *args, **kwargs):  # type: ignore[override]
        return self._opt_list


def _dup_models() -> list[pc.CatalogModel]:
    # Same id appearing twice — what aggregation across sources produces.
    return [
        pc.CatalogModel(id="vendor/dup", name="Dup A", provider_id="openrouter"),
        pc.CatalogModel(id="vendor/dup", name="Dup B", provider_id="openrouter"),
        pc.CatalogModel(id="vendor/other", name="Other", provider_id="openrouter"),
    ]


def test_populate_dedups_duplicate_ids_search_path():
    """Search path: duplicate ids must not raise DuplicateID."""
    picker = _PopulatePicker(_cloud_provider())
    picker._models = _dup_models()

    # query matches both duplicates; without dedup the 2nd add_option raises.
    picker._populate("vendor/dup")

    ids = [opt.id for opt in picker._opt_list._options]
    assert ids == ["vendor/dup"]


def test_populate_dedups_duplicate_ids_default_path():
    """Curated-default (unsearched) path: duplicate ids must not raise."""
    picker = _PopulatePicker(_cloud_provider())
    picker._models = _dup_models()

    picker._populate()  # no query → curated_default

    ids = [opt.id for opt in picker._opt_list._options]
    # both unique ids present, each exactly once
    assert ids.count("vendor/dup") == 1
    assert ids.count("vendor/other") == 1


def test_populate_preserves_first_occurrence_order():
    """Dedup keeps the FIRST occurrence and preserves listing order."""
    picker = _PopulatePicker(_cloud_provider())
    picker._models = [
        pc.CatalogModel(id="a", name="A", provider_id="openrouter"),
        pc.CatalogModel(id="b", name="B", provider_id="openrouter"),
        pc.CatalogModel(id="a", name="A again", provider_id="openrouter"),
        pc.CatalogModel(id="c", name="C", provider_id="openrouter"),
    ]
    # search "" returns list(models); but we want the search path with a query
    # that matches all four — use a query common to all ids' names.
    picker._populate("a")  # matches ids 'a' (x2) by id; name 'A again'

    ids = [opt.id for opt in picker._opt_list._options]
    assert ids == ["a"]  # single 'a', no DuplicateID


def test_unmounted_optionlist_actually_raises_without_dedup():
    """Anchor: confirm the underlying primitive raises so the dedup is load-bearing."""
    ol = OptionList()
    from textual.widgets.option_list import Option

    ol.add_option(Option("first", id="dup"))
    with pytest.raises(DuplicateID):
        ol.add_option(Option("second", id="dup"))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))


async def test_custom_with_base_url_probes_and_lists(monkeypatch):
    """A custom provider with an operator-supplied base_url is PROBED — a
    reachable OpenAI-compatible endpoint fills the pick-list, with the typed
    id still available beneath it."""
    seen = {}

    def _fetch(p, **k):
        seen.update(k)
        return [pc.CatalogModel(id="my-local-33b", name="m",
                                provider_id="custom", modality="text")]

    monkeypatch.setattr(pc, "fetch_models", _fetch)
    app = _Host(pc.CUSTOM, base_url="https://host/v1")
    async with app.run_test() as pilot:
        await pilot.pause()
        ol = await _wait_options(pilot, app)
        assert ol.get_option_at_index(0).id == "my-local-33b"
        assert seen.get("base_url") == "https://host/v1"   # probed THE endpoint
        # the sanctioned typed path stays present alongside the probe result
        assert app.query_one("#mp-custom-id", Input)


async def test_custom_unreachable_endpoint_still_types_the_id(monkeypatch):
    """The probe failing (unreachable/incompatible endpoint) must not block
    custom's typed-id path — status says so, the input still submits."""
    monkeypatch.setattr(pc, "fetch_models", lambda p, **k: [])
    app = _Host(pc.CUSTOM, base_url="https://host/v1")
    async with app.run_test() as pilot:
        await pilot.pause()
        mp = app.query_one("#mp", ModelPicker)
        for _ in range(20):
            await pilot.pause(0.05)
        assert mp._models == []  # probe came back empty, no crash
        app.query_one("#mp-custom-id", Input).value = "my-model-v1"
        await pilot.pause()
        await pilot.click("#mp-custom-go")
        await pilot.pause()
        assert app.chosen == [("custom", "my-model-v1")]
