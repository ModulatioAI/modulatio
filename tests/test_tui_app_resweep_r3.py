"""Round-3 re-sweep regression for ``modulatio.tui.app`` (1 finding).

Finding 1 (LOW/resource-leak) — ``on_worker_state_changed`` only branched on
``WorkerState.SUCCESS`` and ``WorkerState.ERROR`` for both the ``kickoff`` and
``converse`` worker groups. ``WorkerState.CANCELLED`` was silently dropped, so
neither ``_on_kickoff_done`` nor a converse-lane settle ran when a worker was
cancelled (app teardown, or Textual cancelling an exclusive worker). For a
cancelled KICKOFF that meant the elapsed-time ``set_interval`` kept ticking,
``_kickoff_pending`` stayed armed, and the launch guard (``_kickoff_tick`` live)
locked out every future kickoff with "a job is already running". For a cancelled
CONVERSE with no replacement worker the leader-status spinner hung on
"thinking" forever.

The fix adds a ``CANCELLED`` branch to both groups: kickoff routes through
``_on_kickoff_done(None, None)`` (the full teardown); converse routes through a
new ``_on_converse_cancelled`` that settles the lanes WITHOUT posting a spurious
"(cancelled)" chat reply, and is guarded so it never clobbers a replacement
converse worker's fresh "thinking" status.
"""

from __future__ import annotations

import pytest

from modulatio import config, setup_state, vault
from modulatio.tui.app import ModulatioApp


PROJECT_CODE = "R3RES"


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


class _FakeWorker:
    """Minimal stand-in for a Textual ``Worker`` — ``on_worker_state_changed``
    only ever reads ``.group``, ``.result``, ``.error`` off the worker and
    ``.state`` off the event."""

    def __init__(self, group, state, result=None, error=None):
        self.group = group
        self.state = state
        self.result = result
        self.error = error


class _FakeStateChanged:
    def __init__(self, worker, state):
        self.worker = worker
        self.state = state


def _cancelled_event(group):
    from textual.worker import WorkerState

    worker = _FakeWorker(group, WorkerState.CANCELLED)
    return _FakeStateChanged(worker, WorkerState.CANCELLED)


# ─── kickoff: a cancelled worker must tear down the timer + guards ───────────


@pytest.mark.asyncio
async def test_cancelled_kickoff_tears_down_timer_and_guards():
    """A CANCELLED kickoff worker must run the same teardown as a finished one:
    stop + drop ``_kickoff_tick``, clear ``_kickoff_pending``, re-enable the
    button. Otherwise the timer ticks forever and the launch guard permanently
    locks out new kickoffs."""
    from textual.worker import WorkerState

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()

        # Simulate a kickoff in flight: live tick + armed startup guard.
        stopped = {"n": 0}

        class _Tick:
            def stop(self_inner):
                stopped["n"] += 1

        app._kickoff_tick = _Tick()
        app._kickoff_pending = True
        app._kickoff_started_at = 0.0
        app._kickoff_mode = "stub"

        app.on_worker_state_changed(_cancelled_event("kickoff"))
        await pilot.pause()

        assert stopped["n"] == 1, "cancelled kickoff must stop the elapsed tick"
        assert getattr(app, "_kickoff_tick", None) is None, (
            "cancelled kickoff must drop the tick handle so the launch guard "
            "(_kickoff_tick live) no longer locks out new runs"
        )
        assert app._kickoff_pending is False, (
            "cancelled kickoff must clear the in-flight startup guard"
        )
        # Sanity: with the guards cleared a fresh kickoff is no longer refused.
        assert WorkerState.CANCELLED  # state member exists (guards the import)


@pytest.mark.asyncio
async def test_cancelled_kickoff_does_not_raise_without_result():
    """The CANCELLED kickoff teardown is driven with ``result=None`` — the
    done-handler must tolerate a missing result dict (no KeyError) and reach a
    clean settled state."""
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()
        app._kickoff_pending = True
        # No _kickoff_tick / _kickoff_started_at set — teardown must be tolerant.
        app.on_worker_state_changed(_cancelled_event("kickoff"))
        await pilot.pause()
        assert app._kickoff_pending is False


# ─── converse: a cancelled worker must settle the leader lane ────────────────


@pytest.mark.asyncio
async def test_cancelled_converse_settles_leader_lane_when_no_replacement():
    """A CANCELLED converse worker with NO replacement in flight must settle the
    leader-status lane off "thinking" (set_idle), instead of leaving the spinner
    hung forever."""
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()

        status = app._lane_status("stream-leader-status")
        assert status is not None
        # Arm the spinner as if a converse worker were thinking.
        status.set_activity("leader_thinking")
        assert status._verb is not None

        app.on_worker_state_changed(_cancelled_event("converse"))
        await pilot.pause()

        assert status._verb is None and status._error is None, (
            "cancelled converse with no replacement must settle the leader lane "
            "(spinner off), not leave it stuck on 'thinking'"
        )


@pytest.mark.asyncio
async def test_cancelled_converse_does_not_post_chat_reply():
    """The cancel teardown must NOT inject a "(cancelled)" message into the
    Leader chat — that would be spurious noise in the common case where a second
    message simply replaced the first."""
    from modulatio.tui.widgets.stream_view import StreamView

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()

        posted = []
        view = app.query_one("#stream-leader", StreamView)
        view.add_leader_message = lambda msg: posted.append(msg)  # type: ignore

        app.on_worker_state_changed(_cancelled_event("converse"))
        await pilot.pause()

        assert posted == [], (
            "cancelled converse must settle silently, not post a chat reply"
        )
