"""Tests for skill-based agent dispatch (slice #6c).

Dispatch is deterministic Python logic — runs in the Coordinator's
tactical execution phase right after ``_plan_tasks`` returns. No
new LLM calls. Given a task's ``required_skills`` and the project
roster, picks the single best agent, or returns ``None`` so the
orchestrator falls back to the hardcoded role safety net.

Ranking:
1. Tightest fit — fewer extra skills wins (specialized over generalist
   when both cover the requirement).
2. Cheapest cost_class — free-local < paid-cloud < premium-cloud.
3. Deterministic — lexicographic agent.id as a final tiebreak.

No filtering by model_tier in #6c; adding a tier-match policy is larger
than this slice should carry.
"""

from __future__ import annotations

from uuid import uuid4

from modulatio import dispatch, roster
from modulatio.types import Task


def _task(
    required_skills: list[str],
    required_capabilities: list[str] | None = None,
) -> Task:
    return Task(
        id="X-T-001",
        project_id=uuid4(),
        goal_id="X-G-001",
        description="anything",
        required_skills=required_skills,
        required_capabilities=required_capabilities or [],
    )


def _agent(
    id: str,
    skills: list[str],
    cost_class: str = "paid-cloud",
    tier: str = "producer",
    model_tier: str | None = None,
    capability_tags: list[str] | None = None,
) -> roster.Agent:
    return roster.Agent(
        id=id,
        name=id,
        identity=f"{id} identity",
        skills=skills,
        cost_class=cost_class,
        tier=tier,
        model_tier=model_tier,
        capability_tags=capability_tags or [],
    )


# ── fallback signals ───────────────────────────────────────────────────────

def test_select_returns_none_when_task_has_no_required_skills():
    """Empty required_skills = no routing constraint — orchestrator uses
    the hardcoded role path. Dispatch doesn't pick arbitrarily."""
    agents = [_agent("drafter", ["drafter"])]
    assert dispatch.select_agent(_task([]), agents) is None


def test_select_returns_none_when_roster_is_empty():
    """Cold project with no roster falls back. Dispatch never invents
    agents."""
    assert dispatch.select_agent(_task(["drafter"]), []) is None


def test_select_returns_none_when_no_agent_covers_required_skills():
    """No agent in the roster holds all the required skills → None.
    Capability ticket candidate (slice #6d), fallback for now."""
    agents = [
        _agent("drafter", ["drafter"]),
        _agent("researcher", ["researcher"]),
    ]
    assert dispatch.select_agent(_task(["wp-cli-admin"]), agents) is None


# ── covering candidates ────────────────────────────────────────────────────

def test_select_returns_the_only_qualifying_agent():
    agents = [
        _agent("drafter", ["drafter"]),
        _agent("researcher", ["researcher"]),
    ]
    picked = dispatch.select_agent(_task(["drafter"]), agents)
    assert picked is not None
    assert picked.id == "drafter"


def test_select_prefers_tightest_fit_over_generalist():
    """Two agents both cover; the one with fewer extra skills wins.
    A specialized drafter-only beats a multi-skill generalist for a
    drafter-only task."""
    specialist = _agent("drafter", ["drafter"])
    generalist = _agent("swiss-army", ["drafter", "researcher", "qc"])
    picked = dispatch.select_agent(_task(["drafter"]), [specialist, generalist])
    assert picked is not None
    assert picked.id == "drafter"


def test_select_prefers_broader_agent_when_task_actually_needs_the_breadth():
    """Inverse of the tight-fit test: when required_skills includes
    skills only the generalist holds, the generalist wins."""
    specialist = _agent("drafter", ["drafter"])
    generalist = _agent("swiss-army", ["drafter", "researcher", "qc"])
    picked = dispatch.select_agent(
        _task(["drafter", "researcher"]),
        [specialist, generalist],
    )
    assert picked is not None
    assert picked.id == "swiss-army"


def test_select_prefers_cheaper_cost_class_when_skill_fit_ties():
    """Two agents, identical tight fit, different cost_class — the
    cheaper agent wins. Mirrors the cron-agent policy (qwen local for
    utility, cloud only when needed)."""
    free = _agent("drafter-local", ["drafter"], cost_class="free-local")
    paid = _agent("drafter-cloud", ["drafter"], cost_class="paid-cloud")
    premium = _agent("drafter-premium", ["drafter"], cost_class="premium-cloud")
    picked = dispatch.select_agent(_task(["drafter"]), [premium, paid, free])
    assert picked is not None
    assert picked.id == "drafter-local"


def test_select_deterministic_by_id_when_all_other_factors_tie():
    """Two agents tied on skill fit AND cost_class — deterministic by
    id so dispatch is reproducible. No randomness, no flapping between
    runs."""
    a = _agent("a-drafter", ["drafter"])
    z = _agent("z-drafter", ["drafter"])
    picked = dispatch.select_agent(_task(["drafter"]), [z, a])
    assert picked is not None
    assert picked.id == "a-drafter"


# ── plan_dispatch outcomes (slice #6d) ─────────────────────────────────────

def test_plan_dispatch_no_constraint_when_required_skills_empty():
    """Empty required_skills → NO_CONSTRAINT. Orchestrator takes the
    hardcoded-role path; no ticket, no block. Matches #6c safety-net
    behavior for constraint-less tasks."""
    task = _task([])
    agents = [_agent("drafter", ["drafter"])]
    result = dispatch.plan_dispatch(task, agents, available_skill_names=["drafter"])
    assert result.outcome is dispatch.DispatchOutcome.NO_CONSTRAINT
    assert result.agent is None
    assert result.missing_skills == ()


def test_plan_dispatch_matched_when_agent_covers_valid_skills():
    task = _task(["drafter"])
    agents = [_agent("drafter", ["drafter"])]
    result = dispatch.plan_dispatch(task, agents, available_skill_names=["drafter"])
    assert result.outcome is dispatch.DispatchOutcome.MATCHED
    assert result.agent is not None
    assert result.agent.id == "drafter"
    assert result.missing_skills == ()


def test_plan_dispatch_invalid_skill_when_required_not_in_registry():
    """Coordinator LLM invented a skill name that doesn't exist in the
    registry — upstream hallucination. Classify distinctly from
    roster-gap so the ticket priority can be CRITICAL (dev fix) instead
    of BLOCKER (roster-decision)."""
    task = _task(["drafter", "hallucinated-skill"])
    agents = [_agent("drafter", ["drafter", "hallucinated-skill"])]
    result = dispatch.plan_dispatch(
        task, agents, available_skill_names=["drafter"]
    )
    assert result.outcome is dispatch.DispatchOutcome.INVALID_SKILL
    assert result.agent is None
    # missing_skills reports what wasn't in the registry.
    assert "hallucinated-skill" in result.missing_skills
    assert "drafter" not in result.missing_skills


def test_plan_dispatch_roster_gap_when_all_valid_but_no_cover():
    """All required_skills are in the registry, but no agent holds
    every one of them → ROSTER_GAP. Capability gap that needs a human
    roster decision, not a dev fix."""
    task = _task(["drafter", "wp-cli-admin"])
    agents = [
        _agent("drafter", ["drafter"]),  # covers drafter only
        _agent("sysadmin", ["linux-admin"]),  # covers neither
    ]
    result = dispatch.plan_dispatch(
        task, agents,
        available_skill_names=["drafter", "wp-cli-admin", "linux-admin"],
    )
    assert result.outcome is dispatch.DispatchOutcome.ROSTER_GAP
    assert result.agent is None
    # missing_skills reports skills held by NO agent at all — actionable
    # "install this" list. Skills held by some agent but not the right
    # agent don't appear (that's a composition problem, not a capability
    # gap).
    assert "wp-cli-admin" in result.missing_skills
    assert "drafter" not in result.missing_skills


def test_plan_dispatch_roster_gap_when_skills_scattered_across_agents():
    """Edge case: every required skill IS held by *some* agent, but no
    single agent has all of them. Still a ROSTER_GAP (the dispatcher
    can't pick), but missing_skills comes back empty — the resolution
    is compositional (combine skills on one agent), not capability
    acquisition. Body text disambiguates for the human."""
    task = _task(["drafter", "researcher"])
    agents = [
        _agent("d", ["drafter"]),
        _agent("r", ["researcher"]),
    ]
    result = dispatch.plan_dispatch(
        task, agents,
        available_skill_names=["drafter", "researcher"],
    )
    assert result.outcome is dispatch.DispatchOutcome.ROSTER_GAP
    assert result.missing_skills == ()  # every skill held by some agent


def test_plan_dispatch_empty_roster_with_valid_skills_is_roster_gap():
    """Empty roster + declared required_skills that exist in the registry
    → ROSTER_GAP. The skills are real; the project just has no agents
    yet. Human needs to compose the initial roster."""
    result = dispatch.plan_dispatch(
        _task(["drafter"]), [], available_skill_names=["drafter"]
    )
    assert result.outcome is dispatch.DispatchOutcome.ROSTER_GAP
    assert "drafter" in result.missing_skills


# ── plan_dispatch semantic layer (slice #6e.b) ─────────────────────────────

def test_plan_dispatch_semantic_matched_when_deterministic_misses_but_matcher_hits():
    """When deterministic skill-intersection finds no cover but the
    semantic matcher returns a hit, plan_dispatch returns
    SEMANTIC_MATCHED with the score. This is the embedding-fallback
    path that prevents ticket-flood for "close enough" matches."""
    task = _task(["long-form-production"])
    agent = _agent("custom-agent", ["drafter", "contrarian"])
    # Agent doesn't literally cover "long-form-production", but the matcher
    # decides it's semantically close.
    def matcher(t):
        return (agent, 0.82) if t is task else None

    result = dispatch.plan_dispatch(
        task, [agent], available_skill_names=["long-form-production"],
        semantic_matcher=matcher,
    )
    assert result.outcome is dispatch.DispatchOutcome.SEMANTIC_MATCHED
    assert result.agent is agent
    assert result.similarity_score == 0.82


def test_plan_dispatch_prefers_deterministic_match_over_semantic():
    """When deterministic finds a cover, don't even call the semantic
    matcher. Deterministic is cheaper and more trustworthy; semantic is
    a relaxation, not a replacement."""
    task = _task(["drafter"])
    agent = _agent("drafter-agent", ["drafter"])
    matcher_called = {"n": 0}

    def matcher(t):
        matcher_called["n"] += 1
        return None

    result = dispatch.plan_dispatch(
        task, [agent], available_skill_names=["drafter"],
        semantic_matcher=matcher,
    )
    assert result.outcome is dispatch.DispatchOutcome.MATCHED
    assert matcher_called["n"] == 0


def test_plan_dispatch_semantic_not_tried_on_invalid_skill():
    """INVALID_SKILL means the Coordinator hallucinated a skill name —
    upstream bug, CRITICAL ticket. Semantic fallback must NOT rescue
    this; papering over a hallucination ships wrong output."""
    task = _task(["made-up-skill"])
    agent = _agent("any", ["drafter"])
    matcher_called = {"n": 0}

    def matcher(t):
        matcher_called["n"] += 1
        return (agent, 0.99)

    result = dispatch.plan_dispatch(
        task, [agent], available_skill_names=["drafter"],
        semantic_matcher=matcher,
    )
    assert result.outcome is dispatch.DispatchOutcome.INVALID_SKILL
    assert matcher_called["n"] == 0


def test_plan_dispatch_semantic_not_tried_on_no_constraint():
    """Empty required_skills → no routing constraint. Hardcoded-role
    fallback already handles this. Don't consult the semantic matcher
    for constraint-less tasks."""
    task = _task([])
    agent = _agent("any", ["drafter"])
    matcher_called = {"n": 0}

    def matcher(t):
        matcher_called["n"] += 1
        return (agent, 0.99)

    result = dispatch.plan_dispatch(
        task, [agent], available_skill_names=["drafter"],
        semantic_matcher=matcher,
    )
    assert result.outcome is dispatch.DispatchOutcome.NO_CONSTRAINT
    assert matcher_called["n"] == 0


def test_plan_dispatch_roster_gap_when_semantic_matcher_also_misses():
    """Deterministic miss AND semantic miss → ROSTER_GAP ticket. This
    is the case where neither layer finds a home for the task; the
    human has to install a skill or create an agent."""
    task = _task(["drafter"])
    agent = _agent("unrelated", ["researcher"])
    def matcher(_t):
        return None

    result = dispatch.plan_dispatch(
        task, [agent], available_skill_names=["drafter", "researcher"],
        semantic_matcher=matcher,
    )
    assert result.outcome is dispatch.DispatchOutcome.ROSTER_GAP


def test_plan_dispatch_without_matcher_still_produces_roster_gap():
    """Back-compat: plan_dispatch without a semantic_matcher argument
    (or with None) behaves exactly like slice #6d — deterministic
    only, ROSTER_GAP on no cover."""
    task = _task(["drafter"])
    agent = _agent("unrelated", ["researcher"])

    result = dispatch.plan_dispatch(
        task, [agent], available_skill_names=["drafter", "researcher"],
    )
    assert result.outcome is dispatch.DispatchOutcome.ROSTER_GAP


def test_plan_dispatch_empty_roster_with_matcher_still_tries_matcher():
    """Empty roster but a matcher is present — let the matcher try
    (though it'll typically return None since the underlying index is
    empty). Important edge: a matcher could be backed by a shared index
    across projects some day; plan_dispatch shouldn't assume empty
    roster = no semantic chance."""
    task = _task(["drafter"])
    matcher_called = {"n": 0}

    def matcher(t):
        matcher_called["n"] += 1
        return None

    result = dispatch.plan_dispatch(
        task, [], available_skill_names=["drafter"], semantic_matcher=matcher,
    )
    assert result.outcome is dispatch.DispatchOutcome.ROSTER_GAP
    assert matcher_called["n"] == 1


# ── QC tier dispatch (slice #6f-F) ─────────────────────────────────────────

def test_select_qc_agent_returns_none_when_no_qc_tier_candidate():
    """No agent has tier='qc' → None → orchestrator falls back to the
    role-keyed 'qc' runner (current behavior preserved, different-mind
    still comes from --qc-model CLI flag)."""
    agents = [
        _agent("drafter", ["drafter"], tier="producer"),
        _agent("researcher", ["researcher"], tier="producer"),
    ]
    picked = dispatch.select_qc_agent(
        producer_agent_id="drafter",
        producer_model_tier="generalist",
        qc_candidates=agents,
    )
    assert picked is None


def test_select_qc_agent_filters_by_tier():
    """Only agents with tier='qc' are candidates. A producer-tier agent
    holding the 'qc' skill is not a QC candidate — structural rule, not
    skill-based."""
    agents = [
        _agent(
            "qc-kimi", ["qc"],
            tier="qc", model_tier="reasoning-heavy",
            cost_class="premium-cloud",
        ),
        # Holds the qc skill but tier is producer — not a QC candidate.
        _agent(
            "polyvalent", ["drafter", "qc"],
            tier="producer", model_tier="generalist",
        ),
    ]
    picked = dispatch.select_qc_agent(
        producer_agent_id="polyvalent",
        producer_model_tier="generalist",
        qc_candidates=agents,
    )
    assert picked is not None
    assert picked.id == "qc-kimi"


def test_select_qc_agent_excludes_producer_for_different_mind():
    """Even a qc-tier agent is excluded if it's the same agent that
    produced the artifact for this task. 'Different mind' is structural,
    not convention — enforced here."""
    agents = [
        # Same id as producer → excluded.
        _agent(
            "qc-kimi", ["qc"],
            tier="qc", model_tier="reasoning-heavy",
        ),
        _agent(
            "qc-grok", ["qc"],
            tier="qc", model_tier="reasoning-heavy",
        ),
    ]
    picked = dispatch.select_qc_agent(
        producer_agent_id="qc-kimi",
        producer_model_tier="generalist",
        qc_candidates=agents,
    )
    assert picked is not None
    assert picked.id == "qc-grok"


def test_select_qc_agent_enforces_capability_floor():
    """QC's model_tier must meet or exceed the producer's — 'weak judge
    cannot evaluate strong output.' A budget-tier QC against a
    reasoning-heavy producer is filtered out."""
    agents = [
        _agent("qc-weak", ["qc"], tier="qc", model_tier="budget"),
        _agent("qc-strong", ["qc"], tier="qc", model_tier="reasoning-heavy"),
    ]
    picked = dispatch.select_qc_agent(
        producer_agent_id="some-drafter",
        producer_model_tier="reasoning-heavy",
        qc_candidates=agents,
    )
    assert picked is not None
    assert picked.id == "qc-strong"


def test_select_qc_agent_capability_floor_allows_equal_tier():
    """Equal model_tier passes the floor — a generalist QC can verify
    a generalist producer. Only strictly-below is excluded."""
    agents = [
        _agent("qc-same", ["qc"], tier="qc", model_tier="generalist"),
    ]
    picked = dispatch.select_qc_agent(
        producer_agent_id="p1",
        producer_model_tier="generalist",
        qc_candidates=agents,
    )
    assert picked is not None
    assert picked.id == "qc-same"


def test_select_qc_agent_unknown_model_tier_passes_gracefully():
    """When either tier is unknown/uncomparable, the floor check passes
    rather than excluding the candidate — graceful degradation instead
    of brittle strictness. User can calibrate model_tier values later."""
    agents = [
        _agent("qc-unknown-tier", ["qc"], tier="qc", model_tier=None),
    ]
    picked = dispatch.select_qc_agent(
        producer_agent_id="p1",
        producer_model_tier="reasoning-heavy",
        qc_candidates=agents,
    )
    assert picked is not None


def test_select_qc_agent_tiebreak_by_cost_then_id():
    """When multiple qc-tier candidates pass all filters, pick cheapest
    cost_class first, then lexicographic id. Deterministic and
    reproducible."""
    agents = [
        _agent(
            "qc-premium-z", ["qc"],
            tier="qc", model_tier="reasoning-heavy",
            cost_class="premium-cloud",
        ),
        _agent(
            "qc-paid-z", ["qc"],
            tier="qc", model_tier="reasoning-heavy",
            cost_class="paid-cloud",
        ),
        _agent(
            "qc-paid-a", ["qc"],
            tier="qc", model_tier="reasoning-heavy",
            cost_class="paid-cloud",
        ),
    ]
    picked = dispatch.select_qc_agent(
        producer_agent_id="p1",
        producer_model_tier="generalist",
        qc_candidates=agents,
    )
    assert picked is not None
    # Cheapest tier (paid-cloud), then lexicographic id (qc-paid-a).
    assert picked.id == "qc-paid-a"


def test_plan_dispatch_empty_roster_with_invalid_skills_is_invalid_skill():
    """When a required skill isn't in the registry, classify as
    INVALID_SKILL regardless of roster state. Invalid-skill takes
    precedence — fix the upstream hallucination before worrying about
    roster composition."""
    result = dispatch.plan_dispatch(
        _task(["made-up"]), [], available_skill_names=["drafter"]
    )
    assert result.outcome is dispatch.DispatchOutcome.INVALID_SKILL
    assert "made-up" in result.missing_skills


# ── capability-tag filtering (slice #9a) ───────────────────────────────────

def test_select_agent_filters_by_required_capabilities():
    """Agent must cover ``required_skills`` AND ``required_capabilities``.
    Skills name WHAT the agent does; capabilities name HOW. An agent
    with the right skill but missing a required capability is not a
    candidate. Business-harness level — applies to any artifact class.
    """
    weak = _agent(
        "producer-budget", ["producer"],
        capability_tags=["generalist"],
    )
    strong = _agent(
        "producer-reasoning", ["producer"],
        capability_tags=["generalist", "reasoning-heavy"],
    )
    task = _task(["producer"], required_capabilities=["reasoning-heavy"])
    picked = dispatch.select_agent(task, [weak, strong])
    assert picked is not None
    assert picked.id == "producer-reasoning"


def test_select_agent_returns_none_when_skills_match_but_capabilities_miss():
    """Every agent covers the skill but none covers the required
    capability → no candidate. Orchestrator opens a ROSTER_GAP ticket
    via ``plan_dispatch`` (covered below)."""
    a = _agent(
        "producer-a", ["producer"],
        capability_tags=["generalist"],
    )
    b = _agent(
        "producer-b", ["producer"],
        capability_tags=["long-context"],
    )
    task = _task(["producer"], required_capabilities=["structured-output"])
    assert dispatch.select_agent(task, [a, b]) is None


def test_select_agent_vacuous_capabilities_does_not_filter():
    """Empty ``required_capabilities`` = no capability constraint. Skill
    cover alone picks the agent, matching pre-#9a behavior. Back-compat
    for every task that predates this slice."""
    agent = _agent(
        "producer-a", ["producer"],
        capability_tags=[],  # no declared capabilities
    )
    task = _task(["producer"])  # no required_capabilities
    picked = dispatch.select_agent(task, [agent])
    assert picked is not None
    assert picked.id == "producer-a"


def test_plan_dispatch_matched_when_skills_and_capabilities_both_cover():
    """Happy path: agent covers both axes → MATCHED. No missing fields
    populated on the result."""
    agent = _agent(
        "producer", ["producer"],
        capability_tags=["reasoning-heavy"],
    )
    task = _task(["producer"], required_capabilities=["reasoning-heavy"])
    result = dispatch.plan_dispatch(
        task, [agent], available_skill_names=["producer"],
    )
    assert result.outcome is dispatch.DispatchOutcome.MATCHED
    assert result.agent is agent
    assert result.missing_skills == ()
    assert result.missing_capabilities == ()


def test_plan_dispatch_roster_gap_when_capability_uncovered():
    """Skills covered, capability not held by any agent → ROSTER_GAP
    with ``missing_capabilities`` populated. Human resolution is same
    shape as a skills gap: install the capability on an existing agent,
    pick a different-tier model, or defer."""
    agent = _agent(
        "producer-budget", ["producer"],
        capability_tags=["generalist"],
    )
    task = _task(["producer"], required_capabilities=["reasoning-heavy"])
    result = dispatch.plan_dispatch(
        task, [agent], available_skill_names=["producer"],
    )
    assert result.outcome is dispatch.DispatchOutcome.ROSTER_GAP
    assert result.missing_capabilities == ("reasoning-heavy",)
    # Skills were covered — nothing reported on the skills axis.
    assert result.missing_skills == ()


def test_plan_dispatch_reports_both_gap_types_when_both_missing():
    """When both a required skill AND a required capability are absent
    from the roster, the result reports both actionable lists so the
    ticket body can surface both."""
    agent = _agent(
        "producer", ["producer"],
        capability_tags=["generalist"],
    )
    task = _task(
        ["producer", "shell-runner"],
        required_capabilities=["reasoning-heavy"],
    )
    result = dispatch.plan_dispatch(
        task, [agent],
        available_skill_names=["producer", "shell-runner"],
    )
    assert result.outcome is dispatch.DispatchOutcome.ROSTER_GAP
    assert "shell-runner" in result.missing_skills
    assert result.missing_capabilities == ("reasoning-heavy",)


def test_plan_dispatch_semantic_hit_filtered_by_capability():
    """Semantic fallback that returns an agent lacking a required
    capability is NOT rescued — capabilities are a hard human-configured
    filter. Falls through to ROSTER_GAP with missing_capabilities."""
    task = _task(
        ["long-form-production"],  # not literally covered by any agent
        required_capabilities=["reasoning-heavy"],
    )
    semantic_pick = _agent(
        "producer", ["producer"],
        capability_tags=["generalist"],  # missing reasoning-heavy
    )
    def matcher(t):
        return (semantic_pick, 0.82) if t is task else None

    result = dispatch.plan_dispatch(
        task, [semantic_pick],
        available_skill_names=["long-form-production"],
        semantic_matcher=matcher,
    )
    assert result.outcome is dispatch.DispatchOutcome.ROSTER_GAP
    assert result.missing_capabilities == ("reasoning-heavy",)
    assert result.agent is None


def test_plan_dispatch_semantic_hit_with_capability_cover_succeeds():
    """Semantic match whose agent DOES cover the required capabilities
    → SEMANTIC_MATCHED. Capabilities filter doesn't block — it's a
    hard gate, and this candidate passes it."""
    task = _task(
        ["long-form-production"],
        required_capabilities=["reasoning-heavy"],
    )
    semantic_pick = _agent(
        "producer", ["producer"],
        capability_tags=["reasoning-heavy", "long-context"],
    )
    def matcher(t):
        return (semantic_pick, 0.90) if t is task else None

    result = dispatch.plan_dispatch(
        task, [semantic_pick],
        available_skill_names=["long-form-production"],
        semantic_matcher=matcher,
    )
    assert result.outcome is dispatch.DispatchOutcome.SEMANTIC_MATCHED
    assert result.agent is semantic_pick
    assert result.similarity_score == 0.90


def test_select_treats_unknown_cost_class_as_most_expensive():
    """Agents without a declared cost_class rank last among cost tiers
    — a known cost_class always beats an unknown. Unknown-is-expensive
    is the safe default (don't accidentally route to an agent we can't
    reason about the cost of)."""
    known = _agent("drafter-known", ["drafter"], cost_class="paid-cloud")
    unknown = roster.Agent(
        id="drafter-unknown",
        name="unknown",
        identity="x",
        skills=["drafter"],
        cost_class=None,
    )
    picked = dispatch.select_agent(_task(["drafter"]), [known, unknown])
    assert picked is not None
    assert picked.id == "drafter-known"


# ── skill-level capability floor (slice #9b) ──────────────────────────────

def _floor(floors: dict[str, tuple[str, ...]]):
    """Build a tiny skill_floor_for lookup from a plain dict. Skills not
    in the dict get an empty tuple — back-compat default."""
    def _f(skill_name: str) -> tuple[str, ...]:
        return floors.get(skill_name, ())
    return _f


def test_select_agent_filters_by_skill_declared_capability_floor():
    """A skill can declare a capability floor on its executing agent.
    When task.required_capabilities is empty, the skill-floor still
    applies — an agent missing the floor capability is excluded even
    though the Coordinator didn't ask for that capability explicitly.
    Business-harness level: works for any skill of any artifact class.
    """
    weak = _agent(
        "runner-no-shell", ["shell-runner"],
        capability_tags=["generalist"],
    )
    strong = _agent(
        "runner-shell", ["shell-runner"],
        capability_tags=["generalist", "shell-access"],
    )
    task = _task(["shell-runner"])  # no required_capabilities declared
    skill_floor_for = _floor({"shell-runner": ("shell-access",)})
    picked = dispatch.select_agent(
        task, [weak, strong], skill_floor_for=skill_floor_for
    )
    assert picked is not None
    assert picked.id == "runner-shell"


def test_select_agent_unions_task_caps_with_skill_floor():
    """When both task.required_capabilities AND a skill floor declare
    capabilities, dispatch filters by the union. An agent missing any
    required cap from either source is excluded."""
    only_shell = _agent(
        "a", ["shell-runner"],
        capability_tags=["shell-access"],
    )
    only_structured = _agent(
        "b", ["shell-runner"],
        capability_tags=["structured-output"],
    )
    both = _agent(
        "c", ["shell-runner"],
        capability_tags=["shell-access", "structured-output"],
    )
    # Task declares one cap; skill floor adds another.
    task = _task(["shell-runner"], required_capabilities=["structured-output"])
    skill_floor_for = _floor({"shell-runner": ("shell-access",)})
    picked = dispatch.select_agent(
        task, [only_shell, only_structured, both],
        skill_floor_for=skill_floor_for,
    )
    assert picked is not None
    assert picked.id == "c"


def test_select_agent_no_floor_callable_preserves_pre_9b_behavior():
    """Back-compat: when skill_floor_for is None (or omitted), dispatch
    ignores skill floors entirely. Every pre-#9b call site keeps working.
    """
    weak = _agent(
        "runner-no-shell", ["shell-runner"],
        capability_tags=["generalist"],
    )
    task = _task(["shell-runner"])  # no required_capabilities
    # No skill_floor_for passed → weak agent is a valid candidate.
    picked = dispatch.select_agent(task, [weak])
    assert picked is not None
    assert picked.id == "runner-no-shell"


def test_select_agent_unions_floors_across_multiple_required_skills():
    """When a task requires multiple skills, the effective floor is the
    UNION of each skill's declared floor. An agent must meet every
    union entry."""
    partial = _agent(
        "a", ["shell-runner", "structured-producer"],
        capability_tags=["shell-access"],  # missing structured-output
    )
    full = _agent(
        "b", ["shell-runner", "structured-producer"],
        capability_tags=["shell-access", "structured-output"],
    )
    task = _task(["shell-runner", "structured-producer"])
    skill_floor_for = _floor({
        "shell-runner": ("shell-access",),
        "structured-producer": ("structured-output",),
    })
    picked = dispatch.select_agent(
        task, [partial, full], skill_floor_for=skill_floor_for
    )
    assert picked is not None
    assert picked.id == "b"


def test_plan_dispatch_roster_gap_when_skill_floor_not_met():
    """A skill-floor gap surfaces as ROSTER_GAP with missing_capabilities
    populated. Ticket body (orchestrator concern) reads the capability
    axis; the dispatch layer doesn't distinguish floor-derived vs
    task-declared — same resolution for the human."""
    weak = _agent(
        "runner-no-shell", ["shell-runner"],
        capability_tags=["generalist"],
    )
    task = _task(["shell-runner"])
    skill_floor_for = _floor({"shell-runner": ("shell-access",)})
    result = dispatch.plan_dispatch(
        task, [weak],
        available_skill_names=["shell-runner"],
        skill_floor_for=skill_floor_for,
    )
    assert result.outcome is dispatch.DispatchOutcome.ROSTER_GAP
    assert result.missing_capabilities == ("shell-access",)


def test_plan_dispatch_semantic_hit_gated_by_skill_floor():
    """Semantic hit whose agent fails a skill-declared floor does NOT
    rescue. Hard filter, consistent with #9a: capability floors are
    human-configured constraints that semantic similarity can't bypass."""
    task = _task(["long-form-production"])  # not literally covered
    semantic_pick = _agent(
        "producer", ["producer"],
        capability_tags=["generalist"],  # missing reasoning-heavy
    )
    def matcher(t):
        return (semantic_pick, 0.88) if t is task else None
    skill_floor_for = _floor({"long-form-production": ("reasoning-heavy",)})

    result = dispatch.plan_dispatch(
        task, [semantic_pick],
        available_skill_names=["long-form-production"],
        semantic_matcher=matcher,
        skill_floor_for=skill_floor_for,
    )
    assert result.outcome is dispatch.DispatchOutcome.ROSTER_GAP
    assert result.missing_capabilities == ("reasoning-heavy",)


def test_plan_dispatch_matched_when_skill_floor_is_met():
    """Happy path: agent covers skills AND meets the skill-declared
    floor. MATCHED with no missing fields on either axis."""
    agent = _agent(
        "runner-shell", ["shell-runner"],
        capability_tags=["shell-access"],
    )
    task = _task(["shell-runner"])
    skill_floor_for = _floor({"shell-runner": ("shell-access",)})
    result = dispatch.plan_dispatch(
        task, [agent],
        available_skill_names=["shell-runner"],
        skill_floor_for=skill_floor_for,
    )
    assert result.outcome is dispatch.DispatchOutcome.MATCHED
    assert result.agent is agent
    assert result.missing_capabilities == ()


# ── domain-level capability floor (slice #9b follow-on) ────────────────────

def _domain_floor(floors: dict[str, tuple[str, ...]]):
    """Build a domain_floor_for lookup from a plain dict keyed by
    artifact_kind. Unknown kinds get an empty tuple."""
    def _f(artifact_kind: str) -> tuple[str, ...]:
        return floors.get(artifact_kind, ())
    return _f


def test_select_agent_applies_domain_floor_from_artifact_kind():
    """A standards domain floor applies to every task of that
    artifact_kind, regardless of which specific skill runs it. Agent
    must cover the domain-declared capability in addition to task and
    skill floors."""
    weak = _agent(
        "producer-a", ["producer"],
        capability_tags=["generalist"],
    )
    strong = _agent(
        "producer-b", ["producer"],
        capability_tags=["generalist", "structured-output"],
    )
    task = _task(["producer"])
    task.artifact_kind = "code"
    picked = dispatch.select_agent(
        task, [weak, strong],
        domain_floor_for=_domain_floor({"code": ("structured-output",)}),
    )
    assert picked is not None
    assert picked.id == "producer-b"


def test_effective_capabilities_union_task_skill_and_domain_floors():
    """The effective capability filter is the union of (a) task-level
    required_capabilities, (b) each required skill's floor, and (c)
    the domain-level floor from standards. Any source's requirement
    stands; an agent missing any of them is not a candidate."""
    partial = _agent(
        "a", ["producer"],
        # Has task cap + skill-floor cap, MISSING domain-floor cap.
        capability_tags=["task-cap", "skill-floor-cap"],
    )
    full = _agent(
        "b", ["producer"],
        capability_tags=["task-cap", "skill-floor-cap", "domain-floor-cap"],
    )
    task = _task(["producer"], required_capabilities=["task-cap"])
    task.artifact_kind = "research"
    skill_floor_for = _floor({"producer": ("skill-floor-cap",)})
    domain_floor_for = _domain_floor({"research": ("domain-floor-cap",)})
    picked = dispatch.select_agent(
        task, [partial, full],
        skill_floor_for=skill_floor_for,
        domain_floor_for=domain_floor_for,
    )
    assert picked is not None
    assert picked.id == "b"


def test_plan_dispatch_roster_gap_when_domain_floor_unmet():
    """Domain-level floor gap surfaces as ROSTER_GAP with
    missing_capabilities populated — same ticket shape as task-level
    or skill-level gaps. Dispatch doesn't distinguish the source; the
    ticket just names the missing capability."""
    agent = _agent(
        "producer", ["producer"],
        capability_tags=["generalist"],
    )
    task = _task(["producer"])
    task.artifact_kind = "code"
    domain_floor_for = _domain_floor({"code": ("structured-output",)})
    result = dispatch.plan_dispatch(
        task, [agent],
        available_skill_names=["producer"],
        domain_floor_for=domain_floor_for,
    )
    assert result.outcome is dispatch.DispatchOutcome.ROSTER_GAP
    assert "structured-output" in result.missing_capabilities


def test_plan_dispatch_no_domain_floor_callable_preserves_pre_followon_behavior():
    """Back-compat: when domain_floor_for is None (default), dispatch
    ignores the domain-level axis. Every pre-follow-on call site keeps
    working unchanged."""
    agent = _agent(
        "producer", ["producer"],
        capability_tags=[],
    )
    task = _task(["producer"])
    task.artifact_kind = "code"
    result = dispatch.plan_dispatch(
        task, [agent],
        available_skill_names=["producer"],
    )
    assert result.outcome is dispatch.DispatchOutcome.MATCHED


def test_semantic_hit_gated_by_domain_floor():
    """A semantic-matched agent missing a domain-declared capability
    falls through to ROSTER_GAP. Domain floors are hard, same as task
    and skill floors."""
    task = _task(["long-form-production"])
    task.artifact_kind = "research"
    semantic_pick = _agent(
        "producer", ["producer"],
        capability_tags=["generalist"],
    )
    def matcher(t):
        return (semantic_pick, 0.85) if t is task else None
    domain_floor_for = _domain_floor({"research": ("long-context",)})
    result = dispatch.plan_dispatch(
        task, [semantic_pick],
        available_skill_names=["long-form-production"],
        semantic_matcher=matcher,
        domain_floor_for=domain_floor_for,
    )
    assert result.outcome is dispatch.DispatchOutcome.ROSTER_GAP
    assert "long-context" in result.missing_capabilities


# ── producer escalation (slice #9c) ────────────────────────────────────────

def test_select_escalation_agent_picks_strictly_higher_tier():
    """Escalation helper picks the cheapest, deterministic candidate
    whose model_tier is STRICTLY higher than the current producer's.
    Same-tier candidates are not escalations — escalation means a
    different mind with more capability."""
    current = _agent(
        "producer-generalist", ["producer"],
        model_tier="generalist",
        capability_tags=["generalist"],
    )
    same_tier = _agent(
        "producer-another-generalist", ["producer"],
        model_tier="generalist",
        capability_tags=["generalist"],
    )
    higher = _agent(
        "producer-reasoning", ["producer"],
        model_tier="reasoning-heavy",
        capability_tags=["generalist", "reasoning-heavy"],
    )
    task = _task(["producer"])
    picked = dispatch.select_escalation_agent(
        task=task,
        current_agent_id=current.id,
        current_model_tier="generalist",
        agents=[current, same_tier, higher],
    )
    assert picked is not None
    assert picked.id == "producer-reasoning"


def test_select_escalation_agent_returns_none_when_no_higher_tier():
    """When no agent in the roster has a strictly-higher tier, escalation
    is not possible — return None. Orchestrator settles QC_REJECTED as
    today."""
    current = _agent(
        "producer-reasoning", ["producer"],
        model_tier="reasoning-heavy",
    )
    peer = _agent(
        "producer-peer", ["producer"],
        model_tier="reasoning-heavy",
    )
    task = _task(["producer"])
    picked = dispatch.select_escalation_agent(
        task=task,
        current_agent_id=current.id,
        current_model_tier="reasoning-heavy",
        agents=[current, peer],
    )
    assert picked is None


def test_select_escalation_agent_excludes_current_agent():
    """Even if the current agent's tier would trivially satisfy
    'strictly higher than itself' via comparison bug, escalation must
    never re-pick the same agent. Same-mind retry is not escalation."""
    current = _agent(
        "producer-generalist", ["producer"],
        model_tier="generalist",
    )
    higher = _agent(
        "producer-strategic", ["producer"],
        model_tier="strategic",
    )
    task = _task(["producer"])
    picked = dispatch.select_escalation_agent(
        task=task,
        current_agent_id="producer-generalist",
        current_model_tier="generalist",
        agents=[current, higher],
    )
    assert picked is not None
    assert picked.id != "producer-generalist"
    assert picked.id == "producer-strategic"


def test_select_escalation_agent_enforces_skill_cover():
    """Escalation respects the same skill-cover constraint as first-pick
    dispatch. A higher-tier agent that doesn't hold the required skill
    is not a valid escalation target."""
    current = _agent(
        "producer-generalist", ["producer"],
        model_tier="generalist",
    )
    # Higher tier but wrong skill — not a valid escalation.
    wrong_skill = _agent(
        "researcher-strategic", ["researcher"],
        model_tier="strategic",
    )
    right_skill_higher = _agent(
        "producer-strategic", ["producer"],
        model_tier="strategic",
    )
    task = _task(["producer"])
    picked = dispatch.select_escalation_agent(
        task=task,
        current_agent_id=current.id,
        current_model_tier="generalist",
        agents=[current, wrong_skill, right_skill_higher],
    )
    assert picked is not None
    assert picked.id == "producer-strategic"


def test_select_escalation_agent_enforces_capability_floor():
    """Escalation respects both task-level required_capabilities AND the
    #9b skill-declared floor. A higher-tier agent missing any effective
    capability is filtered out — escalation is NOT a relaxation of the
    capability gate."""
    current = _agent(
        "producer-generalist", ["producer"],
        model_tier="generalist",
        capability_tags=["generalist"],
    )
    # Higher tier but missing the required capability.
    higher_incomplete = _agent(
        "producer-strategic-incomplete", ["producer"],
        model_tier="strategic",
        capability_tags=["generalist"],  # missing reasoning-heavy
    )
    higher_complete = _agent(
        "producer-strategic-complete", ["producer"],
        model_tier="strategic",
        capability_tags=["generalist", "reasoning-heavy"],
    )
    task = _task(["producer"], required_capabilities=["reasoning-heavy"])
    picked = dispatch.select_escalation_agent(
        task=task,
        current_agent_id=current.id,
        current_model_tier="generalist",
        agents=[current, higher_incomplete, higher_complete],
    )
    assert picked is not None
    assert picked.id == "producer-strategic-complete"


def test_select_escalation_agent_respects_skill_floor_callable():
    """Skill-floor capabilities (slice #9b) apply to escalation candidates
    just like first-pick dispatch. An escalation target missing a
    skill-declared floor capability is filtered out."""
    current = _agent(
        "runner-generalist", ["shell-runner"],
        model_tier="generalist",
        capability_tags=["shell-access"],
    )
    higher_no_shell = _agent(
        "runner-strategic-no-shell", ["shell-runner"],
        model_tier="strategic",
        capability_tags=["generalist"],  # missing shell-access floor
    )
    higher_shell = _agent(
        "runner-strategic-shell", ["shell-runner"],
        model_tier="strategic",
        capability_tags=["shell-access"],
    )
    task = _task(["shell-runner"])
    skill_floor_for = _floor({"shell-runner": ("shell-access",)})
    picked = dispatch.select_escalation_agent(
        task=task,
        current_agent_id=current.id,
        current_model_tier="generalist",
        agents=[current, higher_no_shell, higher_shell],
        skill_floor_for=skill_floor_for,
    )
    assert picked is not None
    assert picked.id == "runner-strategic-shell"


def test_select_escalation_agent_unknown_current_tier_returns_none():
    """When the current tier is unknown or uncomparable, 'strictly higher'
    is undefined — return None. Consistent with #6f-F graceful
    degradation: don't invent an ordering we can't reason about."""
    current = _agent(
        "producer-mystery", ["producer"],
        model_tier="mystery-tier",  # not in _TIER_RANK
    )
    higher = _agent(
        "producer-strategic", ["producer"],
        model_tier="strategic",
    )
    task = _task(["producer"])
    picked = dispatch.select_escalation_agent(
        task=task,
        current_agent_id=current.id,
        current_model_tier="mystery-tier",
        agents=[current, higher],
    )
    assert picked is None


def test_select_escalation_agent_cheapest_higher_tier_wins():
    """Among strictly-higher-tier candidates, prefer the cheapest cost
    class, then lexicographic id. Deterministic and reproducible."""
    current = _agent(
        "producer-generalist", ["producer"],
        model_tier="generalist",
    )
    premium = _agent(
        "producer-strategic-z", ["producer"],
        model_tier="strategic",
        cost_class="premium-cloud",
    )
    paid_b = _agent(
        "producer-strategic-b", ["producer"],
        model_tier="strategic",
        cost_class="paid-cloud",
    )
    paid_a = _agent(
        "producer-strategic-a", ["producer"],
        model_tier="strategic",
        cost_class="paid-cloud",
    )
    task = _task(["producer"])
    picked = dispatch.select_escalation_agent(
        task=task,
        current_agent_id=current.id,
        current_model_tier="generalist",
        agents=[current, premium, paid_b, paid_a],
    )
    assert picked is not None
    # Cheapest tier (paid-cloud), then lexicographic id.
    assert picked.id == "producer-strategic-a"


def test_select_agent_honors_continuity_hint_when_qualified():
    """Pre-V2 Slice D: when ``task.preferred_continuity_agent`` names a
    qualifying agent (covers skills + caps), pick them over the
    cheaper/lex-first alternative. Same engineer who wrote engine.py
    keeps writing combat.py — they have the prior file in their
    working memory."""
    cheaper = _agent("engineer-a", ["coding"], cost_class="free-local")
    pricier = _agent("engineer-b", ["coding"], cost_class="paid-cloud")
    task = _task(["coding"])
    # No hint → cheaper wins by sort_key.
    assert dispatch.select_agent(task, [cheaper, pricier]).id == "engineer-a"
    # Hint pointing at the pricier qualifying agent → hint wins.
    task.preferred_continuity_agent = "engineer-b"
    assert dispatch.select_agent(task, [cheaper, pricier]).id == "engineer-b"


def test_select_agent_ignores_continuity_hint_when_unqualified():
    """Hinted agent that doesn't cover the required skills is silently
    ignored — falls through to normal selection. A continuity hint is
    advisory; can't override the dispatch correctness floor."""
    fits = _agent("engineer-a", ["coding"])
    misfit = _agent("writer-b", ["drafter"])  # lacks 'coding'
    task = _task(["coding"])
    task.preferred_continuity_agent = "writer-b"
    # Hint points at unqualified agent → fall through.
    picked = dispatch.select_agent(task, [fits, misfit])
    assert picked is not None
    assert picked.id == "engineer-a"


def test_select_agent_ignores_continuity_hint_pointing_at_nonexistent_agent():
    """Defensive: hint pointing at an id not in the roster (agent left
    the team, hint stale from a prior run) → ignored, normal selection
    runs. No crash, no error."""
    fits = _agent("engineer-a", ["coding"])
    task = _task(["coding"])
    task.preferred_continuity_agent = "engineer-ghost"
    picked = dispatch.select_agent(task, [fits])
    assert picked is not None
    assert picked.id == "engineer-a"


# ── _propagate_continuity_hint (Slice D upstream — orchestration helper) ──
#
# Audit fix 2026-05-02: select_agent's hint-honoring path was tested
# above, but the orchestrator step that *populates* the hint from a
# task's depends_on chain wasn't. Lifted out of the dispatch loop into
# a unit-testable helper so this layer is verified end-to-end.


def _hint_task(
    task_id: str,
    depends_on: list[str] | None = None,
    preferred_continuity_agent: str | None = None,
    assigned_agent_id: str | None = None,
) -> Task:
    """Build a Task with the fields the propagation helper actually reads."""
    t = Task(
        id=task_id,
        project_id=uuid4(),
        goal_id="X-G-001",
        description="anything",
        required_skills=["coding"],
        required_capabilities=[],
    )
    t.depends_on = list(depends_on or [])
    t.preferred_continuity_agent = preferred_continuity_agent
    t.assigned_agent_id = assigned_agent_id
    return t


def test_propagate_continuity_hint_stamps_from_first_dispatched_dep():
    """When a task has deps and no own hint, the helper stamps the
    first dep's assigned_agent_id as the continuity hint."""
    from modulatio.orchestration import _propagate_continuity_hint

    dep = _hint_task("X-T-001", assigned_agent_id="engineer-b")
    target = _hint_task("X-T-002", depends_on=["X-T-001"])
    id_to_task = {dep.id: dep, target.id: target}

    _propagate_continuity_hint(target, id_to_task)
    assert target.preferred_continuity_agent == "engineer-b"


def test_propagate_continuity_hint_skips_when_no_deps():
    from modulatio.orchestration import _propagate_continuity_hint

    target = _hint_task("X-T-001")
    _propagate_continuity_hint(target, {target.id: target})
    assert target.preferred_continuity_agent is None


def test_propagate_continuity_hint_preserves_existing_hint():
    """If the caller already set a hint (explicit Coordinator override),
    propagation does NOT overwrite it."""
    from modulatio.orchestration import _propagate_continuity_hint

    dep = _hint_task("X-T-001", assigned_agent_id="engineer-b")
    target = _hint_task(
        "X-T-002",
        depends_on=["X-T-001"],
        preferred_continuity_agent="writer-c",
    )
    _propagate_continuity_hint(target, {dep.id: dep, target.id: target})
    assert target.preferred_continuity_agent == "writer-c"


def test_propagate_continuity_hint_picks_first_dep_with_agent():
    """Multiple deps, only some dispatched: pick the first one with an
    assigned_agent_id (deps are walked in declared order)."""
    from modulatio.orchestration import _propagate_continuity_hint

    dep1 = _hint_task("X-T-001", assigned_agent_id=None)  # not dispatched
    dep2 = _hint_task("X-T-002", assigned_agent_id="engineer-b")
    dep3 = _hint_task("X-T-003", assigned_agent_id="engineer-c")
    target = _hint_task(
        "X-T-004", depends_on=["X-T-001", "X-T-002", "X-T-003"]
    )
    id_to_task = {t.id: t for t in (dep1, dep2, dep3, target)}

    _propagate_continuity_hint(target, id_to_task)
    # First dep with an assigned agent is dep2.
    assert target.preferred_continuity_agent == "engineer-b"


def test_propagate_continuity_hint_no_op_when_dep_missing_from_map():
    """Stale dep_id (dep was dropped from the dispatch list) → no crash,
    no hint stamped."""
    from modulatio.orchestration import _propagate_continuity_hint

    target = _hint_task("X-T-002", depends_on=["X-T-NONEXISTENT"])
    _propagate_continuity_hint(target, {target.id: target})
    assert target.preferred_continuity_agent is None


# ── _audit_class_qc_fallback (peer-QC routing for audit tasks) ─────────────
#
# Audit fix 2026-05-02: the predicate is_audit_class_artifact_kind was
# unit-tested above; the orchestrator branch that consumes it (when
# peer-QC selection fails on an audit-class task, route to leader)
# wasn't. Lifted into a testable helper.


def test_audit_class_qc_fallback_picks_leader_when_audit_kind():
    """Happy path: artifact_kind is audit-class, roster has a leader →
    return leader's id so verification doesn't self-review."""
    from modulatio.orchestration import _audit_class_qc_fallback

    leader = _agent("leader-a", ["leader-skill"], tier="leader")
    drafter = _agent("drafter-a", ["coding"], tier="producer")
    fallback_id = _audit_class_qc_fallback("audit", [leader, drafter])
    assert fallback_id == "leader-a"


def test_audit_class_qc_fallback_returns_none_for_non_audit_kind():
    """Non-audit-class artifacts fall through to the role-keyed qc
    runner — that's the right shape for regular drafts; only audit-class
    tasks need the leader fallback."""
    from modulatio.orchestration import _audit_class_qc_fallback

    leader = _agent("leader-a", ["leader-skill"], tier="leader")
    drafter = _agent("drafter-a", ["coding"], tier="producer")
    assert _audit_class_qc_fallback("essay", [leader, drafter]) is None
    assert _audit_class_qc_fallback("draft", [leader, drafter]) is None
    assert _audit_class_qc_fallback("article", [leader, drafter]) is None


def test_audit_class_qc_fallback_returns_none_when_no_leader_in_roster():
    """Degenerate roster: no leader-tier agent. Helper returns None;
    orchestrator then falls through to role-keyed qc runner (least-bad,
    same as non-audit). No crash, no exception."""
    from modulatio.orchestration import _audit_class_qc_fallback

    drafter_only = _agent("drafter-a", ["coding"], tier="producer")
    assert _audit_class_qc_fallback("audit", [drafter_only]) is None


def test_audit_class_qc_fallback_handles_audit_kind_variations():
    """Predicate already matches case + whitespace + audit family
    (audit / audit-report / consistency-audit / synthesis / review).
    Confirm the fallback fires for each."""
    from modulatio.orchestration import _audit_class_qc_fallback

    leader = _agent("leader-a", ["leader-skill"], tier="leader")
    for kind in ("audit", "audit-report", "consistency-audit", "synthesis", "review"):
        assert _audit_class_qc_fallback(kind, [leader]) == "leader-a"


def test_is_audit_class_artifact_kind_membership():
    """Audit-class kinds resolve True; unrelated kinds resolve False.
    Case + whitespace tolerant. Drives orchestrator's peer-QC-fallback
    branch for audit/synthesis/review tasks on small teams."""
    assert dispatch.is_audit_class_artifact_kind("audit") is True
    assert dispatch.is_audit_class_artifact_kind("audit-report") is True
    assert dispatch.is_audit_class_artifact_kind("CONSISTENCY-AUDIT") is True
    assert dispatch.is_audit_class_artifact_kind(" synthesis ") is True
    assert dispatch.is_audit_class_artifact_kind("review") is True
    assert dispatch.is_audit_class_artifact_kind("review-report") is True

    assert dispatch.is_audit_class_artifact_kind("brief") is False
    assert dispatch.is_audit_class_artifact_kind("python-module") is False
    assert dispatch.is_audit_class_artifact_kind("") is False
    assert dispatch.is_audit_class_artifact_kind(None) is False



# ── Core rebuild B2: capacity-aware wave scheduler ──────────────────────


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


def test_schedule_wave_allocates_independent_tasks_to_distinct_agents():
    tasks = [_wtask("W-T-001", ["drafter"]), _wtask("W-T-002", ["researcher"])]
    agents = [_wagent("d", ["drafter"]), _wagent("r", ["researcher"])]
    sched = dispatch.schedule_wave(tasks, agents)
    assert sched.assignments == {"W-T-001": "d", "W-T-002": "r"}
    assert sched.deferred == ()
    assert sched.gaps == ()


def test_schedule_wave_defers_when_one_cheapest_agent_overloaded():
    """Nemo's hazard: 3 ready tasks all fit the SAME cheapest specialist
    (cap=1). One is scheduled; the other two are DEFERRED_CAPACITY — NOT
    assigned to the same agent (which would silently serialize), and NOT
    a roster gap."""
    tasks = [_wtask(f"W-T-00{i}", ["drafter"]) for i in (1, 2, 3)]
    agents = [_wagent("solo", ["drafter"], cap=1)]
    sched = dispatch.schedule_wave(tasks, agents)
    assert sched.assignments == {"W-T-001": "solo"}
    deferred_ids = {d[0] for d in sched.deferred}
    assert deferred_ids == {"W-T-002", "W-T-003"}
    # deferred carries blocking agent id + its cap
    assert all(d[1] == "solo" and d[2] == 1 for d in sched.deferred)
    assert sched.gaps == ()


def test_schedule_wave_load_balances_across_qualifying_agents():
    """Two qualifying agents (cap=1 each) + 3 tasks → 2 scheduled to
    DIFFERENT agents, 1 deferred. Capacity is consumed across the roster,
    not piled on the cheapest."""
    tasks = [_wtask(f"W-T-00{i}", ["drafter"]) for i in (1, 2, 3)]
    agents = [_wagent("a", ["drafter"], cap=1), _wagent("b", ["drafter"], cap=1)]
    sched = dispatch.schedule_wave(tasks, agents)
    assert len(sched.assignments) == 2
    assert set(sched.assignments.values()) == {"a", "b"}
    assert len(sched.deferred) == 1


def test_schedule_wave_capacity_cap_gt_one_takes_multiple():
    tasks = [_wtask(f"W-T-00{i}", ["drafter"]) for i in (1, 2, 3)]
    agents = [_wagent("big", ["drafter"], cap=2)]
    sched = dispatch.schedule_wave(tasks, agents)
    assert sched.assignments == {"W-T-001": "big", "W-T-002": "big"}
    assert {d[0] for d in sched.deferred} == {"W-T-003"}


def test_schedule_wave_roster_gap_when_no_qualifying_agent():
    tasks = [_wtask("W-T-001", ["wp-cli-admin"])]
    agents = [_wagent("d", ["drafter"])]
    sched = dispatch.schedule_wave(tasks, agents)
    assert sched.assignments == {}
    assert sched.deferred == ()
    assert sched.gaps == ("W-T-001",)


def test_schedule_wave_global_cap_limits_total():
    tasks = [_wtask(f"W-T-00{i}", ["drafter"]) for i in (1, 2, 3)]
    agents = [_wagent("a", ["drafter"], cap=5), _wagent("b", ["drafter"], cap=5)]
    sched = dispatch.schedule_wave(tasks, agents, global_in_flight_cap=2)
    assert len(sched.assignments) == 2
    assert len(sched.deferred) == 1


def test_schedule_wave_skips_no_constraint_tasks():
    """Empty required_skills (legacy NO_CONSTRAINT) are not skill-scheduled
    — neither scheduled, deferred, nor gapped here (caller's legacy path)."""
    tasks = [_wtask("W-T-001", []), _wtask("W-T-002", ["drafter"])]
    agents = [_wagent("d", ["drafter"])]
    sched = dispatch.schedule_wave(tasks, agents)
    assert sched.assignments == {"W-T-002": "d"}
    assert "W-T-001" not in sched.assignments
    assert sched.gaps == ()
    assert sched.deferred == ()


def test_schedule_wave_respects_skill_capability_floor():
    """Nemo impl-sweep B2: the wave scheduler must apply skill/domain
    capability floors, so a capacity rebalance can't hand a task to a
    cheaper agent that covers the skill name but NOT the skill's hidden
    floor. With the floor passed, the floored (pricier) agent wins; without
    it, the cheaper under-floor agent would — proving the floor is enforced."""
    def _floor(name: str):
        return ("reasoning-heavy",) if name == "drafter" else ()

    task = _wtask("W-T-001", ["drafter"])
    cheap_underfloor = roster.Agent(
        id="cheap", name="cheap", identity="x", skills=["drafter"],
        cost_class="free-local", tier="producer", capacity_cap=1,
        capability_tags=[],  # lacks the floor cap
    )
    pricey_floored = roster.Agent(
        id="floored", name="floored", identity="x", skills=["drafter"],
        cost_class="premium-cloud", tier="producer", capacity_cap=1,
        capability_tags=["reasoning-heavy"],  # satisfies the floor
    )
    agents = [cheap_underfloor, pricey_floored]

    # With the floor: under-floor agent is filtered out → floored wins.
    sched = dispatch.schedule_wave(agents=agents, wave_tasks=[task],
                                   skill_floor_for=_floor)
    assert sched.assignments == {"W-T-001": "floored"}
    assert sched.gaps == ()

    # Without the floor: cheaper agent wins (shows the floor changed it).
    sched_nofloor = dispatch.schedule_wave([task], agents)
    assert sched_nofloor.assignments == {"W-T-001": "cheap"}
