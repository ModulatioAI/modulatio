"""Skill-based agent dispatch — the tactical roster-selection step.

Given a task's ``required_skills`` (emitted by the task-plan LLM call
in slice #6b) and the project's agent roster, pick the single best
agent to execute the task. Pure Python — no LLM call here; the planner
already did the reasoning when it declared the skills. This is
deterministic selection against the roster.

Returns ``None`` as a safety-net signal so the orchestrator falls back
to hardcoded role routing (the behavior that shipped in slices #1–#6b):

- Empty ``required_skills`` — the planner declared no constraint.
- Empty roster — cold project, no agents composed yet.
- No agent in the roster covers the required skills — capability gap,
  slice #6d opens a ticket for the human.

Ranking, top to bottom:

1. **Tightest fit.** Fewer extra skills wins — a specialist beats a
   swiss-army agent for a task that only needs one of its skills.
   Echoes Modulatio's core thesis: narrow agents ship narrow prompts.
2. **Cheapest cost_class.** free-local < paid-cloud < premium-cloud.
   Mirrors the cron-agent policy (route to the cheapest tier that can
   actually do the work).
3. **Deterministic.** Lexicographic ``agent.id`` so dispatch is
   reproducible across runs.

Not in #6c: model_tier filtering, capability-tag floor, capacity-aware
backpressure. Those are later slices — #6d / #8 / murmuration.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional

from modulatio.roster import Agent
from modulatio.types import Task

#: Signature of the embedding-fallback callable accepted by
#: ``plan_dispatch``. Given a task, returns ``(agent, similarity_score)``
#: or ``None`` on miss. The matcher owns its index lifecycle (build,
#: query) — plan_dispatch just consumes the verdict.
SemanticMatcher = Callable[[Task], Optional[tuple[Agent, float]]]

#: Signature of the skill-level capability-floor lookup accepted by
#: ``plan_dispatch`` / ``select_agent`` in slice #9b. Given a skill
#: name, returns the tuple of capability tags the skill requires of
#: its executing agent — a hard filter that applies on top of the
#: task's own ``required_capabilities``. Unknown skill names return
#: ``()`` so lookups are safe for cold projects or stale registries.
SkillFloorLookup = Callable[[str], tuple[str, ...]]

#: Signature of the domain-level capability-floor lookup (slice #9b
#: follow-on). Given a task's ``artifact_kind``, returns the tuple of
#: capability tags the standards file for that domain requires of any
#: executing agent — a cross-cutting floor that applies regardless of
#: which specific skill runs the task. Unknown kinds return ``()``.
DomainFloorLookup = Callable[[str], tuple[str, ...]]

_COST_RANK: dict[str | None, int] = {
    "free-local": 0,
    "paid-cloud": 1,
    "premium-cloud": 2,
}
# Unknown cost_class ranks last — don't route to an agent we can't
# reason about the cost of.
_UNKNOWN_COST_RANK = 99

# Model-tier ranking for the QC capability-floor rule (slice #6f-F).
# A partial order is fine for our purposes — the rule is "QC rank >=
# producer rank." Unknown tiers pass gracefully (treated as
# uncomparable rather than excluded).
_TIER_RANK: dict[str, int] = {
    "budget": 1,
    "generalist": 2,
    "tactical": 3,
    "tool-using": 4,
    "reasoning-heavy": 5,
    "strategic": 6,
}


def _cost_rank(cost_class: str | None) -> int:
    if cost_class is None:
        return _UNKNOWN_COST_RANK
    return _COST_RANK.get(cost_class, _UNKNOWN_COST_RANK)


def _effective_required_capabilities(
    task: Task,
    skill_floor_for: SkillFloorLookup | None,
    domain_floor_for: DomainFloorLookup | None = None,
) -> list[str]:
    """Union task-level ``required_capabilities`` with each required
    skill's declared capability floor (slice #9b) and the task's
    artifact_kind domain floor from standards (slice #9b follow-on).
    Deduplicated while preserving first-seen order so rankings and
    ticket bodies stay stable across runs.

    Precedence for ordering (purely cosmetic, for stable output):
    1. Task-declared caps (user input takes lead).
    2. Skill floors (one per required_skill).
    3. Domain floor (artifact_kind's standards file).

    Empty floors (or ``None`` lookups) degrade to fewer axes — every
    pre-#9b call site works unchanged.
    """
    effective: list[str] = []
    seen: set[str] = set()
    for cap in task.required_capabilities:
        if cap not in seen:
            effective.append(cap)
            seen.add(cap)
    if skill_floor_for is not None:
        for skill_name in task.required_skills:
            for cap in skill_floor_for(skill_name):
                if cap not in seen:
                    effective.append(cap)
                    seen.add(cap)
    if domain_floor_for is not None and task.artifact_kind:
        for cap in domain_floor_for(task.artifact_kind):
            if cap not in seen:
                effective.append(cap)
                seen.add(cap)
    return effective


def select_agent(
    task: Task,
    agents: list[Agent],
    skill_floor_for: SkillFloorLookup | None = None,
    domain_floor_for: DomainFloorLookup | None = None,
) -> Agent | None:
    """Pick the best agent for ``task`` from ``agents``, or ``None`` to
    signal fallback to the hardcoded role.

    Empty ``task.required_skills`` or an empty agent list → ``None``.
    No covering agent → ``None``. Otherwise returns the lowest-ranking
    (best) candidate by (extra-skills-count, cost-rank, id).

    Slice #9a: candidate set is also filtered by
    ``task.required_capabilities`` — skill cover AND capability cover
    are both required.

    Slice #9b: when ``skill_floor_for`` is provided, the effective
    capability requirement is the union of the task's caps and each
    required skill's declared floor. ``None`` (default) preserves
    pre-#9b behavior for every call site that isn't capability-floor
    aware yet.

    Pre-V2 Slice D: when ``task.preferred_continuity_agent`` is set,
    that agent is picked first IF they qualify (cover skills + caps).
    Lets a code-task chain stick with the same engineer (who has the
    prior file in their working memory) instead of fanning out to a
    fresh engineer with no context. Hinted agent that doesn't qualify
    is silently ignored — falls through to normal selection.
    """
    if not task.required_skills:
        return None
    if not agents:
        return None

    required_set = set(task.required_skills)
    effective_caps = _effective_required_capabilities(
        task, skill_floor_for, domain_floor_for
    )
    candidates = [
        a for a in agents
        if a.covers(task.required_skills)
        and a.covers_capabilities(effective_caps)
    ]
    if not candidates:
        return None

    # Slice D: continuity hint takes precedence over the cheapest-first
    # sort when the hinted agent is among qualifying candidates.
    hint = getattr(task, "preferred_continuity_agent", None)
    if hint:
        for a in candidates:
            if a.id == hint:
                return a

    return min(candidates, key=lambda a: _candidate_sort_key(a, required_set))


def _candidate_sort_key(agent: Agent, required_set: set) -> tuple[int, int, str]:
    """Ranking shared by ``select_agent`` and ``schedule_wave``: tightest
    fit (fewest extra skills) → cheapest cost_class → lexicographic id.
    Factored so the single-task picker and the wave scheduler can never
    drift apart in how they rank qualifying agents."""
    extra = len(set(agent.skills) - required_set)
    return (extra, _cost_rank(agent.cost_class), agent.id)


def _qualifying_candidates(
    task: Task,
    agents: list[Agent],
    skill_floor_for: SkillFloorLookup | None,
    domain_floor_for: DomainFloorLookup | None,
) -> list[Agent]:
    """Agents that cover ``task``'s required skills AND effective
    capabilities — the same filter ``select_agent`` applies, factored for
    reuse by ``schedule_wave``. Empty required_skills → no skill-routed
    candidate (mirrors ``select_agent`` returning None for NO_CONSTRAINT)."""
    if not task.required_skills:
        return []
    effective_caps = _effective_required_capabilities(
        task, skill_floor_for, domain_floor_for
    )
    return [
        a for a in agents
        if a.covers(task.required_skills) and a.covers_capabilities(effective_caps)
    ]


@dataclass(frozen=True)
class WaveSchedule:
    """Result of allocating one ready wave against remaining capacity.

    - ``assignments``: ``task_id -> agent_id`` for tasks scheduled to run
      THIS wave (capacity reserved).
    - ``deferred``: ``(task_id, blocking_agent_id, agent_capacity_cap)`` —
      DEFERRED_CAPACITY tasks, retry next wave. Distinct from a roster gap
      (Lovecraft round-1: keep capacity contention vs capability gap
      separate for later tuning).
    - ``gaps``: ``task_id``s with ROSTER_GAP (no qualifying agent).
    """
    assignments: dict[str, str] = field(default_factory=dict)
    deferred: tuple[tuple[str, str, int], ...] = field(default_factory=tuple)
    gaps: tuple[str, ...] = field(default_factory=tuple)


def schedule_wave(
    wave_tasks: list[Task],
    agents: list[Agent],
    *,
    global_in_flight_cap: int | None = None,
    skill_floor_for: SkillFloorLookup | None = None,
    domain_floor_for: DomainFloorLookup | None = None,
) -> WaveSchedule:
    """Allocate one concurrent wave's tasks to agents with capacity IN
    selection (core rebuild B2, per Nemo's round-1 correction: a
    post-selection batch limiter would assign five ready tasks that all
    fit the cheapest specialist to that ONE agent, then serialize them).

    For each task (deterministic id order): find qualifying candidates
    (skill + capability cover); among those with remaining per-agent
    ``capacity_cap`` AND a free global slot, pick the best by
    ``_candidate_sort_key`` (continuity hint honored first); decrement
    capacity. A qualifying agent that's at cap → DEFERRED_CAPACITY (retry
    next wave). No qualifying agent → ROSTER_GAP.

    Tasks with empty required_skills (NO_CONSTRAINT legacy fallback) are
    NOT skill-scheduled here — the caller routes them via the legacy path.
    They are silently skipped (neither scheduled nor deferred nor gapped).

    Pure function: capacity is tracked locally; ``agents`` is not mutated.
    """
    remaining = {a.id: max(0, a.capacity_cap) for a in agents}
    by_id = {a.id: a for a in agents}
    global_remaining = global_in_flight_cap

    assignments: dict[str, str] = {}
    deferred: list[tuple[str, str, int]] = []
    gaps: list[str] = []

    for task in sorted(wave_tasks, key=lambda t: t.id):
        if not task.required_skills:
            continue  # legacy NO_CONSTRAINT — not skill-scheduled
        candidates = _qualifying_candidates(
            task, agents, skill_floor_for, domain_floor_for
        )
        if not candidates:
            gaps.append(task.id)
            continue
        if global_remaining is not None and global_remaining <= 0:
            blocking = min(candidates, key=lambda a: a.id)
            deferred.append((task.id, blocking.id, blocking.capacity_cap))
            continue
        available = [a for a in candidates if remaining.get(a.id, 0) > 0]
        if not available:
            # Every qualifying agent is at capacity this wave.
            blocking = min(candidates, key=lambda a: a.id)
            deferred.append((task.id, blocking.id, blocking.capacity_cap))
            continue
        required_set = set(task.required_skills)
        hint = getattr(task, "preferred_continuity_agent", None)
        pick = None
        if hint and hint in by_id and remaining.get(hint, 0) > 0 and by_id[hint] in available:
            pick = by_id[hint]
        if pick is None:
            pick = min(available, key=lambda a: _candidate_sort_key(a, required_set))
        assignments[task.id] = pick.id
        remaining[pick.id] -= 1
        if global_remaining is not None:
            global_remaining -= 1

    return WaveSchedule(
        assignments=assignments,
        deferred=tuple(deferred),
        gaps=tuple(gaps),
    )


class DispatchOutcome(str, Enum):
    """How dispatch classifies a task once it has seen the required_skills
    against the skill registry and the project roster. Slice #6d uses
    the distinction to decide whether to open a capability ticket or
    fall back to hardcoded-role routing; slice #6e adds the semantic
    fallback layer.
    """

    #: An agent covers every required skill deterministically —
    #: strict skill-intersection match. Cheapest + most trustworthy.
    MATCHED = "matched"
    #: Task declared no required_skills — orchestrator uses the
    #: hardcoded-role path. Not a gap; not a ticket.
    NO_CONSTRAINT = "no_constraint"
    #: At least one required_skill is not in the skill registry —
    #: treat as an upstream hallucination that needs a dev fix. Ticket
    #: priority CRITICAL. Semantic fallback does NOT rescue this —
    #: papering over a hallucinated skill name ships wrong output.
    INVALID_SKILL = "invalid_skill"
    #: Deterministic miss but the embedding-fallback matcher (slice
    #: #6e) returned a hit above its similarity threshold. Agent has
    #: the right *shape* even if the declared skill names don't line
    #: up exactly. ``DispatchResult.similarity_score`` carries the
    #: match score so the human can audit whether dispatch was too
    #: lenient.
    SEMANTIC_MATCHED = "semantic_matched"
    #: All required_skills are valid, but no single agent in the roster
    #: covers them deterministically AND no semantic fallback hit
    #: either. Legitimate capability gap — BLOCKER ticket asking the
    #: human to install the skill, create an agent, or defer.
    ROSTER_GAP = "roster_gap"


@dataclass(frozen=True)
class DispatchResult:
    """Outcome of classifying a task against the registry + roster.

    - ``agent`` is set on ``MATCHED`` and ``SEMANTIC_MATCHED``.
    - ``missing_skills`` reports the actionable gap for INVALID_SKILL
      (entries not in registry) or ROSTER_GAP (subset of required_skills
      that NO agent holds at all). A scatter-gap (every skill held by
      some agent, no single agent holds all) returns ROSTER_GAP with
      empty ``missing_skills`` — the ticket body disambiguates.
    - ``similarity_score`` is set on SEMANTIC_MATCHED (value in [0, 1])
      and recorded on the state-transition rationale so the human can
      audit whether the threshold is too lenient. ``None`` on all
      other outcomes.
    """

    outcome: DispatchOutcome
    agent: Agent | None = None
    missing_skills: tuple[str, ...] = field(default_factory=tuple)
    #: Capability tags required by the task but not declared by any
    #: agent in the roster (slice #9a). Populated on ROSTER_GAP when
    #: the capability axis is the gap; empty on MATCHED and on the
    #: skill-gap-only flavor of ROSTER_GAP. Capabilities are the HOW
    #: axis (reasoning-heavy, structured-output, shell-access …).
    missing_capabilities: tuple[str, ...] = field(default_factory=tuple)
    similarity_score: float | None = None


def plan_dispatch(
    task: Task,
    agents: list[Agent],
    available_skill_names: list[str],
    semantic_matcher: SemanticMatcher | None = None,
    skill_floor_for: SkillFloorLookup | None = None,
    domain_floor_for: DomainFloorLookup | None = None,
) -> DispatchResult:
    """Classify a task against the registry + roster, returning the
    outcome the orchestrator should act on.

    Precedence: NO_CONSTRAINT > INVALID_SKILL > MATCHED >
    SEMANTIC_MATCHED > ROSTER_GAP.

    - INVALID_SKILL short-circuits before deterministic or semantic
      matching — a hallucinated skill name is an upstream dev problem
      that neither layer should paper over.
    - Deterministic MATCHED short-circuits before semantic — exact
      skill-intersection is both cheaper and more trustworthy.
    - Semantic layer only fires when a ``semantic_matcher`` is passed
      and deterministic missed.

    Slice #9b: ``skill_floor_for`` lets each required skill contribute
    its own capability floor to the effective filter. Union happens
    once, at the top; both deterministic and semantic paths consume it.
    Callers that don't care about floors can omit the arg for
    back-compat.
    """
    required = list(task.required_skills)
    if not required:
        return DispatchResult(outcome=DispatchOutcome.NO_CONSTRAINT)

    registry = set(available_skill_names)
    invalid = tuple(s for s in required if s not in registry)
    if invalid:
        return DispatchResult(
            outcome=DispatchOutcome.INVALID_SKILL,
            missing_skills=invalid,
        )

    picked = select_agent(
        task, agents,
        skill_floor_for=skill_floor_for,
        domain_floor_for=domain_floor_for,
    )
    if picked is not None:
        return DispatchResult(outcome=DispatchOutcome.MATCHED, agent=picked)

    effective_caps = _effective_required_capabilities(
        task, skill_floor_for, domain_floor_for
    )
    if semantic_matcher is not None:
        hit = semantic_matcher(task)
        if hit is not None:
            match_agent, score = hit
            # Slice #9a/#9b: capabilities — task-declared and
            # skill-floor — are a hard filter. A semantic hit that
            # lacks any effective capability is not rescued; fall
            # through to ROSTER_GAP so the ticket surfaces the gap.
            if match_agent.covers_capabilities(effective_caps):
                return DispatchResult(
                    outcome=DispatchOutcome.SEMANTIC_MATCHED,
                    agent=match_agent,
                    similarity_score=float(score),
                )

    # ROSTER_GAP — actionable subsets for each axis so the ticket body
    # can surface exactly what the human needs to install or add.
    held_by_anyone: set[str] = set()
    capabilities_held_by_anyone: set[str] = set()
    for a in agents:
        held_by_anyone.update(a.skills)
        capabilities_held_by_anyone.update(a.capability_tags)
    missing_from_roster = tuple(s for s in required if s not in held_by_anyone)
    missing_caps_from_roster = tuple(
        c for c in effective_caps if c not in capabilities_held_by_anyone
    )
    return DispatchResult(
        outcome=DispatchOutcome.ROSTER_GAP,
        missing_skills=missing_from_roster,
        missing_capabilities=missing_caps_from_roster,
    )


def _meets_capability_floor(
    qc_model_tier: str | None,
    producer_model_tier: str | None,
) -> bool:
    """QC's model_tier must meet or exceed the producer's — "a weak
    judge cannot evaluate strong output" (quality-architecture.md).

    Unknown or uncomparable tiers pass gracefully so a misconfigured
    roster doesn't brick QC dispatch. Only *strictly below* fails.
    """
    if qc_model_tier is None or producer_model_tier is None:
        return True
    qc_rank = _TIER_RANK.get(qc_model_tier)
    producer_rank = _TIER_RANK.get(producer_model_tier)
    if qc_rank is None or producer_rank is None:
        return True
    return qc_rank >= producer_rank


def select_escalation_agent(
    task: Task,
    current_agent_id: str | None,
    current_model_tier: str | None,
    agents: list[Agent],
    skill_floor_for: SkillFloorLookup | None = None,
    domain_floor_for: DomainFloorLookup | None = None,
) -> Agent | None:
    """Pick a strictly-higher-tier escalation agent for a producer that
    exhausted its retry budget on QC rejection, or ``None`` when no
    qualifying escalation candidate exists.

    Filter (all required):
    1. ``Agent.id != current_agent_id`` — escalation is a different
       mind. Same-agent retry is the orchestrator's job to handle when
       this helper returns ``None``; it is never a valid escalation
       target.
    2. Covers ``task.required_skills`` — same skill-cover constraint
       as first-pick dispatch.
    3. Covers the effective required capabilities — union of
       ``task.required_capabilities`` and each required skill's floor
       (slice #9b). Escalation is NOT a relaxation of the capability
       gate.
    4. ``model_tier`` strictly higher than ``current_model_tier`` per
       ``_TIER_RANK``. When either tier is unknown or uncomparable,
       "strictly higher" is undefined and no escalation is picked —
       consistent with #6f-F graceful degradation.

    Ranking among qualifying candidates: cheapest ``cost_class`` first,
    then lexicographic ``id``. Deterministic and reproducible.

    Orchestrator contract: a ``None`` return means "no valid escalation
    in the roster." The caller may then choose to retry once more with
    the current agent as a last-ditch attempt (slice #9c orchestration
    policy) — that behavior is the orchestrator's, not this helper's.
    """
    current_rank = _TIER_RANK.get(current_model_tier) if current_model_tier else None
    if current_rank is None:
        return None

    effective_caps = _effective_required_capabilities(
        task, skill_floor_for, domain_floor_for
    )

    def _strictly_higher(candidate: Agent) -> bool:
        c_rank = _TIER_RANK.get(candidate.model_tier) if candidate.model_tier else None
        if c_rank is None:
            return False
        return c_rank > current_rank

    candidates = [
        a for a in agents
        if a.id != current_agent_id
        and a.covers(task.required_skills)
        and a.covers_capabilities(effective_caps)
        and _strictly_higher(a)
    ]
    if not candidates:
        return None

    def sort_key(a: Agent) -> tuple[int, str]:
        return (_cost_rank(a.cost_class), a.id)

    return min(candidates, key=sort_key)


def select_qc_agent(
    producer_agent_id: str | None,
    producer_model_tier: str | None,
    qc_candidates: list[Agent],
) -> Agent | None:
    """Pick the QC agent for a task, or ``None`` for role-keyed fallback.

    Slice #6f-F structural rules (enforced mechanically, not by
    convention):

    1. **Tier filter.** Must have ``Agent.tier == "qc"``. A producer
       holding the 'qc' skill is not a QC candidate — QC is a role
       type, not just a skill match.
    2. **Different mind.** Must NOT be the same agent that produced
       the artifact for this task. Same-agent self-QC collapses into
       rubber-stamping — explicitly banned in the architecture doc.
    3. **Capability floor.** QC's ``model_tier`` must meet or exceed
       the producer's. A budget-tier QC against a reasoning-heavy
       producer is filtered out.

    Among qualifying candidates: cheapest cost_class wins, with
    lexicographic id as deterministic tiebreak.
    """
    filtered = [
        a for a in qc_candidates
        if a.tier == "qc"
        and a.id != producer_agent_id
        and _meets_capability_floor(a.model_tier, producer_model_tier)
    ]
    if not filtered:
        return None

    def sort_key(a: Agent) -> tuple[int, str]:
        return (_cost_rank(a.cost_class), a.id)

    return min(filtered, key=sort_key)


#: Artifact kinds whose product is itself a quality judgment over other
#: artifacts (cross-doc audit, synthesis, review reports). When the
#: producer is the only qc-tier agent on the team — typical of small
#: rosters — the standard "QC must be a different agent" filter returns
#: no peer, and the role-keyed fallback silently lets the producer
#: self-verify its own audit. Orchestrator routes verification to
#: Leader instead for these kinds; see issue #5/#6 from the 2026-04-30
#: smoke test (memory: project_modulatio_qc_as_producer_gap.md).
AUDIT_CLASS_ARTIFACT_KINDS: frozenset[str] = frozenset({
    "audit",
    "audit-report",
    "synthesis",
    "synthesis-report",
    "review",
    "review-report",
    "consistency-audit",
    "cross-artifact-audit",
})


def is_audit_class_artifact_kind(artifact_kind: str | None) -> bool:
    """True iff ``artifact_kind`` names a verification/synthesis product
    whose verification should peer-route to Leader on small teams.

    Case-insensitive, whitespace-stripped. ``None`` / empty / unknown
    kinds → False (default routing applies).
    """
    if not artifact_kind:
        return False
    return artifact_kind.strip().lower() in AUDIT_CLASS_ARTIFACT_KINDS


__all__ = [
    "AUDIT_CLASS_ARTIFACT_KINDS",
    "DispatchOutcome",
    "DispatchResult",
    "DomainFloorLookup",
    "SemanticMatcher",
    "SkillFloorLookup",
    "is_audit_class_artifact_kind",
    "plan_dispatch",
    "schedule_wave",
    "select_agent",
    "select_escalation_agent",
    "select_qc_agent",
    "WaveSchedule",
]
