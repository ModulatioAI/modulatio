# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Durable smoke for Brick 3 (the keystone) — offline, no network.

Proves capability-and-availability routing that never blocks:
  1. skills DON'T gate — a producer holding no matching skill is still picked,
  2. never-block — when no producer meets the capability floor, the best-
     available producer is picked with an advisory shortfall (→ PQR), not a gap,
  3. a referenced skill not in the library is advisory missing_skills, not a
     CRITICAL block,
  4. load-balance — a multi-task pass spreads across idle producers,
  5. ROSTER_GAP fires ONLY when there's no producer at all,
  6. the wizard builds a producer as a pure model endpoint (no skills).

Run: .venv/bin/python scripts/smoke/skill-library/smoke_keystone_brick3.py
Exit 0 = all checks pass.
"""

from __future__ import annotations

import sys
from uuid import uuid4


def main() -> int:
    from modulatio import dispatch
    from modulatio.roster import Agent
    from modulatio.types import Task

    failures: list[str] = []

    def check(label: str, cond: bool) -> None:
        print(f"  [{'ok ' if cond else 'FAIL'}] {label}")
        if not cond:
            failures.append(label)

    def producer(pid, tags=(), cost="paid-cloud", mtier="generalist"):
        return Agent(id=pid, name=pid, tier="producer", capability_tags=list(tags),
                     cost_class=cost, model_tier=mtier)

    def task(skills=("web-search",), caps=()):
        return Task(id="T", project_id=uuid4(), goal_id="G", description="d",
                    artifact_kind="research", required_skills=list(skills),
                    required_capabilities=list(caps))

    print("Brick 3 smoke — capability + availability routing, never-block")

    # 1. skills don't gate
    p = producer("p1")
    r = dispatch.plan_dispatch(task(["web-search"]), [p], ["web-search"])
    check("skill-less producer is picked (skills don't gate)",
          r.outcome is dispatch.DispatchOutcome.MATCHED and r.agent.id == "p1")

    # 2. never-block on capability floor
    weak = producer("weak", tags=["fast"], mtier="budget")
    strong = producer("strong", tags=["reasoning-heavy"], mtier="strategic")
    r = dispatch.plan_dispatch(task(caps=["vision"]), [weak, strong], ["web-search"])
    check("nobody meets floor → best-available (highest tier) + shortfall",
          r.outcome is dispatch.DispatchOutcome.MATCHED
          and r.agent.id == "strong" and r.capability_shortfall == ("vision",))

    # 3. unknown skill is advisory, not a block
    r = dispatch.plan_dispatch(task(["not-in-library"]), [p], ["web-search"])
    check("unknown skill → MATCHED + advisory missing_skills",
          r.outcome is dispatch.DispatchOutcome.MATCHED
          and "not-in-library" in r.missing_skills)

    # 4. load-balance: a 4-task pass spreads across 2 idle producers
    a, b = producer("aaa"), producer("bbb")
    load: dict[str, int] = {}
    picks = []
    for _ in range(4):
        res = dispatch.plan_dispatch(task(), [a, b], ["web-search"], load=load)
        picks.append(res.agent.id)
        load[res.agent.id] = load.get(res.agent.id, 0) + 1
    check("4 tasks spread evenly across 2 producers (2 each)",
          picks.count("aaa") == 2 and picks.count("bbb") == 2)

    # 5. ROSTER_GAP only when no producer exists
    leader = Agent(id="ldr", name="ldr", tier="leader")
    r = dispatch.plan_dispatch(task(), [leader], ["web-search"])
    check("no producer at all → ROSTER_GAP",
          r.outcome is dispatch.DispatchOutcome.ROSTER_GAP)

    # 6. wizard producer is a pure model endpoint (no skills)
    from modulatio import model_presets
    _orig = model_presets.get_preset
    model_presets.get_preset = lambda k: None  # force inference
    try:
        from modulatio.setup_wizard import agent_step, steps
        steps.confirm_yn = lambda *a, **k: True  # accept inferred caps
        prod = agent_step._build_producer("claude-opus-4-8", index=1)
    finally:
        model_presets.get_preset = _orig
    check("wizard producer has NO skills + caps from model",
          prod["skills"] == [] and prod["tier"] == "producer"
          and "vision" in prod["capability_tags"])

    print()
    if failures:
        print(f"SMOKE FAILED — {len(failures)} check(s): {failures}")
        return 1
    print("SMOKE PASSED — routing is capability+availability, never blocks.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
