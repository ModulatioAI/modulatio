"""Re-sweep (0.9.0 pre-ship) regression for ``modulatio.tui.app``.

Finding 1 (MEDIUM/race) — ``_on_kickoff_done`` settled the shared TEAM spinner
unconditionally from the direct kickoff's OWN result. The engine explicitly
supports a converse-driven ``run_job`` AND a direct kickoff being live at the
same time (each on its own worker — see ``_active_job_orchestrators``). When a
fast kickoff finished while a converse ``run_job`` was still in flight, the
old code would ``set_done()`` / ``set_error()`` the TEAM spinner, lying about
the still-running converse job. The fix mirrors the symmetric guard already in
``_on_converse_done``: settle the spinner only when ``not _any_job_in_flight()``.
"""

from __future__ import annotations

import pytest

from modulatio import config, setup_state, vault
from modulatio.tui.app import ModulatioApp


PROJECT_CODE = "RESWEEP"


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


class _FakeOrch:
    """Stand-in for an Orchestrator running a converse-driven ``run_job``.

    ``_kickoff_active`` is the engine's "this orch has a job in flight" signal
    that ``_active_job_orchestrators`` reads."""

    def __init__(self, active: bool) -> None:
        self._kickoff_active = active


_GOOD_RESULT = {
    "mode": "stub",
    "goals": 1,
    "tasks": 1,
    "drafts": 1,
    "blocked_tasks": 0,
    "incomplete_goals": 0,
    "errors": 0,
}


@pytest.mark.asyncio
async def test_kickoff_done_does_not_settle_team_spinner_while_converse_job_in_flight():
    """A direct kickoff finishing must NOT settle the shared TEAM spinner when a
    separate converse-driven ``run_job`` is still live on its own worker — doing
    so would falsely read the running job as done."""
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()

        team = app._lane_status("stream-team-status")
        assert team is not None

        # Put the TEAM spinner into a clearly-running state.
        team.set_activity("decompose_started", "alice")
        assert team._verb is not None and not team._done

        # A converse-driven run_job is still in flight on its own worker.
        app._conv_orch = _FakeOrch(active=True)
        # This kickoff's own orch is already inactive — the engine clears
        # ``_kickoff_active`` in its finally before the worker delivers the
        # result, and ``_kickoff_pending`` is cleared inside _on_kickoff_done.
        app._kickoff_orch = _FakeOrch(active=False)
        app._kickoff_pending = True  # gets cleared by _on_kickoff_done

        app._on_kickoff_done(dict(_GOOD_RESULT), None)
        await pilot.pause()

        # The still-running converse job must keep the spinner running — NOT
        # forced to done/error by the unrelated kickoff's result.
        assert not team._done, (
            "kickoff settled the TEAM spinner to done while a converse run_job "
            "was still in flight"
        )
        assert team._error is None
        assert team._verb is not None


@pytest.mark.asyncio
async def test_kickoff_done_settles_team_spinner_when_nothing_else_running():
    """Sanity: with no other job in flight, a finished kickoff DOES settle the
    TEAM spinner from its own result (the guard only blocks the cross-job lie)."""
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()

        team = app._lane_status("stream-team-status")
        assert team is not None
        team.set_activity("decompose_started", "alice")

        # Nothing else live: no converse orch, no kickoff in startup window.
        app._conv_orch = None
        app._kickoff_orch = _FakeOrch(active=False)
        app._kickoff_pending = True  # cleared by _on_kickoff_done

        app._on_kickoff_done(dict(_GOOD_RESULT), None)
        await pilot.pause()

        assert team._done, "clean kickoff failed to settle the idle TEAM spinner"


@pytest.mark.asyncio
async def test_kickoff_error_does_not_settle_team_spinner_while_other_job_in_flight():
    """The guard covers the error path too: a kickoff erroring out must not flip
    the shared spinner to ✗ while a separate job is still running."""
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()

        team = app._lane_status("stream-team-status")
        assert team is not None
        team.set_activity("decompose_started", "alice")

        app._conv_orch = _FakeOrch(active=True)
        app._kickoff_orch = _FakeOrch(active=False)
        app._kickoff_pending = True

        app._on_kickoff_done(None, RuntimeError("kickoff blew up"))
        await pilot.pause()

        assert team._error is None, (
            "kickoff error flipped the TEAM spinner to ✗ while another job ran"
        )
        assert not team._done
        assert team._verb is not None
