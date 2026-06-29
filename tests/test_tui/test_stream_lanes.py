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
    """Nemo B1 #4: a producer SKILL role that merely starts with 'leader' (no
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
