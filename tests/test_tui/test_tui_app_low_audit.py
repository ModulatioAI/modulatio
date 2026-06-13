"""LOW-severity audit regressions for ``modulatio.tui.app`` (findings #85, #86).

#85 — ``switch_tab:cron:<code>`` silently dropped the project filter: the
``_apply_side_effect`` third-part handler only honored ``memory``; the cron
filter was parsed off the side-effect string and then ignored, so ``/cron STA``
landed on an unfiltered tab.

#86 — ``_on_converse_done`` force-settled the TEAM spinner to ``set_done`` even
when a *separate* kickoff job (button/F5) was still in flight on its own worker,
lying about the live floor. It must only force-settle when nothing is running.
"""

from __future__ import annotations

import pytest

from modulatio import config, cron, setup_state, vault
from modulatio.tui.app import ModulatioApp


PROJECT_CODE = "LOWAUD"


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "config"
    monkeypatch.setattr(config, "CONFIG_DIR", cfg_dir)
    monkeypatch.setattr(config, "DEFAULTS_FILE", cfg_dir / "defaults.json")
    monkeypatch.setattr(setup_state, "SETUP_STATE_FILE", cfg_dir / "setup-state.json")
    config.save_defaults({"vault_root": str(tmp_path / "vault")})
    config.reload()
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path / "vault")
    yield


# ─── #85: cron project filter must actually apply ───────────────────────────


@pytest.mark.asyncio
async def test_switch_tab_cron_routes_code_to_focus_project():
    """``/cron STA`` → side-effect ``switch_tab:cron:STA`` must forward the
    project code to the cron screen instead of dropping it. app.py routes the
    code to ``CronScreen.focus_project`` (sibling of ``MemoryScreen.focus_agent``).
    Spy on the REAL method (save/restore — never ``del`` it, that would strip the
    method off the class for the rest of the session)."""
    cron.add(name="a", schedule="weekly mon 09:00", project_code="STA", objective="x")

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        from textual.widgets import TabbedContent

        from modulatio.tui.screens.cron import CronScreen

        captured: list[str] = []
        original = CronScreen.focus_project

        def _spy(self, code):
            captured.append(code)
            return original(self, code)

        CronScreen.focus_project = _spy  # type: ignore[assignment]
        try:
            app._apply_side_effect("switch_tab:cron:STA")
            # focus_project is invoked via call_after_refresh — pump the loop.
            for _ in range(6):
                await pilot.pause()
        finally:
            CronScreen.focus_project = original  # type: ignore[assignment]

        assert app.query_one("#app-tabs", TabbedContent).active == "tab-cron"
        assert captured == ["STA"], "the project code must reach the cron screen"


@pytest.mark.asyncio
async def test_cron_focus_project_unknown_code_falls_back_to_all():
    """#85: ``focus_project`` with a blank or unknown code must fall back to All
    projects (``_project_filter is None``) so the Select stays on a valid option."""
    cron.add(name="a", schedule="weekly mon 09:00", project_code="STA", objective="x")

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        from modulatio.tui.screens.cron import CronScreen

        screen = app.query_one(CronScreen)
        screen.focus_project("STA")
        assert screen._project_filter == "STA"
        screen.focus_project("NOPE")  # unknown code
        assert screen._project_filter is None
        screen.focus_project("")  # blank
        assert screen._project_filter is None
        await pilot.pause()


@pytest.mark.asyncio
async def test_switch_tab_cron_without_focus_method_does_not_crash():
    """When the cron screen has no ``focus_project`` yet, ``/cron STA`` must
    still switch tabs cleanly (no crash, code routing is a best-effort no-op)."""
    cron.add(name="a", schedule="weekly mon 09:00", project_code="STA", objective="x")

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        from textual.widgets import TabbedContent

        app._apply_side_effect("switch_tab:cron:STA")
        await pilot.pause()
        await pilot.pause()

        assert app.query_one("#app-tabs", TabbedContent).active == "tab-cron"


# ─── #86: converse-done must not clobber a running kickoff's spinner ─────────


@pytest.mark.asyncio
async def test_converse_done_does_not_settle_running_kickoff_spinner():
    """A kickoff is still in flight (``_running_job_orchestrator`` non-None);
    ``_on_converse_done`` must NOT force the TEAM spinner to done."""
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()

        # TEAM lane shows a running job.
        app._set_lane_status("stream-team-status", "modulating")
        await pilot.pause()
        team = app._lane_status("stream-team-status")
        assert team is not None and team._done is False

        # Pretend a kickoff worker is in flight.
        app._running_job_orchestrator = lambda: object()  # type: ignore[method-assign]

        app._on_converse_done("hi there")
        await pilot.pause()

        # Spinner must NOT have been force-settled — the kickoff is still live.
        assert team._done is False


@pytest.mark.asyncio
async def test_converse_done_settles_spinner_when_idle():
    """With nothing running, ``_on_converse_done`` still settles the TEAM
    spinner (the belt for the converse run_job error path is preserved)."""
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()

        app._set_lane_status("stream-team-status", "modulating")
        await pilot.pause()
        team = app._lane_status("stream-team-status")
        assert team is not None and team._done is False

        # No job in flight.
        app._running_job_orchestrator = lambda: None  # type: ignore[method-assign]

        app._on_converse_done("hi there")
        await pilot.pause()

        assert team._done is True
