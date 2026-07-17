# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""The stream lanes are AGENT-AGNOSTIC (parallel-execution Phase 1).

A producer is a model endpoint running ANY composable skill — there are no fixed
roles — so the TEAM lane must be the COMPLEMENT of the Leader + run-level roles,
never a hardcoded allow-list. The old ``TEAM_ROLES = {drafter, qc, researcher}``
allow-list silently dropped the actually-emitted ``research`` role, any custom
``default_producer_role``, and ``comptroller``. These tests pin the agnostic rule.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from modulatio import config, setup_state, vault
from modulatio.tui.app import ModulatioApp
from modulatio.tui.widgets.stream_status import StreamStatus
from modulatio.tui.widgets.stream_view import (
    StreamView,
    is_leader_role,
    is_team_role,
)
from modulatio.types import ActivityEvent


def _event(agent_id: str, task_id: str, phase: str) -> ActivityEvent:
    return ActivityEvent(
        agent_id=agent_id,
        role="drafter",
        phase=phase,
        task_id=task_id,
        timestamp=datetime.now(timezone.utc),
    )


@pytest.fixture
def _isolate(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "config"
    monkeypatch.setattr(config, "CONFIG_DIR", cfg_dir)
    monkeypatch.setattr(config, "DEFAULTS_FILE", cfg_dir / "defaults.json")
    monkeypatch.setattr(setup_state, "SETUP_STATE_FILE", cfg_dir / "setup-state.json")
    config.save_defaults({"vault_root": str(tmp_path / "vault")})
    config.reload()
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path / "vault")
    yield


@pytest.mark.asyncio
async def test_concurrent_settled_lines_keep_own_agent_task_pairing(_isolate):
    """Two producers running in parallel must never cross agent↔task in the
    rendered feed: each ``wrapped up a task`` line carries its OWN event's
    agent and task id, not another in-flight task's."""
    app = ModulatioApp(project_code="STRMX", stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()
        tv = app.query_one("#stream-team", StreamView)
        # Two producers dispatched on two different tasks, then settle in the
        # opposite order — the concurrent-cross scenario.
        tv.add_event(_event("nemotron", "modulatio1-T-002", "task_dispatched"))
        tv.add_event(_event("jimmy", "modulatio1-T-001", "task_dispatched"))
        tv.add_event(_event("nemotron", "modulatio1-T-002", "task_settled"))
        tv.add_event(_event("jimmy", "modulatio1-T-001", "task_settled"))
        await pilot.pause()

        settled = [m for m in tv.messages if "wrapped up a task" in m]
        assert len(settled) == 2, settled
        nemo_line = next(m for m in settled if "Nemotron" in m)
        jimmy_line = next(m for m in settled if "Jimmy" in m)
        # nemotron was on T-002, jimmy on T-001 — no cross.
        assert "modulatio1-T-002" in nemo_line, nemo_line
        assert "modulatio1-T-001" not in nemo_line, nemo_line
        assert "modulatio1-T-001" in jimmy_line, jimmy_line
        assert "modulatio1-T-002" not in jimmy_line, jimmy_line


def test_leader_role_covers_leader_star_and_planner():
    for r in ("leader", "leader-decompose", "leader-reflect", "leader-iterate",
              "leader-chat", "planner"):
        assert is_leader_role(r), r
    for r in ("drafter", "qc", "research", "orchestrator"):
        assert not is_leader_role(r), r


def test_team_role_is_the_complement_any_producer_shows():
    # the actually-emitted producer/QC roles — including ``research`` (which the
    # old allow-list spelled ``researcher`` and therefore DROPPED) …
    for r in ("drafter", "qc", "research"):
        assert is_team_role(r), r
    # … and ANY skill role / custom default_producer_role — agent-agnostic.
    for r in ("long-form", "consolidation", "media-assembly", "data-assembly",
              "my-custom-producer", "writer-3000"):
        assert is_team_role(r), r


def test_team_role_excludes_leader_and_run_level():
    for r in ("leader", "leader-reflect", "planner", "orchestrator", "comptroller", ""):
        assert not is_team_role(r), r


def test_lanes_partition_emitted_roles():
    """Every role the orchestrator emits lands in exactly one place: leader lane,
    team lane, or run-level (neither) — none silently dropped from both."""
    emitted = [
        "leader", "leader-chat", "leader-decompose", "leader-iterate",
        "leader-reflect", "planner",            # → leader
        "orchestrator", "comptroller",          # → run-level (neither)
        "qc", "research", "drafter",            # → team
    ]
    for r in emitted:
        in_leader = is_leader_role(r)
        in_team = is_team_role(r)
        assert not (in_leader and in_team), f"{r} in BOTH lanes"
        if r in ("orchestrator", "comptroller"):
            assert not in_leader and not in_team  # run-level, neither transcript lane
        else:
            assert in_leader ^ in_team, f"{r} in NEITHER lane (silently dropped)"


def test_leader_role_does_not_overcatch_producer_skills():
    """A producer SKILL role that merely starts with 'leader' (no
    hyphen) must NOT be mis-routed to the Leader lane — the hyphen is required."""
    for r in ("leaderboard-generator", "leadership-coach", "leaderly-writer"):
        assert not is_leader_role(r), r
        assert is_team_role(r), r  # it's a producer → team lane
    # the real leader-* surfaces still route to the Leader lane
    for r in ("leader", "leader-chat", "leader-decompose"):
        assert is_leader_role(r) and not is_team_role(r), r


@pytest.mark.asyncio
async def test_leader_call_failed_drives_status_into_honest_error(_isolate):
    """Op C: a wedged/timed-out leader call emits ``<role>_call_failed`` — the
    LEADER lane must show an HONEST error (✗ in the status, ⚠ in the feed), NOT
    stay stuck on its last working spinner as if the call were still in flight."""
    app = ModulatioApp(project_code="STRMX", stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()
        ev = ActivityEvent(
            agent_id="leader", role="leader", phase="leader_call_failed",
            task_id=None, timestamp=datetime.now(timezone.utc),
        )
        app._record_activity_impl(ev)
        await pilot.pause()
        # Status line: honest error, not a perpetual spinner.
        status = app.query_one("#stream-leader-status", StreamStatus)
        assert status._error is not None, "leader status stuck working, not errored"
        assert "timed out" in status._error, status._error
        # Feed: the LEADER lane renders the failure plainly, not a raw phase string.
        lv = app.query_one("#stream-leader", StreamView)
        assert any("timed out" in m for m in lv.messages), lv.messages


# ── per-tool activity icons + verbs (Feng-Tui refinement arc, W3) ────────────
# A tool_call_ended event carries detail={"tool": name}; the feed renders a
# themed glyph + verb per tool ("⌕ searching the web"), never the raw phase.


def _tool_event(tool: str | None):
    return ActivityEvent(
        agent_id="prod-1", role="drafter", phase="tool_call_ended",
        task_id="T-1", timestamp=datetime.now(timezone.utc),
        detail={"tool": tool} if tool else None,
    )


def test_tool_glyph_and_verb_resolve_from_detail():
    from modulatio.tui.widgets.stream_view import _tool_glyph_verb
    # W3b: doubled heavy phosphor marks — a two-cell icon, not a doot.
    assert _tool_glyph_verb(_tool_event("web_search")) == ("◉◉", "is searching the web")
    assert _tool_glyph_verb(_tool_event("write_artifact")) == ("✎✎", "is writing")
    assert _tool_glyph_verb(_tool_event("run_shell")) == ("▲▲", "is building")
    g, v = _tool_glyph_verb(_tool_event("mystery_tool"))
    assert "mystery tool" in v  # unknown tool still reads honestly


def test_tool_event_without_detail_still_reads_honestly():
    from modulatio.tui.widgets.stream_view import _tool_glyph_verb
    g, v = _tool_glyph_verb(_tool_event(None))
    assert v == "ran a tool"


def test_richer_phases_have_glyph_rows():
    from modulatio.tui.widgets.stream_view import _PHASE
    for phase in ("leader_thinking", "task_planning_started",
                  "qc_authored_fix", "skill_codified", "model_fallback",
                  "task_decomposed"):
        assert phase in _PHASE, phase


@pytest.mark.asyncio
async def test_repeat_events_coalesce_into_a_counter(_isolate):
    """W3b: the same agent doing the same thing again updates one line's (×N)
    counter instead of stacking a wall; a different verb starts a fresh line."""
    from modulatio.tui.app import ModulatioApp
    from modulatio.tui.widgets.stream_view import StreamView

    app = ModulatioApp(project_code="LANES", stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()
        view = app.query_one("#stream-team", StreamView)
        for _ in range(3):
            view.add_event(_tool_event("http_get"))
        await pilot.pause()
        lines = [str(w.render()) for w in view.query(".stream-line")]
        page_lines = [ln for ln in lines if "is reading a page" in ln]
        assert len(page_lines) == 1
        assert "(×3)" in page_lines[0]

        view.add_event(_tool_event("web_search"))  # different verb → new line
        view.add_event(_tool_event("http_get"))    # chain broken → fresh line
        await pilot.pause()
        lines = [str(w.render()) for w in view.query(".stream-line")]
        assert sum("is reading a page" in ln for ln in lines) == 2
        assert sum("is searching the web" in ln for ln in lines) == 1


@pytest.mark.asyncio
async def test_leader_reply_carries_highlight_operator_does_not(_isolate):
    """Clif live-test (2026-07-06): the Leader's replies sit on a dark
    highlight block so they read apart from the operator's lines — strong
    contrast with the near-white letters, theme-agnostic (neutral, not an
    accent fill). The operator's own lines stay on the bare phosphor black."""
    from modulatio.tui.feng_theme import LEADER_HIGHLIGHT_BG

    app = ModulatioApp(project_code="STRMH", stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()
        tv = app.query_one("#stream-leader", StreamView)
        tv.add_operator_message("me talking")
        tv.add_leader_message("the Leader talking")
        await pilot.pause()
        lines = list(tv.query(".stream-line"))
        op_styles = " ".join(str(s.style) for s in lines[-2].render().spans)
        ld_styles = " ".join(str(s.style) for s in lines[-1].render().spans)
        # styles normalize hex → rgb(...) in the rendered spans
        from rich.color import Color
        r, g, b = Color.parse(LEADER_HIGHLIGHT_BG).get_truecolor()
        mark = f"on rgb({r},{g},{b})"
        assert mark in ld_styles      # the Leader's block is highlighted
        assert mark not in op_styles  # the operator stays on bare black
