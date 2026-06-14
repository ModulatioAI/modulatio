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
        app._any_job_in_flight = lambda: True  # type: ignore[method-assign]

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
        app._any_job_in_flight = lambda: False  # type: ignore[method-assign]

        app._on_converse_done("hi there")
        await pilot.pause()

        assert team._done is True


# ─── pre-ship #F8a: STOP must signal EVERY active orchestrator ───────────────


class _FakeOrch:
    """A stand-in orchestrator: ``_kickoff_active`` says it's running, and an
    ``abort_event`` whose ``.set()`` we can observe."""

    def __init__(self, active: bool = True):
        self._kickoff_active = active

        class _Ev:
            def __init__(self):
                self.was_set = False

            def set(self):
                self.was_set = True

        self.abort_event = _Ev()


@pytest.mark.asyncio
async def test_stop_signals_both_concurrent_orchestrators():
    """F8 with BOTH a converse-driven run_job and a direct kickoff in flight
    must signal ``abort_event`` on BOTH — not just the first one found."""
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()

        conv = _FakeOrch(active=True)
        kick = _FakeOrch(active=True)
        app._conv_orch = conv  # type: ignore[attr-defined]
        app._kickoff_orch = kick  # type: ignore[attr-defined]

        app.action_stop_job()

        assert conv.abort_event.was_set is True
        assert kick.abort_event.was_set is True


@pytest.mark.asyncio
async def test_stop_skips_inactive_orchestrator():
    """An orchestrator that exists but is NOT running (``_kickoff_active`` is
    False) must not be signaled; only the live one is."""
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()

        idle = _FakeOrch(active=False)
        live = _FakeOrch(active=True)
        app._conv_orch = idle  # type: ignore[attr-defined]
        app._kickoff_orch = live  # type: ignore[attr-defined]

        app.action_stop_job()

        assert idle.abort_event.was_set is False
        assert live.abort_event.was_set is True


@pytest.mark.asyncio
async def test_stop_noop_when_nothing_running():
    """F8 with no live orchestrators must be a clean no-op (never raises)."""
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()
        # No _conv_orch / _kickoff_orch attributes at all.
        app.action_stop_job()  # must not raise


# ─── pre-ship #F8b: kickoff startup window must hold the TEAM spinner ─────────


@pytest.mark.asyncio
async def test_kickoff_pending_keeps_converse_done_from_settling():
    """During a kickoff's startup window the worker is scheduled but
    ``_kickoff_orch`` hasn't come up yet, so ``_active_job_orchestrators`` is
    empty. The ``_kickoff_pending`` flag must still make ``_any_job_in_flight``
    True so a concurrent converse-done does NOT falsely settle the spinner."""
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()

        app._set_lane_status("stream-team-status", "modulating")
        await pilot.pause()
        team = app._lane_status("stream-team-status")
        assert team is not None and team._done is False

        # Startup window: pending set, but no orchestrator exposed yet.
        app._kickoff_pending = True  # type: ignore[attr-defined]
        assert getattr(app, "_kickoff_orch", None) is None
        assert app._active_job_orchestrators() == []
        assert app._any_job_in_flight() is True

        app._on_converse_done("hi there")
        await pilot.pause()

        # Must NOT have force-settled — the kickoff is still spinning up.
        assert team._done is False
