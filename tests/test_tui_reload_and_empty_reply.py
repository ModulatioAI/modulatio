"""Two small TUI fixes (post-0.9.8.5 backlog quick wins).

1. Empty-reply guard — an empty/whitespace-only Leader converse turn (a model
   refusal, a turn that ends on tool calls with no final content, a Clay
   ``claude -p`` hiccup) used to render as a silent "◆ Leader" glyph with no
   text, reading as if the Leader ignored you. It must render a visible fallback.

2. Reload services — changing the Leader model in the agent picker rewrites the
   roster, but converse uses a CACHED orchestrator (``_conv_orch``), so the
   change didn't take until a full TUI restart. ``reload_services`` drops the
   cached orchestrator + refreshes the config cache so the next message/run
   rebuilds from disk, and refuses while the Leader or a job is busy.
"""
from __future__ import annotations

import pytest

from modulatio import config, setup_state, vault
from modulatio.tui.app import ModulatioApp

PROJECT_CODE = "RLDEMP"


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


@pytest.mark.asyncio
async def test_empty_converse_reply_renders_visible_fallback():
    from modulatio.tui.widgets.stream_view import StreamView

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()
        posted: list[str] = []
        view = app.query_one("#stream-leader", StreamView)
        view.add_leader_message = lambda msg: posted.append(msg)  # type: ignore

        app._on_converse_done("   ")  # whitespace-only = effectively empty
        await pilot.pause()

        assert len(posted) == 1 and posted[0].strip(), (
            "an empty/whitespace Leader reply must render a visible fallback, "
            "not a silent bubble"
        )


@pytest.mark.asyncio
async def test_nonempty_converse_reply_passes_through_unchanged():
    from modulatio.tui.widgets.stream_view import StreamView

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()
        posted: list[str] = []
        view = app.query_one("#stream-leader", StreamView)
        view.add_leader_message = lambda msg: posted.append(msg)  # type: ignore

        app._on_converse_done("Here's your answer.")
        await pilot.pause()

        assert posted == ["Here's your answer."]


@pytest.mark.asyncio
async def test_reload_services_clears_cached_orchestrator_when_idle():
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()
        app._conv_orch = object()  # a cached converse orchestrator
        ok, msg = app.reload_services()
        assert ok is True
        assert app._conv_orch is None, (
            "reload must invalidate the cached converse orchestrator so the next "
            "message rebuilds from current config/roster"
        )
        assert msg


@pytest.mark.asyncio
async def test_reload_services_refused_while_busy_leaves_orchestrator_intact():
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()
        sentinel = object()
        app._conv_orch = sentinel
        app._any_job_in_flight = lambda: True  # type: ignore — pretend a job is live
        ok, msg = app.reload_services()
        assert ok is False
        assert app._conv_orch is sentinel, (
            "a refused reload must NOT invalidate the live orchestrator mid-job"
        )
        assert msg


# ── /reload surface wiring (arc #3 close-out, 2026-07-02) ───────────────────
# ``reload_services`` existed with no user-facing surface — nothing outside
# tests called it. ``/reload`` in the prompt (F1: "Reload services") now
# routes to it via the side-effect dispatcher and toasts the outcome.


def test_reload_command_dispatches_reload_services_side_effect():
    from modulatio.tui import commands

    result = commands.dispatch("/reload")
    assert result.handled and result.ok
    assert result.side_effect == "reload_services"


@pytest.mark.asyncio
async def test_reload_side_effect_calls_reload_services_and_notifies():
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()
        calls: list[str] = []
        app.reload_services = lambda: (calls.append("hit") or (True, "reloaded"))  # type: ignore
        toasts: list[str] = []
        app.notify = lambda msg, **kw: toasts.append(str(msg))  # type: ignore

        app._apply_side_effect("reload_services")
        await pilot.pause()

        assert calls == ["hit"]
        assert toasts and "reloaded" in toasts[0]


# ── /cls + Ctrl+L: clear the active TV (Feng-Tui refinement arc, W4) ─────────


@pytest.mark.asyncio
async def test_clear_tv_clears_the_active_view_only():
    from modulatio.tui.widgets.stream_view import StreamView

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()
        leader = app.query_one("#stream-leader", StreamView)
        team = app.query_one("#stream-team", StreamView)
        leader.add_leader_message("a verdict rides here")
        team.add_operator_message("team chatter")
        await pilot.pause()
        assert leader.messages and team.messages

        # LEADER view is active by default → Ctrl+L clears the leader TV only
        app.action_clear_tv()
        await pilot.pause()
        assert not leader.messages
        assert team.messages  # untouched

        # flip to TEAM (F4) → clear hits the team TV
        app.action_flip_stream()
        await pilot.pause()
        app.action_clear_tv()
        await pilot.pause()
        assert not team.messages
