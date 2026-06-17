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
        # cycle wraps amber → green → cyan → amber
        app.action_cycle_theme()
        await pilot.pause()
        assert app.theme == "feng-cyan"
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
    # Lock the operator-facing key hook (the action test isn't enough — Wild Bill).
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.theme == "feng-amber"
        await pilot.press("f2")
        await pilot.pause()
        assert app.theme == "feng-green"


async def test_f4_keybinding_flips_stream_without_error():
    # F4 (the remapped LEADER/TEAM flip) must dispatch without raising.
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("f4")
        await pilot.pause()


async def test_header_surfaces_active_variant():
    # The operator can see which variant is live (Lovecraft coherence note).
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert "feng-tui · amber" in app.sub_title
        await pilot.press("f2")
        await pilot.pause()
        assert "feng-tui · green" in app.sub_title
