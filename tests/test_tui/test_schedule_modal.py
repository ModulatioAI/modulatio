"""Tests for ScheduleModal (Feng-Tui overhaul, Group B).

The modal collects a cron schedule string for a Job Template: it dismisses the
entered string on Save (or Enter), and None on Cancel/Escape.
"""
from __future__ import annotations

from textual.app import App, ComposeResult
from textual.widgets import Button, Input

from modulatio.tui.widgets.schedule_modal import ScheduleModal


class _Host(App):
    def __init__(self) -> None:
        super().__init__()
        self.result: object = "unset"

    def get_css_variables(self) -> dict[str, str]:
        # The modal borders use the Feng-Tui theme var; supply it for the bare host.
        v = super().get_css_variables()
        v.setdefault("frame", "#6cb6e4")
        return v

    def compose(self) -> ComposeResult:
        yield Input(id="anchor")  # something to mount

    def open_modal(self) -> None:
        self.push_screen(ScheduleModal("daily-essay"), self._capture)

    def _capture(self, value: object) -> None:
        self.result = value


async def test_save_returns_the_entered_schedule():
    app = _Host()
    async with app.run_test() as pilot:
        app.open_modal()
        await pilot.pause()
        modal = app.screen  # the pushed ScheduleModal
        modal.query_one("#schedule-input", Input).value = "weekly mon 09:00"
        modal.query_one("#schedule-save", Button).press()
        await pilot.pause()
        assert app.result == "weekly mon 09:00"


async def test_cancel_returns_none():
    app = _Host()
    async with app.run_test() as pilot:
        app.open_modal()
        await pilot.pause()
        modal = app.screen
        modal.query_one("#schedule-input", Input).value = "daily 09:00"
        modal.query_one("#schedule-cancel", Button).press()
        await pilot.pause()
        assert app.result is None


async def test_blank_schedule_dismisses_none():
    """An empty field is not a schedule — Save dismisses None (no cron add)."""
    app = _Host()
    async with app.run_test() as pilot:
        app.open_modal()
        await pilot.pause()
        app.screen.query_one("#schedule-save", Button).press()
        await pilot.pause()
        assert app.result is None
