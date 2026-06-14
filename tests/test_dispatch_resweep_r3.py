"""Round-3 pre-ship regression for ``dispatch.schedule_wave`` deferral
diagnostics (0.9.0 finding).

When the run-wide GLOBAL in-flight cap is the limiter — not any one
producer's per-agent capacity — the deferred tuple must NOT attribute the
block to a specific producer (which may still have free per-agent slots).
The blocking fields are a sentinel ``("", 0)`` in that case, distinct from
the per-agent-capacity deferral which names the saturated producer + cap.

Additive, separate file (does not touch ``test_dispatch.py`` or
``test_dispatch_resweep.py``).
"""

from __future__ import annotations

from uuid import uuid4

from modulatio import dispatch, roster
from modulatio.types import Task


def _wtask(tid: str, skills: list[str]) -> Task:
    return Task(
        id=tid, project_id=uuid4(), goal_id="W-G-001",
        description=tid, required_skills=skills,
    )


def _wagent(id: str, skills: list[str], *, cap: int = 1,
            cost_class: str = "paid-cloud") -> roster.Agent:
    return roster.Agent(
        id=id, name=id, identity=f"{id} id", skills=skills,
        cost_class=cost_class, tier="producer", capacity_cap=cap,
    )


def test_global_cap_deferral_uses_sentinel_not_producer_capacity():
    """Producers have ample per-agent capacity (cap=5 each); the GLOBAL cap
    of 2 is what blocks the 3rd task. The deferred tuple must name the
    global limiter via the ("", 0) sentinel — NOT producer 'a' with cap=5,
    which still has free slots."""
    tasks = [_wtask(f"W-T-00{i}", ["drafter"]) for i in (1, 2, 3)]
    agents = [_wagent("a", ["drafter"], cap=5), _wagent("b", ["drafter"], cap=5)]
    sched = dispatch.schedule_wave(tasks, agents, global_in_flight_cap=2)

    assert len(sched.assignments) == 2
    assert len(sched.deferred) == 1
    blocked_task, blocking_agent, blocking_cap = sched.deferred[0]
    assert blocked_task == "W-T-003"
    # Global cap is the limiter — sentinel, not a specific producer/cap.
    assert blocking_agent == ""
    assert blocking_cap == 0


def test_per_agent_capacity_deferral_still_names_the_producer():
    """Guard the other branch is unchanged: when a producer's OWN per-agent
    capacity is the limiter (no global cap), the deferral still names that
    saturated producer and its cap — so the two reasons stay distinguishable."""
    tasks = [_wtask(f"W-T-00{i}", ["drafter"]) for i in (1, 2, 3)]
    agents = [_wagent("solo", ["drafter"], cap=1)]
    sched = dispatch.schedule_wave(tasks, agents)

    assert sched.assignments == {"W-T-001": "solo"}
    assert len(sched.deferred) == 2
    for _tid, blocking_agent, blocking_cap in sched.deferred:
        assert blocking_agent == "solo"
        assert blocking_cap == 1
