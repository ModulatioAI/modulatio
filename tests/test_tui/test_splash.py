# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""SplashScreen — the Feng-Tui boot frame loads the wordmark + tagline.

Proves the boot art renders the MODULATIO wordmark + tagline + "powered by
Feng-Tui", re-tints on F2, dismisses on a key, and — critically — is opt-in so
the default (test) construction reaches the tab surface without a blocking
splash on top.
"""
from textual.widgets import Static, TabbedContent

from modulatio.tui.app import ModulatioApp
from modulatio.tui.screens.splash import SplashScreen, _TAGLINE

PROJECT_CODE = "FENG"


async def test_splash_default_off_tabs_reachable_immediately():
    # The stub harness constructs the app without splash=True, so no boot frame
    # blocks the tab surface — the whole existing TUI suite depends on this.
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert not isinstance(app.screen, SplashScreen)
        # the tab surface is right there
        app.query_one(TabbedContent)


async def test_splash_loads_wordmark_and_tagline():
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True, splash=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert isinstance(app.screen, SplashScreen)
        art = app.screen.query_one("#splash-art", Static).render()
        text = art.plain if hasattr(art, "plain") else str(art)
        # the block wordmark renders with the heavy block glyph…
        assert "█" in text
        # …and the tagline + Feng-Tui credit load
        assert _TAGLINE in text
        assert "Feng-Tui" in text


async def test_splash_dismisses_on_key():
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True, splash=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert isinstance(app.screen, SplashScreen)
        # Past the opening dwell, any key begins.
        app.screen._enable_dismiss()
        await pilot.press("enter")
        await pilot.pause()
        # boot frame gone — the TUI is revealed underneath
        assert not isinstance(app.screen, SplashScreen)
        app.query_one(TabbedContent)


async def test_splash_swallows_early_keystroke(monkeypatch):
    # The bug: the Enter that launched `modulatio` can leak into the alt-screen
    # and skip the boot frame at t≈0, so the tagline never registers. A key
    # within the opening dwell must be swallowed, leaving the frame on screen.
    #
    # Determinism under load: pin the opening dwell effectively unbounded so the
    # swallow window covers the whole test regardless of how long mount→press
    # takes on a busy box. With the real 0.75s window, a loaded full-suite run
    # can exceed it before the press lands — the dwell timer would then fire and
    # the key would dismiss, flaking the test (the production guard is unaffected).
    from modulatio.tui.screens import splash as _splash
    monkeypatch.setattr(_splash, "_MIN_DWELL_SECONDS", 10_000.0)
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True, splash=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert isinstance(app.screen, SplashScreen)
        assert app.screen._dismissable is False  # gate starts closed
        await pilot.press("enter")               # a leaked launch keystroke
        await pilot.pause()
        # still on the boot frame — the early key did NOT skip it
        assert isinstance(app.screen, SplashScreen)


async def test_splash_retints_on_f2():
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True, splash=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.theme == "feng-amber"
        # F2 on the splash cycles the variant (and re-renders the art)
        await pilot.press("f2")
        await pilot.pause()
        assert app.theme == "feng-green"
        art = app.screen.query_one("#splash-art", Static).render()
        text = art.plain if hasattr(art, "plain") else str(art)
        assert "green" in text  # the variant name credit updated
