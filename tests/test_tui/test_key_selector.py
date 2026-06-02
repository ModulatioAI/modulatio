"""Tests for the KeySelector (#1/#2/#N key picker, redacted)."""
from __future__ import annotations

from textual.app import App, ComposeResult
from textual.widgets import RadioSet

from modulatio import provider_keys
from modulatio.tui.widgets.key_selector import KeySelector


class _Host(App):
    def __init__(self, base: str) -> None:
        super().__init__()
        self.base = base

    def compose(self) -> ComposeResult:
        yield KeySelector(self.base, id="ks")


def _slot(i, ev, label):
    return {"index": i, "env_var": ev, "label": label, "is_set": True}


async def test_no_keys_no_picker_defaults_to_base(monkeypatch):
    monkeypatch.setattr(provider_keys, "list_keys", lambda b: [])
    app = _Host("NEWPROV_KEY")
    async with app.run_test() as pilot:
        await pilot.pause()
        ks = app.query_one("#ks", KeySelector)
        assert not ks.has_choice
        assert not app.query("#key-radio")  # nothing rendered
        assert ks.chosen_env_var == "NEWPROV_KEY"


async def test_single_key_no_picker(monkeypatch):
    monkeypatch.setattr(
        provider_keys, "list_keys",
        lambda b: [_slot(1, "GEMINI_API_KEY", None)],
    )
    app = _Host("GEMINI_API_KEY")
    async with app.run_test() as pilot:
        await pilot.pause()
        ks = app.query_one("#ks", KeySelector)
        assert not ks.has_choice
        assert ks.chosen_env_var == "GEMINI_API_KEY"


async def test_multi_key_picker_defaults_to_one(monkeypatch):
    monkeypatch.setattr(provider_keys, "list_keys", lambda b: [
        _slot(1, "GEMINI_API_KEY", "text"),
        _slot(2, "GEMINI_API_KEY_2", "images"),
        _slot(3, "GEMINI_API_KEY_3", "web search"),
    ])
    app = _Host("GEMINI_API_KEY")
    async with app.run_test() as pilot:
        await pilot.pause()
        ks = app.query_one("#ks", KeySelector)
        assert ks.has_choice
        assert app.query("#key-radio")
        assert ks.chosen_env_var == "GEMINI_API_KEY"  # #1 pre-selected


async def test_selecting_key_two_changes_env_var(monkeypatch):
    monkeypatch.setattr(provider_keys, "list_keys", lambda b: [
        _slot(1, "GEMINI_API_KEY", "text"),
        _slot(2, "GEMINI_API_KEY_2", "images"),
    ])
    app = _Host("GEMINI_API_KEY")
    async with app.run_test() as pilot:
        await pilot.pause()
        ks = app.query_one("#ks", KeySelector)
        rs = app.query_one("#key-radio", RadioSet)
        rs.focus()
        await pilot.press("down")
        await pilot.press("enter")
        await pilot.pause()
        assert ks.chosen_env_var == "GEMINI_API_KEY_2"
