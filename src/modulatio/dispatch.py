# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Producer dispatch — the tactical roster-selection step.

Given a task and the project's agent roster, pick the best PRODUCER to
execute it. Pure Python — no LLM call here; this is deterministic selection.

Since the skill-library arc (Brick 3) a producer is just a model endpoint —
it holds no skills; it checks out whatever its task needs from the shared
library at run-time. So **skills are not a routing constraint**: any producer
can run any task, and routing always finds one when producers exist.

Selection, top to bottom:

1. **Availability first.** Least-loaded producer wins, so a multi-task wave
   spreads across idle models instead of serializing onto one ("never queue
   onto a busy model when others are idle").
2. **Capability floor — soft** (it *prefers* but does not *require*).
   Producers whose model meets the task's capability floor are preferred;
   but if none do, the best-available
   producer is picked anyway and the shortfall ships as a Product Quality
   Report reservation. Routing NEVER blocks on capability ("always
   best-available + PQR").
3. **Cost, then determinism.** Among equals, cheapest ``cost_class`` (cheap
   producers do the bulk — the QC thesis), then lexicographic ``agent.id``.

``select_agent`` returns ``None`` only for a no-required-skills task (legacy
NO_CONSTRAINT) or an empty producer pool. ``schedule_wave`` adds capacity-aware
backpressure for concurrent waves. A referenced skill that isn't in the
library, or a capability the picked producer lacks, is advisory metadata on a
MATCHED result — never a block.
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


def _producer_pool(agents: list[Agent]) -> list[Agent]:
    """Producers are the only routing targets now — skills are checked out
    at run-time, so any producer can run any task. Leader/QC are excluded by
    tier; they have their own selection paths (``select_qc_agent`` etc.)."""
    return [a for a in agents if a.tier == "producer"]


def _capability_shortfall(agent: Agent, effective_caps: list[str]) -> tuple[str, ...]:
    """Effective capability tags the picked producer does NOT declare — the
    advisory that rides to the Product Quality Report when we route
    best-available below the requested floor (never a block)."""
    held = set(agent.capability_tags)
    return tuple(c for c in effective_caps if c not in held)


def _rank_producers(
    producers: list[Agent], effective_caps: list[str]
) -> tuple[list[Agent], bool]:
    """Return ``(pool, is_fallback)``. The pool is the producers that meet
    the capability floor; if NONE do, it's ALL producers (best-available,
    never block) and ``is_fallback`` is True."""
    meeting = [a for a in producers if a.covers_capabilities(effective_caps)]
    if meeting:
        return meeting, False
    return list(producers), True


def _producer_sort_key(
    agent: Agent, load: dict[str, int], is_fallback: bool
) -> tuple:
    """Availability-first ranking shared by the single-pick and the wave
    scheduler so they can't drift. Least-loaded first (spread a wave across
    idle models, never serialize onto one); then — among producers that meet
    the floor — cheapest wins (cheap producers do the bulk, the QC thesis);
    among the best-available fallback set, highest tier wins (closest to the
    requested floor). Lexicographic id is the deterministic final tiebreak."""
    base = load.get(agent.id, 0)
    if is_fallback:
        return (base, -_TIER_RANK.get(agent.model_tier or "", 0),
                _cost_rank(agent.cost_class), agent.id)
    return (base, _cost_rank(agent.cost_class), agent.id)


def select_agent(
    task: Task,
    agents: list[Agent],
    skill_floor_for: SkillFloorLookup | None = None,
    domain_floor_for: DomainFloorLookup | None = None,
    load: dict[str, int] | None = None,
) -> Agent | None:
    """Pick the best PRODUCER for ``task`` — capability-and-availability
    routing. Returns ``None`` only when the task declared no required_skills
    (legacy NO_CONSTRAINT fallback) or no producer exists at all.

    Since the skill-library arc (Brick 3), skills are NOT a routing
    constraint: a producer checks out whatever its task needs from the
    library, so any producer can run any task. The capability floor is a
    SOFT preference — producers that meet it are preferred, but if none do,
    the best-available producer is picked anyway and the shortfall ships as a
    Product Quality Report reservation (never a block). Availability is
    primary: among the least-loaded producers (``load`` maps agent_id →
    tasks already assigned this pass), pick the best fit, so a multi-task
    wave spreads across idle models instead of serializing onto one.

    A ``preferred_continuity_agent`` hint wins when it's in the chosen pool
    (a code-task chain sticks with the producer that has the prior file in
    its working memory). A hint outside the pool is silently ignored.
    """
    if not task.required_skills:
        return None
    producers = _producer_pool(agents)
    if not producers:
        return None
    load = load or {}
    effective_caps = _effective_required_capabilities(
        task, skill_floor_for, domain_floor_for
    )
    pool, is_fallback = _rank_producers(producers, effective_caps)

    hint = getattr(task, "preferred_continuity_agent", None)
    if hint:
        for a in pool:
            if a.id == hint:
                return a

    return min(pool, key=lambda a: _producer_sort_key(a, load, is_fallback))


def _qualifying_candidates(
    task: Task,
    agents: list[Agent],
    skill_floor_for: SkillFloorLookup | None,
    domain_floor_for: DomainFloorLookup | None,
) -> list[Agent]:
    """The producers eligible to run ``task`` this wave, ranked best-fit
    first by the SAME sort ``select_agent`` uses (so the wave scheduler and
    the single picker never drift). Skills don't gate — every producer is a
    candidate; the capability floor only orders them (with the
    best-available fallback when none meet it). Empty required_skills → no
    skill-routed candidate (legacy NO_CONSTRAINT handled by the caller)."""
    if not task.required_skills:
        return []
    producers = _producer_pool(agents)
    if not producers:
        return []
    effective_caps = _effective_required_capabilities(
        task, skill_floor_for, domain_floor_for
    )
    pool, is_fallback = _rank_producers(producers, effective_caps)
    return sorted(pool, key=lambda a: _producer_sort_key(a, {}, is_fallback))


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

    For each task (deterministic id order): rank the producer pool best-fit
    first (capability floor + best-available fallback — skills don't gate);
    among those with remaining per-agent ``capacity_cap`` AND a free global
    slot, take the best-ranked (continuity hint honored first); decrement
    capacity. Walking the pre-ranked list IS the load-balance — as the best
    pick fills to capacity the next task falls to the next idle producer. A
    producer at cap → DEFERRED_CAPACITY (retry next wave). No producer at all
    → ROSTER_GAP.

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
        # candidates are every producer, already ranked best-fit first
        # (capability floor + best-available fallback). Skills don't gate.
        candidates = _qualifying_candidates(
            task, agents, skill_floor_for, domain_floor_for
        )
        if not candidates:
            gaps.append(task.id)  # no producer exists at all — setup gap
            continue
        if global_remaining is not None and global_remaining <= 0:
            blocking = candidates[0]
            deferred.append((task.id, blocking.id, blocking.capacity_cap))
            continue
        # Honor a continuity hint with free capacity, else take the
        # best-ranked producer that still has a slot this wave. Walking the
        # pre-ranked list IS the load-balance: as the best pick fills to
        # capacity, the next task falls to the next-best idle producer.
        hint = getattr(task, "preferred_continuity_agent", None)
        pick = None
        if hint and hint in by_id and remaining.get(hint, 0) > 0:
            pick = by_id[hint]
        if pick is None:
            for a in candidates:
                if remaining.get(a.id, 0) > 0:
                    pick = a
                    break
        if pick is None:
            # Every qualifying producer is at capacity this wave.
            blocking = candidates[0]
            deferred.append((task.id, blocking.id, blocking.capacity_cap))
            continue
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

    #: A producer was picked for the task. Since the skill-library arc
    #: (Brick 3) this is the routing outcome for essentially every
    #: skill-carrying task: skills are checked out at run-time, so any
    #: producer can run any task and routing always finds one. The result
    #: may carry ADVISORY metadata that never blocks the run —
    #: ``capability_shortfall`` (the picked producer is below the requested
    #: capability floor → ships a Product Quality Report reservation) and
    #: ``missing_skills`` (a referenced skill isn't in the library yet →
    #: feeds the skill-creation proposal).
    MATCHED = "matched"
    #: Task declared no required_skills — orchestrator uses the
    #: hardcoded-role path. Not a gap; not a ticket.
    NO_CONSTRAINT = "no_constraint"
    #: No producer-tier agent exists in the roster at all — a SETUP gap
    #: (the wizard guarantees at least one producer, so this only fires on
    #: a hand-broken roster). Unlike the pre-skill-library era this is no
    #: longer a per-task capability gap: a producer that lacks a skill just
    #: checks it out, and one below the capability floor still runs
    #: best-available. Kept as the one legitimate ticket.
    ROSTER_GAP = "roster_gap"
    #: DEPRECATED — no longer produced. Before the skill library, a skill
    #: name absent from the registry was a hard CRITICAL block; now it is
    #: advisory ``missing_skills`` on a MATCHED result (feeds skill
    #: creation), never a block. Kept for back-compat of any external
    #: consumer of the enum.
    INVALID_SKILL = "invalid_skill"
    #: DEPRECATED — no longer produced. The embedding fallback existed to
    #: rescue skill-name mismatches into a held-skill match; with skills no
    #: longer gating routing, every producer is already a candidate, so the
    #: semantic layer is never reached. Kept for back-compat.
    SEMANTIC_MATCHED = "semantic_matched"


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
    #: On MATCHED: skills the task referenced that aren't in the library
    #: yet — advisory, feeds the skill-creation proposal (Brick 4), never a
    #: block. On ROSTER_GAP: unchanged (the actionable subset for the
    #: setup ticket).
    missing_skills: tuple[str, ...] = field(default_factory=tuple)
    #: Capability tags required by the task but not declared by any
    #: agent in the roster (slice #9a). Populated on ROSTER_GAP when
    #: the capability axis is the gap. Capabilities are the HOW
    #: axis (reasoning-heavy, structured-output, shell-access …).
    missing_capabilities: tuple[str, ...] = field(default_factory=tuple)
    #: On MATCHED: effective capability tags the PICKED producer does not
    #: declare — it was routed best-available below the requested floor.
    #: Advisory → a Product Quality Report reservation, never a block
    #: (Brick 3: "always best-available + PQR").
    capability_shortfall: tuple[str, ...] = field(default_factory=tuple)
    similarity_score: float | None = None


def plan_dispatch(
    task: Task,
    agents: list[Agent],
    available_skill_names: list[str],
    semantic_matcher: SemanticMatcher | None = None,
    skill_floor_for: SkillFloorLookup | None = None,
    domain_floor_for: DomainFloorLookup | None = None,
    load: dict[str, int] | None = None,
) -> DispatchResult:
    """Classify a task against the roster, returning the outcome the
    orchestrator should act on.

    Outcomes (since the skill-library arc, Brick 3):

    - **NO_CONSTRAINT** — the task declared no required_skills; the legacy
      hardcoded-role path handles it.
    - **MATCHED** — a producer was picked (the normal outcome). Skills are
      checked out at run-time, so any producer can run any task; routing
      always finds one when producers exist. The result may carry advisory
      ``missing_skills`` (a referenced skill isn't in the library yet → feeds
      skill creation) and ``capability_shortfall`` (the picked producer is
      below the requested floor → ships a Product Quality Report
      reservation). Neither blocks.
    - **ROSTER_GAP** — no producer-tier agent exists at all (a setup gap the
      wizard normally prevents). The one legitimate ticket.

    ``load`` (agent_id → tasks already assigned this pass) spreads a wave's
    assignments across idle producers. ``skill_floor_for`` / ``domain_floor_for``
    contribute the capability floor that orders producers (soft, never a
    gate). ``semantic_matcher`` is accepted for signature back-compat and is
    no longer used — every producer is already a candidate.
    """
    required = list(task.required_skills)
    if not required:
        return DispatchResult(outcome=DispatchOutcome.NO_CONSTRAINT)

    # A skill the plan referenced that isn't in the library yet is NO LONGER
    # a block — it's recorded as advisory ``missing_skills`` so the Leader can
    # propose creating it (Brick 4). The producer runs with what it has.
    registry = set(available_skill_names)
    unknown_skills = tuple(s for s in required if s not in registry)

    picked = select_agent(
        task, agents,
        skill_floor_for=skill_floor_for,
        domain_floor_for=domain_floor_for,
        load=load,
    )
    if picked is None:
        # No producer-tier agent exists at all — a setup gap, not a per-task
        # capability gap. (The wizard guarantees >= 1 producer.)
        effective_caps = _effective_required_capabilities(
            task, skill_floor_for, domain_floor_for
        )
        return DispatchResult(
            outcome=DispatchOutcome.ROSTER_GAP,
            missing_skills=unknown_skills,
            missing_capabilities=tuple(effective_caps),
        )

    # Best-available producer picked. Anything it can't meet is advisory —
    # never a block (Brick 3: "always best-available + PQR").
    effective_caps = _effective_required_capabilities(
        task, skill_floor_for, domain_floor_for
    )
    shortfall = _capability_shortfall(picked, effective_caps)
    return DispatchResult(
        outcome=DispatchOutcome.MATCHED,
        agent=picked,
        missing_skills=unknown_skills,
        capability_shortfall=shortfall,
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
    2. ``tier == "producer"`` — escalation targets a producer, like
       first-pick dispatch. Skills no longer gate (they're checked out at
       run-time), so skill-cover is not a constraint.
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
        and a.tier == "producer"
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
