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


# ─── Attention blink (Group B: replaces the MSG/PROBLEM beacons) ─────────────


async def test_request_attention_arms_blink_and_lights_lamp():
    """request_attention starts the blink timer; a blink tick lights the
    relevant lamp (attention-grab from another tab)."""
    app = _Host()
    async with app.run_test() as pilot:
        await pilot.pause()
        row = app.query_one(StatusLampRow)
        assert row._blink_timer is None
        row.request_attention("leader")
        await pilot.pause()
        assert row._blink_timer is not None          # armed
        assert "leader" in row._attention
        row._tick_blink()                            # force a visible phase
        lamp = app.query_one("#lamp-leader", Static)
        # at least one phase lights the lamp (glyph+word unchanged, colour cue)
        assert lamp.has_class("-lit") or row._blink_phase is not None


async def test_clear_attention_disarms_when_none_left():
    """Clearing the last attention lamp stops the blink timer and unlights it
    (you flipped to LEADER → the beacon rests)."""
    app = _Host()
    async with app.run_test() as pilot:
        await pilot.pause()
        row = app.query_one(StatusLampRow)
        row.request_attention("leader")
        row.request_attention("tickets")
        await pilot.pause()
        row.clear_attention("leader")
        assert row._blink_timer is not None          # tickets still pending
        row.clear_attention("tickets")
        await pilot.pause()
        assert row._blink_timer is None              # all clear → disarmed
        assert not app.query_one("#lamp-leader", Static).has_class("-lit")
        assert not app.query_one("#lamp-tickets", Static).has_class("-lit")


async def test_partial_squad_update_keeps_the_other_value_from_state():
    """A mods-only update must not clobber qc (and vice-versa) — the widget
    renders the squad lamp from held fields, not by parsing its own render."""
    app = _Host()
    async with app.run_test() as pilot:
        await pilot.pause()
        row = app.query_one(StatusLampRow)
        row.set_lamps(mods=3, qc=2)
        await pilot.pause()
        row.set_lamps(mods=5)  # qc left None — must stay 2
        await pilot.pause()
        text = str(app.query_one("#lamp-squad", Static).render())
        assert "5 mods" in text and "2 qc" in text
        row.set_lamps(qc=4)  # mods left None — must stay 5
        await pilot.pause()
        text = str(app.query_one("#lamp-squad", Static).render())
        assert "5 mods" in text and "4 qc" in text
