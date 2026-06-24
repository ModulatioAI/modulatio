"""Tests for the shared ControlsRow widget (Feng-Tui overhaul, step 1).

ControlsRow is the thin controls strip atop a list screen — sort / filter /
counts / search — opt-in per screen via constructor kwargs (default off, so a
screen renders exactly the controls it needs). Stateless: it owns the layout;
the screen owns the policy + state (web-UI portability).
"""
from __future__ import annotations

from textual.app import App, ComposeResult
from textual.widgets import Input, Static

from modulatio.tui.widgets.controls_row import ControlsRow


class _Host(App):
    def __init__(self, **kw):
        super().__init__()
        self._kw = kw

    def compose(self) -> ComposeResult:
        yield ControlsRow(**self._kw)


async def test_controls_row_renders_only_enabled_controls():
    """Default-off kwargs: a screen opts in to exactly what it needs, so an
    unused control is absent (not hidden) — no fork, no stray widget."""
    app = _Host(counts=True, search=True)  # no sort, no filter
    async with app.run_test() as pilot:
        await pilot.pause()
        row = app.query_one(ControlsRow)
        assert row.query("#controls-counts")
        assert row.query("#controls-search")
        assert not row.query("#controls-sort")
        assert not row.query("#controls-filter")


async def test_controls_row_search_has_the_pinned_id():
    """The search Input id is fixed in the widget (#controls-search) so every
    screen's on_input_submitted finds it the same way."""
    app = _Host(search=True, search_placeholder="/ find a ticket…")
    async with app.run_test() as pilot:
        await pilot.pause()
        inp = app.query_one("#controls-search", Input)
        assert inp.placeholder == "/ find a ticket…"


async def test_controls_row_set_counts_updates_display():
    """The screen feeds display strings in (stateless widget); set_counts
    updates the counts cell."""
    app = _Host(counts=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        row = app.query_one(ControlsRow)
        row.set_counts("18 logs · 3 unsent")
        await pilot.pause()
        assert "18 logs" in str(app.query_one("#controls-counts", Static).render())
