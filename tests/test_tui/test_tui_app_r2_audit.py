"""Round-2 audit regressions for ``modulatio.tui.app`` (3 findings).

Finding 1 (MEDIUM/resource-leak) — a double KICK OFF (two fast F5 presses, or
F5 while the button-disable guard is bypassed) re-entered ``_run_kickoff`` and
overwrote ``self._kickoff_tick`` with a fresh ``set_interval`` handle WITHOUT
stopping the prior one, leaking a timer that ticks forever (its handle is gone,
so ``_on_kickoff_done`` can never stop it). The fix guards ``_run_kickoff``:
if a kickoff is already in flight (``_kickoff_tick`` live), the second launch is
a no-op with a "job already running" status.

Finding 2 (MEDIUM/correctness) — ``/restart`` calls ``app.exit(return_code=42)``
but the entry point ignored the code, so the TUI just quit with a misleading
"Restarting..." message and never came back. The fix adds ``_relaunch_if_restart``
which re-execs the interpreter on code 42 (and is a no-op otherwise).

Finding 3 (LOW/correctness) — ``_agent_name``'s cache is documented "Cached per
run" but was built once for the app's whole lifetime and never invalidated, so a
roster change between runs (new agent / rename) resolved to a stale/empty name.
The fix drops the cache on each ``kickoff_started`` activity event.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from modulatio import config, setup_state, vault
from modulatio.tui.app import ModulatioApp, _relaunch_if_restart


def _kickoff_started_event():
    """A minimal, fully-populated ``kickoff_started`` activity event."""
    from modulatio.types import ActivityEvent

    return ActivityEvent(
        agent_id="orchestrator",
        role="orchestrator",
        phase="kickoff_started",
        task_id=None,
        timestamp=datetime.now(timezone.utc),
    )


PROJECT_CODE = "R2AUD"


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


# ─── Finding 1: double KICK OFF must not leak a set_interval timer ───────────


@pytest.mark.asyncio
async def test_double_kickoff_does_not_overwrite_running_tick():
    """A second ``_run_kickoff`` while a kickoff is already in flight must NOT
    replace ``_kickoff_tick`` (which would leak the prior, now-unstoppable
    interval). It short-circuits with a 'job already running' status instead."""
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()

        # Simulate a run already in flight: a live tick handle is the engine's
        # "kickoff is running" signal (set in _run_kickoff, torn down in
        # _on_kickoff_done). Use a sentinel so we can detect any overwrite.
        sentinel = object()
        app._kickoff_tick = sentinel

        # A second launch with a real objective must be refused, not start a
        # new interval that clobbers the handle.
        app._run_kickoff("write a second thing")
        await pilot.pause()

        assert app._kickoff_tick is sentinel, (
            "double kickoff overwrote the live tick handle — the prior "
            "set_interval timer is now leaked (unstoppable)"
        )


@pytest.mark.asyncio
async def test_kickoff_starts_tick_when_none_running():
    """Sanity: with no run in flight (no ``_kickoff_tick``), a stub kickoff
    DOES install a tick handle — the guard only blocks a *second* launch."""
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()
        assert getattr(app, "_kickoff_tick", None) is None

        # ``set_interval`` runs synchronously inside ``_run_kickoff`` (before the
        # background worker is scheduled), so the handle is present the instant
        # the call returns — assert before pumping the loop to avoid racing the
        # fast stub worker's ``_on_kickoff_done`` teardown.
        app._run_kickoff("write a thing")
        assert getattr(app, "_kickoff_tick", None) is not None

        # Let the worker drain so the run tears down cleanly.
        for _ in range(8):
            await pilot.pause()


# ─── Finding 2: /restart must actually relaunch on return_code 42 ────────────


class _FakeApp:
    def __init__(self, return_code):
        self.return_code = return_code


def test_relaunch_if_restart_reexecs_on_code_42(monkeypatch):
    """``_relaunch_if_restart`` must re-exec the interpreter when the app exited
    with the restart sentinel (42). Without this, ``/restart`` just quit."""
    calls = []

    def _fake_execv(path, argv):
        calls.append((path, list(argv)))

    monkeypatch.setattr("os.execv", _fake_execv)

    _relaunch_if_restart(_FakeApp(42))

    assert len(calls) == 1, "code 42 must trigger exactly one os.execv re-exec"
    path, argv = calls[0]
    import sys

    assert path == sys.executable
    assert argv[0] == sys.executable


def test_relaunch_if_restart_noop_on_normal_exit(monkeypatch):
    """A plain quit (code 0 / non-42) must NOT re-exec."""
    calls = []
    monkeypatch.setattr("os.execv", lambda *a: calls.append(a))

    _relaunch_if_restart(_FakeApp(0))
    _relaunch_if_restart(_FakeApp(1))
    _relaunch_if_restart(_FakeApp(None))

    assert calls == [], "only the restart sentinel (42) may re-exec"


@pytest.mark.asyncio
async def test_restart_side_effect_exits_with_code_42():
    """The ``/restart`` command's side-effect must call ``exit(return_code=42)``
    — the sentinel that ``_relaunch_if_restart`` keys on. Spy on ``exit`` so we
    observe the code without actually tearing down the test app."""
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()
        captured = {}

        def _spy_exit(*args, return_code=None, **kwargs):
            captured["return_code"] = return_code

        app.exit = _spy_exit  # type: ignore[assignment]
        app._apply_side_effect("restart_tui")
        await pilot.pause()
        assert captured.get("return_code") == 42


# ─── Finding 3: _agent_name cache must be per-run, not per-app-lifetime ──────


@pytest.mark.asyncio
async def test_agent_name_cache_invalidated_on_new_run():
    """``_agent_name`` is documented 'Cached per run'. A ``kickoff_started``
    event must drop the cache so a roster change between runs is reflected."""
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()

        # Prime the cache directly (simulating a first run that resolved names).
        app._agent_name_cache = {"ag-1": "Cowboy"}
        assert app._agent_name("ag-1") == "Cowboy"

        # A new run begins — the per-run cache must be invalidated.
        app._record_activity_impl(_kickoff_started_event())
        await pilot.pause()

        assert getattr(app, "_agent_name_cache", None) is None, (
            "_agent_name cache must be dropped at kickoff_started so a roster "
            "change between runs is not masked by stale entries"
        )


@pytest.mark.asyncio
async def test_agent_name_rebuilds_after_invalidation():
    """After invalidation, the next ``_agent_name`` lookup rebuilds from the
    current roster rather than serving stale state."""
    from modulatio import roster

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()

        # Stale cache from a prior run.
        app._agent_name_cache = {"ghost": "Stale"}

        # New run starts -> cache cleared.
        app._record_activity_impl(_kickoff_started_event())
        await pilot.pause()

        # The next lookup rebuilds from the (empty) live roster -> the stale
        # entry is gone.
        assert app._agent_name("ghost") == ""
        assert isinstance(roster.list_agents(PROJECT_CODE), list)
