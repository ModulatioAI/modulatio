"""Tests for TextEntryModal (Feng-Tui MEMORY overhaul).

A small overlay to add/edit a block of text: dismisses the (stripped) text on
Save, and None on Cancel/Escape or when the field is blank.
"""
from __future__ import annotations

from textual.app import App, ComposeResult
from textual.widgets import Button, Static, TextArea

from modulatio.tui.widgets.text_entry_modal import TextEntryModal


class _Host(App):
    def __init__(self) -> None:
        super().__init__()
        self.result: object = "unset"

    def get_css_variables(self) -> dict[str, str]:
        v = super().get_css_variables()
        v.setdefault("frame", "#6cb6e4")
        return v

    def compose(self) -> ComposeResult:
        yield Static("host")

    def open_modal(self, **kw) -> None:
        self.push_screen(TextEntryModal(**kw), self._capture)

    def _capture(self, value: object) -> None:
        self.result = value


async def test_prefills_and_saves_edited_text():
    app = _Host()
    async with app.run_test() as pilot:
        app.open_modal(title="Edit", initial="old text")
        await pilot.pause()
        modal = app.screen
        assert modal.query_one("#entry-text", TextArea).text == "old text"
        modal.query_one("#entry-text", TextArea).text = "new text"
        modal.query_one("#entry-save", Button).press()
        await pilot.pause()
        assert app.result == "new text"


async def test_cancel_returns_none():
    app = _Host()
    async with app.run_test() as pilot:
        app.open_modal(title="Add")
        await pilot.pause()
        modal = app.screen
        modal.query_one("#entry-text", TextArea).text = "discard me"
        modal.query_one("#entry-cancel", Button).press()
        await pilot.pause()
        assert app.result is None


async def test_blank_save_returns_none():
    app = _Host()
    async with app.run_test() as pilot:
        app.open_modal(title="Add")
        await pilot.pause()
        app.screen.query_one("#entry-save", Button).press()
        await pilot.pause()
        assert app.result is None
