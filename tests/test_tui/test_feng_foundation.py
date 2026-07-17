# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Feng-Tui foundation — themes register + cycle live; LOGS adopts MasterDetail.

Proves the reskin foundation: the three Feng-Tui variants register, amber is the
default, F2 (``action_cycle_theme``) re-resolves the design-system tokens LIVE, the
LOGS proof tab adopts the shared ``MasterDetail`` full-height divider, and the
must-have copy/paste actions are present.
"""
from textual.widgets import DataTable, TabbedContent

from modulatio.tui.app import ModulatioApp
from modulatio.tui.screens.logs import LogsScreen
from modulatio.tui.widgets.master_detail import MasterDetail

PROJECT_CODE = "FENG"


async def test_feng_themes_registered_and_amber_default():
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.theme == "feng-amber"
        assert {"feng-amber", "feng-green", "feng-cyan"} <= set(app.available_themes)
        assert app.current_theme.background == "#000000"
        assert app.current_theme.accent.lower() == "#ffc933"


async def test_theme_cycle_reresolves_accent_live():
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        before = app.current_theme.accent
        app.action_cycle_theme()
        await pilot.pause()
        assert app.theme == "feng-green"
        assert app.current_theme.accent != before
        # the variable actually re-resolved in the live stylesheet…
        assert app.get_css_variables()["accent"].lower() == "#7dff9c"
        # …and the frame chrome tracks the accent.
        assert app.get_css_variables()["frame"].lower() == "#7dff9c"
        # cycle wraps amber → green → cyan → red → purple → amber (W1)
        app.action_cycle_theme()
        await pilot.pause()
        assert app.theme == "feng-cyan"
        app.action_cycle_theme()
        await pilot.pause()
        assert app.theme == "feng-red"
        app.action_cycle_theme()
        await pilot.pause()
        assert app.theme == "feng-purple"
        app.action_cycle_theme()
        await pilot.pause()
        assert app.theme == "feng-amber"


async def test_logs_tab_adopts_master_detail_full_height_divider():
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        app.query_one(TabbedContent).active = "tab-logs"
        await pilot.pause()
        screen = app.query_one(LogsScreen)
        md = screen.query_one(MasterDetail)
        detail = md.query_one("#md-detail")
        # full-height divider: the detail pane carries a left border
        assert detail.styles.border_left[0] is not None
        # existing selectors still resolve (the reskin is layout-only)
        assert app.query_one("#logs-table", DataTable) is not None


async def test_copy_paste_actions_present():
    # Copy/paste across screens/tabs + to outside apps is a must-have in every
    # install — wired app-level (Ctrl+C / Ctrl+V, OS clipboard).
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert hasattr(app, "action_copy_text")
        assert hasattr(app, "action_paste")


async def test_f2_keybinding_cycles_theme():
    # Lock the operator-facing key hook (the action test isn't enough).
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.theme == "feng-amber"
        await pilot.press("f2")
        await pilot.pause()
        assert app.theme == "feng-green"


async def test_theme_persists_across_launches():
    # Cycling the variant is remembered; the NEXT launch reopens on it instead
    # of resetting to amber. (PREFS_FILE is tmp-isolated by the conftest fixture,
    # so this exercises the real save/restore round-trip hermetically.)
    app1 = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app1.run_test() as pilot:
        await pilot.pause()
        assert app1.theme == "feng-amber"   # empty prefs → amber default
        app1.action_cycle_theme()           # → feng-green, persisted
        await pilot.pause()
        assert app1.theme == "feng-green"

    # a fresh app instance restores the saved variant on mount
    app2 = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app2.run_test() as pilot:
        await pilot.pause()
        assert app2.theme == "feng-green"


async def test_unknown_saved_theme_falls_back_to_amber():
    # A stale/garbage saved value must not crash boot — it falls back to amber.
    from modulatio import preferences
    preferences.set_theme("feng-ultraviolet")  # not a registered variant
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.theme == "feng-amber"


async def test_f4_keybinding_flips_stream_without_error():
    # F4 (the remapped LEADER/TEAM flip) must dispatch without raising.
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("f4")
        await pilot.pause()


async def test_header_surfaces_active_variant():
    # The operator can see which variant is live.
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert "feng-tui · amber" in app.sub_title
        await pilot.press("f2")
        await pilot.pause()
        assert "feng-tui · green" in app.sub_title


# ── neon red + purple variants (Feng-Tui refinement arc, W1) ─────────────────


def test_five_variants_in_cycle_order():
    from modulatio.tui.feng_theme import FENG_THEME_NAMES
    assert FENG_THEME_NAMES == [
        "feng-amber", "feng-green", "feng-cyan", "feng-red", "feng-purple",
    ]


def test_red_variant_overrides_error_away_from_the_accent():
    """A red accent would swallow #FF5555 error text — the red variant carries
    an amber-alarm error so failures still jump out of a red screen."""
    from modulatio.tui.feng_theme import FENG_PURPLE, FENG_RED
    assert FENG_RED.error == "#FFB000"
    assert FENG_RED.error != FENG_RED.primary
    # non-red variants keep the terminal-red error
    assert FENG_PURPLE.error == "#FF5555"


def test_new_variants_carry_the_frame_variables():
    from modulatio.tui.feng_theme import FENG_PURPLE, FENG_RED
    for theme in (FENG_RED, FENG_PURPLE):
        assert theme.variables["frame"] == theme.primary
        assert theme.variables["frame-dim"] == theme.secondary
