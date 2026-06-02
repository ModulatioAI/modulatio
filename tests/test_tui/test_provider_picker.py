"""Tests for the Configuration tab's ProviderPicker (add-model flow, step 1)."""
from __future__ import annotations

from textual.app import App, ComposeResult

from modulatio import provider_catalog as pc
from modulatio.tui.widgets.provider_picker import ProviderPicker


class _Host(App):
    def __init__(self) -> None:
        super().__init__()
        self.chosen: list[str] = []

    def get_css_variables(self) -> dict[str, str]:
        # mirror ModulatioApp — the light-blue frame vars the widgets use
        variables = super().get_css_variables()
        variables.setdefault("frame", "#6cb6e4")
        variables.setdefault("frame-dim", "#3f6d8c")
        return variables

    def compose(self) -> ComposeResult:
        yield ProviderPicker(id="pp")

    def on_provider_picker_provider_chosen(
        self, event: ProviderPicker.ProviderChosen
    ) -> None:
        self.chosen.append(event.provider_id)


async def test_lists_every_catalog_provider():
    app = _Host()
    async with app.run_test() as pilot:
        await pilot.pause()
        pp = app.query_one("#pp", ProviderPicker)
        assert pp.option_count == len(pc.list_providers())
        ids = {pp.get_option_at_index(i).id for i in range(pp.option_count)}
        assert ids == {p.id for p in pc.list_providers()}
        # the locals + custom are present alongside the cloud providers
        assert {"ollama_local", "lm_studio", "llama_cpp", "custom"} <= ids


async def test_choosing_a_provider_posts_its_id():
    app = _Host()
    async with app.run_test() as pilot:
        await pilot.pause()
        pp = app.query_one("#pp", ProviderPicker)
        pp.focus()
        pp.highlighted = 0
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        first_id = pc.list_providers()[0].id
        assert app.chosen == [first_id]


async def test_labels_carry_free_and_readiness_badges(monkeypatch):
    # OpenRouter has free models; with no key it reads "needs key"
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    app = _Host()
    async with app.run_test() as pilot:
        await pilot.pause()
        pp = app.query_one("#pp", ProviderPicker)
        labels = {
            pp.get_option_at_index(i).id: str(pp.get_option_at_index(i).prompt)
            for i in range(pp.option_count)
        }
        assert "free" in labels["openrouter"]
        assert "needs key" in labels["openrouter"]
        # a local provider shows no-auth setup, never "needs key"
        assert "needs key" not in labels["ollama_local"]
