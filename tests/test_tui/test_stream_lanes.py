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

from modulatio.tui.widgets.stream_view import is_leader_role, is_team_role


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
