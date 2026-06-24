"""Tests for the Configurator widget (Feng-Tui overhaul, step 1).

The configurator archetype (CONFIG·MODELS/AGENTS, PROJECTS): a persistent
registry list (#cfg-list, the doorway) + a SWAPPABLE companion pane
(#cfg-companion). Shares the full-height-divider LAYOUT with MasterDetail but
a DIFFERENT contract — the right pane is a flow state machine, not a render of
the selected row. The list stays mounted while the
companion swaps; the screen owns the flow state, the widget owns the layout.
"""
from __future__ import annotations

from textual.app import App, ComposeResult
from textual.containers import Vertical
from textual.widgets import Static

from modulatio.tui.widgets.configurator import Configurator


class _Host(App):
    def compose(self) -> ComposeResult:
        with Configurator():
            yield Vertical(Static("registry", id="reg"), id="cfg-list")
            yield Vertical(Static("keys view", id="keys"), id="cfg-companion")


async def test_configurator_composes_persistent_list_and_companion():
    app = _Host()
    async with app.run_test() as pilot:
        await pilot.pause()
        cfg = app.query_one(Configurator)
        assert cfg.query_one("#cfg-list")
        assert cfg.query_one("#cfg-companion")
        assert app.query_one("#reg")  # registry content present
        assert app.query_one("#keys")  # initial companion view present


async def test_swap_companion_replaces_only_the_companion_keeps_the_list():
    """The flow swaps the companion (keys -> provider wizard) while the
    registry list stays mounted — the doorway never flashes."""
    app = _Host()
    async with app.run_test() as pilot:
        await pilot.pause()
        cfg = app.query_one(Configurator)
        await cfg.swap_companion(Static("provider wizard", id="wizard"))
        await pilot.pause()
        assert app.query_one("#wizard")          # new companion view
        assert not app.query("#keys")            # old companion view gone
        assert app.query_one("#reg")             # registry list persists
