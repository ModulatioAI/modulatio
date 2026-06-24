"""Tests for StatusLampRow (Feng-Tui overhaul, step 1).

One dim console-chrome row of glyph+WORD lamps. **Event-sink, not a poller**:
the app calls ``set_lamps(...)`` on state change; the
elapsed timer is a TUI-only ``set_interval`` the widget owns, started/stopped
on the ``running`` lamp. Lamp DATA comes from existing TUI state — the widget
holds no engine poll.
"""
from __future__ import annotations

from textual.app import App, ComposeResult
from textual.widgets import Static

from modulatio.tui.widgets.status_lamp_row import StatusLampRow


class _Host(App):
    def compose(self) -> ComposeResult:
        yield StatusLampRow()


async def test_set_lamps_updates_only_provided_lamps():
    """Event-sink: set_lamps with a subset updates those lamps (glyph+word),
    leaves the rest. None = leave unchanged."""
    app = _Host()
    async with app.run_test() as pilot:
        await pilot.pause()
        row = app.query_one(StatusLampRow)
        row.set_lamps(mods=3, qc=1, tickets=2)
        await pilot.pause()
        assert "3" in str(app.query_one("#lamp-squad", Static).render())
        assert "2" in str(app.query_one("#lamp-tickets", Static).render())


async def test_running_lamp_reads_glyph_and_word():
    """States are glyph + WORD (accessibility), never colour alone."""
    app = _Host()
    async with app.run_test() as pilot:
        await pilot.pause()
        row = app.query_one(StatusLampRow)
        row.set_lamps(running=True)
        await pilot.pause()
        text = str(app.query_one("#lamp-run", Static).render())
        assert "running" in text.lower()
        # idle is also glyph+word
        row.set_lamps(running=False)
        await pilot.pause()
        assert "idle" in str(app.query_one("#lamp-run", Static).render()).lower()


async def test_running_starts_and_stops_the_elapsed_timer():
    """The elapsed timer is owned by the widget (TUI render concern), armed on
    running=True and disarmed on running=False — not a free-running poller."""
    app = _Host()
    async with app.run_test() as pilot:
        await pilot.pause()
        row = app.query_one(StatusLampRow)
        assert row._elapsed_timer is None
        row.set_lamps(running=True)
        await pilot.pause()
        assert row._elapsed_timer is not None  # armed
        row.set_lamps(running=False)
        await pilot.pause()
        assert row._elapsed_timer is None  # disarmed
