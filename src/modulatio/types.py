# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Core contract for Modulatio 2.0.

Shared between backend, TUI, and future web surfaces. Matches the
project's design notes — see architecture.md, gaps/02-evidence.md, and
cross-cutting.md in the design vault for context.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import Enum
from typing import Annotated, Any, Literal, Union
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, field_validator


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _env_int(name: str, default: int) -> int:
    """Guarded env-int for construction-time model defaults (the SETTINGS
    tab's retry knobs): unset/bad/negative → ``default``, never raises.
    Evaluated per construction via ``default_factory`` so a saved override
    applies to every NEW task/goal (stored JSON keeps its explicit value)."""
    import os
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value >= 0 else default


# ─── Enums ──────────────────────────────────────────────────────────────────

class GoalStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    ABANDONED = "abandoned"


class TaskStatus(str, Enum):
    PENDING = "pending"
    DISPATCHED = "dispatched"
    IN_PROGRESS = "in_progress"
    AWAITING_QC = "awaiting_qc"
    QC_REJECTED = "qc_rejected"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    ABANDONED = "abandoned"


class TicketPriority(str, Enum):
    BLOCKER = "blocker"
    CRITICAL = "critical"
    MINOR = "minor"


class TicketStatus(str, Enum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    RESOLVED = "resolved"
    CLOSED = "closed"


class EvidenceClass(str, Enum):
    OBJECTIVE = "objective"
    SUBJECTIVE = "subjective"


class ProjectState(str, Enum):
    DRAFT = "draft"
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    ABANDONED = "abandoned"


# ─── Evidence ───────────────────────────────────────────────────────────────

class _EvidenceBase(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    created_at: datetime = Field(default_factory=_utcnow)
    producer: str  # agent id or "tool:<name>"
    primary: bool  # true = deterministic source, false = synthesized


class ArtifactEvidence(_EvidenceBase):
    kind: Literal["artifact"] = "artifact"
    location: str  # file path, URL, or memory ID
    checksum: str | None = None


class MetricEvidence(_EvidenceBase):
    kind: Literal["metric"] = "metric"
    name: str
    value: float
    target: str  # e.g. "post_count >= 3"
    source: str  # where the metric came from


class AssertionEvidence(_EvidenceBase):
    kind: Literal["assertion"] = "assertion"
    check: str  # human-readable statement of what's being asserted
    command: str | None = None
    expected: str | None = None
    passed: bool


class ReportEvidence(_EvidenceBase):
    kind: Literal["report"] = "report"
    summary: str
    cites: list[UUID] = Field(default_factory=list)  # evidence IDs this report cites


Evidence = Annotated[
    Union[ArtifactEvidence, MetricEvidence, AssertionEvidence, ReportEvidence],
    Field(discriminator="kind"),
]


class EvidenceRequirement(BaseModel):
    """What must be provided before a goal/task can transition to completed."""
    kind: Literal["artifact", "metric", "assertion", "report"]
    description: str
    target: str | None = None  # e.g. "post_count >= 3", "exit code 0"
    source: str | None = None


# ─── Goals and tasks ────────────────────────────────────────────────────────

class StateTransition(BaseModel):
    """Append-only audit record for goal/task/ticket state changes."""
    from_state: str
    to_state: str
    at: datetime = Field(default_factory=_utcnow)
    actor: str  # agent id or "human"
    evidence_ids: list[UUID] = Field(default_factory=list)
    verifier_result: str | None = None
    leader_decision: str | None = None
    rationale: str


class Goal(BaseModel):
    id: str  # e.g. "STA-G-001"
    project_id: UUID
    description: str
    success_criteria: str
    evidence_class: EvidenceClass = EvidenceClass.OBJECTIVE
    evidence_required: list[EvidenceRequirement] = Field(default_factory=list)
    evidence_provided: list[UUID] = Field(default_factory=list)
    status: GoalStatus = GoalStatus.PENDING
    transitions: list[StateTransition] = Field(default_factory=list)
    # Slice #7e: daily retry budget for Leader-led auto-redo on
    # disappointed verdicts. ``retry_count`` is consumed attempts in
    # the current window; ``retry_count_date`` is the window identity
    # (UTC date). When a new day rolls over, ``retry_count`` resets
    # automatically on the first redo check — no manual reset needed.
    # Exhaustion → BLOCKER ticket with refresh_at=tomorrow-midnight-UTC;
    # auto-resume picks it up on next kickoff past the refresh time.
    retry_count: int = 0
    #: ABSOLUTE per-run redo budget. The team gets up to this
    #: many Leader-led reflect-and-redo attempts to get a goal right within a
    #: single run; on exhaustion the goal SHIPS with a Product Quality Report
    #: reservation (never an infinite loop). retry_count only climbs within a
    #: run — it is NOT reset mid-run by a date roll; the daily refresh applies
    #: only to RESUMING a parked goal in a later run (the Alfred loop).
    max_retries: int = Field(
        default_factory=lambda: _env_int("MODULATIO_GOAL_MAX_RETRIES", 4))
    retry_count_date: date | None = None
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class Task(BaseModel):
    id: str  # e.g. "STA-T-001"
    project_id: UUID
    goal_id: str
    description: str
    # DEPRECATED (Brick 1 / D2): the pre-keystone role-pre-assignment axis.
    # Routing is now purely capability + availability (required_skills /
    # required_capabilities → dispatch → the dispatched agent's own model);
    # nothing sets or reads this anymore. Kept solely so 0.5.0-era task JSON
    # on disk still parses; excluded from emission so new tasks never carry
    # it. Full removal lands with the role-language rename brick.
    assignee_specialist: str | None = Field(default=None, exclude=True)
    # The artifact class this task produces — picks the standards domain
    # (e.g. application, code, marketing, research, wordpress) that producer
    # and QC both consume. Modulatio is output-agnostic; the domain is data.
    # Default is the neutral placeholder "text" so a task without an
    # explicit kind does not silently route to any one domain's standards.
    artifact_kind: str = "text"
    # The OPERATION this task performs (build / fix / review / research / measure /
    # …) — orthogonal to ``artifact_kind`` (the output): you can debug code OR data.
    # It selects the verification bar the operation demands (see ``operation_bars``).
    # Default empty → no operation-specific bar (today's behavior). A task property,
    # never a role.
    operation: str = ""
    # External knowledge the producer may need. Each topic is checked
    # against the research cache before delegating to the Researcher; fresh
    # results are cached to `<project>/research/<slug>.md` for reuse.
    research_topics: list[str] = Field(default_factory=list)
    # Skills this task requires — emitted by the task-plan step grounded
    # in the live skill registry (slice #6b). Slice #6c dispatch matches
    # these against `Agent.skills` to pick the narrowest qualifying
    # agent and records the result in `assigned_agent_id` below.
    required_skills: list[str] = Field(default_factory=list)
    # Capability tags this task requires — the fine-grained HOW axis
    # layered on top of skill cover (slice #9a). Skills name WHAT the
    # agent does; capabilities name HOW (e.g. reasoning-heavy,
    # structured-output, long-context, shell-access). Hard dispatch
    # filter: an agent must cover required_skills AND
    # required_capabilities. The task-planning step grounds its picks in
    # the union of the roster's declared capability_tags (no separate registry).
    required_capabilities: list[str] = Field(default_factory=list)
    # Agent picked by dispatch (slice #6c). None signals "no match — use
    # the hardcoded role safety net": either required_skills was empty,
    # the roster was empty, or no agent covered the requirement. Written
    # once by the orchestrator right after the task-planning step.
    assigned_agent_id: str | None = None
    # QC agent picked by tier-constrained dispatch (slice #6f-F).
    # Rules: tier=='qc', different agent than producer, model_tier
    # meets capability floor. None signals "no qc-tier candidate
    # qualified — use the role-keyed 'qc' runner" (CLI's --qc-model
    # flag still provides the different-mind guarantee).
    qc_agent_id: str | None = None
    # Task IDs whose completion is a prerequisite (slice #7a).
    # Orchestrator topologically sorts tasks before execution;
    # successors whose predecessors land in terminal-fail state
    # (BLOCKED, QC_REJECTED, ABANDONED) are themselves BLOCKED
    # without running their producer. Empty list = no deps, dispatches
    # whenever reached.
    depends_on: list[str] = Field(default_factory=list)
    # Pre-V2 stopgap (Slice D): when set, dispatch prefers this agent id
    # if they qualify (cover skills + caps) and have capacity. Falls
    # through to normal selection otherwise. Orchestrator derives the
    # hint from ``depends_on`` — same producer keeps a code-task chain
    # together so engineer who wrote engine.py also writes combat.py
    # rather than parallel handoff to a different engineer with no
    # working memory of the prior file. ``None`` = no hint, normal
    # cheapest-qualifying selection applies.
    preferred_continuity_agent: str | None = None
    # Relative file path for the produced artifact (slice #7b).
    # Resolved under ``<project>/artifacts/<path>``. ``None`` keeps the
    # pre-#7b default of ``drafts/<task_id>.md`` — back-compat for
    # every existing single-artifact task. Set by the planner when
    # it wants a specific path, or by the expansion of an
    # ``artifacts: [...]`` list (one sub-task per declared path).
    output_path: str | None = None
    #: Finished-product flag (2026-05-29). When True, this task's artifact is
    #: a deliverable the user should receive — the Leader tags it at plan
    #: time. The run-finalization stage renders every deliverable's Markdown
    #: to a real document (DOCX default), names it from the document's own
    #: title, and copies it to ``~/Documents/Modulatio/<project>/``. Default
    #: False: intermediate scaffolding (research notes, data) stays in the
    #: run's ``artifacts/`` only. See :mod:`modulatio.delivery`.
    deliverable: bool = False
    # GENERATE draws a fresh artifact from scratch; EDIT applies surgical
    # patches to a prior draft. The planner flips this on the next retry
    # when QC classifies the defect as mechanical (fix-by-editing).
    # Slice #82 PR-C adds DIFF: a multi-file producer mode where one
    # producer call emits ``=== FILE: <path> ===`` blocks for each file
    # it changes. The planner emits ``producer_mode: "diff"`` directly
    # for tasks known to span multiple files; the retry router can also
    # promote a multi-file mechanical defect to diff on retry.
    # §3b adds REVISE: a SUBSTANTIVE-defect redo that builds on the existing
    # draft + the reviewer's critique (never from scratch) — the retry router
    # picks it for non-mechanical defects so neither QC nor the Leader throws
    # the prior work away.
    producer_mode: Literal["generate", "edit", "diff", "revise"] = "generate"
    # Structured arguments for tool-executor skills (slice #9e). Ignored
    # by LLM-executor skills. The planner emits a free-form dict per
    # task alongside required_skills; orchestrator passes it through
    # to the tool callable as kwargs.
    tool_args: dict[str, Any] = Field(default_factory=dict)
    evidence_required: list[EvidenceRequirement] = Field(default_factory=list)
    evidence_provided: list[UUID] = Field(default_factory=list)
    #: Content-addressed QC pass-mark (Part A / review-ledger, task #85/#86). Set
    #: to the artifact's ``sha256:`` checksum the moment QC PASSES this task; the
    #: ArtifactEvidence objects that also carry the checksum are transient (only
    #: their IDs survive on ``evidence_provided``), so this is the durable,
    #: queryable "reviewed @ content" mark. Consumers: an ASSEMBLY task derives
    #: its authoritative expected-unit set from its ``depends_on`` tasks' marks
    #: (path + this checksum), and the no-regress guard checkpoints the version
    #: that earned this mark. ``None`` until the task first passes QC.
    qc_passed_checksum: str | None = None
    status: TaskStatus = TaskStatus.PENDING
    transitions: list[StateTransition] = Field(default_factory=list)
    retry_count: int = 0
    max_retries: int = Field(
        default_factory=lambda: _env_int("MODULATIO_TASK_MAX_RETRIES", 3))
    #: #18 keystone: LIFETIME producer-attempt counter — monotonic, incremented on
    #: every ``_producer_execute`` regardless of which model holds the task or which
    #: path re-entered the redo loop (goal-redo, declined-ticket re-dispatch,
    #: escalation reassignment). Unlike ``retry_count`` (a per-pass display index that
    #: resets on re-entry), this NEVER resets — it ties the producer budget to the
    #: TASK so a model can't skirt the QC-as-fixer floor by earning a fresh budget on
    #: re-entry. ``_run_task_with_redo`` bounds its remaining attempts by
    #: ``(max_retries + 1) - lifetime_attempts``; when spent, QC-as-fixer is forced.
    lifetime_attempts: int = 0
    #: Recursion guard for overflow→decompose (2026-05-30). A task that
    #: overflowed its context budget is split into smaller children, each
    #: ``decompose_depth = parent + 1``. Past a small cap the engine stops
    #: re-splitting and escalates (genuine stuck, not confusion). 0 = an
    #: originally-planned task.
    decompose_depth: int = 0
    # QC-as-fixer Slice 3: True when QC AUTHORED a fix for this task's
    # artifact after the producer exhausted its attempts (or stormed).
    # LOAD-BEARING flag, not cosmetic: a qc-authored artifact has NO
    # independent producer/QC separation — QC both wrote and (cannot
    # truly) verify it. This is CONTROLLED DEGRADATION, surfaced as such
    # everywhere (metrics, reports). Never counted as a clean producer
    # win. See verifier_result="qc_authored_fix" on the completion
    # transition + the mandatory third-agent sanity pass / approval gate.
    qc_authored_fix: bool = False
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)
    # Slice 1 (#88): producer self-claim, 1-2 sentences captured at
    # the end of producer responses. Read by Leader-reflect's Verify
    # phase (PR-B) to render the next ``current_state.md`` doc and
    # compare against QC verdicts for divergence flagging. ``None`` =
    # producer didn't include the field (missing-is-non-fatal). QC
    # explicitly does NOT see this — the artifact is the ground truth
    # for quality evaluation; the self-claim is for state-doc only.
    summary_for_state_doc: str | None = None


# ─── Tickets ────────────────────────────────────────────────────────────────

class Ticket(BaseModel):
    id: str  # e.g. "STA-1"
    project_id: UUID
    priority: TicketPriority
    status: TicketStatus = TicketStatus.OPEN
    title: str
    body: str
    affected_goal_id: str | None = None
    affected_task_id: str | None = None
    #: Provenance: the run that opened this ticket. Tickets are PROJECT-durable
    #: (they accumulate + persist across runs), but recording the originating
    #: run lets the F8 kill-teardown clear only THIS run's residue — and the
    #: TUI show which run raised it. ``None`` for project-level / legacy tickets.
    run_id: str | None = None
    #: Phase 3.1b-ii: ticket references a Leader-produced plan
    #: (``<project>/plans/<id>.md``). When the ticket is approved /
    #: denied via :func:`store.update_ticket_approval`, the linked plan's
    #: ``status`` field flips accordingly so the dispatcher (3.1b-iv)
    #: can read either side of the link.
    affected_plan_id: str | None = None
    #: Auto-resume trigger (slice #7e). BLOCKER tickets opened on
    #: budget exhaustion (retry budget now; cost budget with #9's
    #: Comptroller later) carry the timestamp at which the underlying
    #: budget refreshes. On subsequent kickoff past this time, the
    #: orchestrator auto-resumes the affected goal and transitions
    #: the ticket to RESOLVED. ``None`` = human-only resolution (the
    #: default for capability / invalid-skill / on-the-fence tickets).
    refresh_at: datetime | None = None
    #: Slice #16: approval request flag. ``True`` means this ticket is
    #: asking a human for a binary decision (continue past 80% budget?
    #: accept output with caveats? escalate to premium model?). Most
    #: tickets are plain notifications and stay ``False``.
    approval_required: bool = False
    #: The decision recorded against an approval request. ``None`` when
    #: approval_required=False or when still pending.
    approval_decision: Literal["approved", "denied"] | None = None
    #: Who recorded the decision — "user" in v1.2; reserved for future
    #: role-based approvals (e.g. "leader", "comptroller").
    approval_decided_by: str | None = None
    #: When the decision was recorded. ``None`` while pending.
    approval_decided_at: datetime | None = None
    #: Optional free-text rationale captured with the decision. Surfaces
    #: in the team-visible audit trail so future Leader runs and humans
    #: can see *why*, not just the verdict.
    approval_note: str | None = None
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)
    transitions: list[StateTransition] = Field(default_factory=list)


# ─── Project ────────────────────────────────────────────────────────────────

#: Tuples of unknown context_budget role keys we've already warned about
#: this process. Validator drops repeats to DEBUG to keep TUI/status
#: polling from spamming the log.
#:
#: Bounded to avoid an unbounded-growth leak across the process lifetime:
#: keys come from operator-supplied ``Project.context_budgets`` and a
#: buggy/adversarial caller could feed an endless stream of *distinct*
#: unknown role tuples, each one a new entry that never expires. We cap
#: the cache at ``_SEEN_UNKNOWN_BUDGET_ROLES_MAX`` and flush it wholesale
#: on overflow (kept as a plain set so ``.add`` / ``.discard`` / ``.clear``
#: / membership all stay valid for callers and tests). The only
#: observable effect of a flush is that a previously-seen tuple may emit
#: one fresh WARN if it recurs afterwards — acceptable for what is purely
#: a log-noise dampener, and the cap is far above the handful of distinct
#: role tuples a sane config ever produces.
_SEEN_UNKNOWN_BUDGET_ROLES_MAX = 1024
_SEEN_UNKNOWN_BUDGET_ROLES: set[tuple[str, ...]] = set()
#: Guards the check-then-act on ``_SEEN_UNKNOWN_BUDGET_ROLES``. Project
#: validation runs on concurrent wave-executor threads (waves are ON by
#: default), so an unsynchronized "in?-then-add" can let two threads both
#: miss and both emit the first-sighting WARN. The lock makes the
#: first-sighting/repeat decision deterministic — exactly one WARN per
#: unknown tuple — without changing what gets logged.
_SEEN_UNKNOWN_BUDGET_ROLES_LOCK = threading.Lock()


class Project(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    code: str  # 3-letter unique prefix, e.g. "STA"
    name: str
    objective: str  # one-line top-level goal
    state: ProjectState = ProjectState.DRAFT
    #: Per-agent model assignments. Keyed by agent_id (e.g. "leader",
    #: "engineer", "writer", or any custom-roster id). Preset key from
    #: model_presets.json or raw LiteLLM "provider/model" id.
    #:
    #: Phase 2.2 introduces this map as the new home for project-level
    #: model wiring. The legacy ``leader_model`` field below remains as
    #: a back-compat fallback for the multimodal-Leader path until every
    #: caller (CLI / daemon / TUI / wizard) populates ``agent_models``
    #: directly. Empty by default — orchestrator falls through to the
    #: legacy field.
    agent_models: dict[str, str] = Field(default_factory=dict)
    #: Legacy field — Phase 2.2 transition. Only consumed today by
    #: ``_run_multimodal_leader`` (vision-in-kickoff). New callers
    #: should populate ``agent_models["leader"]`` instead. ``None``
    #: = unset; multimodal path raises a clear error if neither field
    #: provides a model. Made optional so first-class peer-team
    #: projects can omit hierarchy entirely.
    leader_model: str | None = None
    wiki_path: str  # path to <vault_root>/<project-slug>/ — see vault.project_dir()
    #: Per-kickoff run identifier. ``None`` for legacy callers (tests
    #: + pre-run-isolation paths) — store/orchestration fall back to
    #: project-root paths in that case. Production CLI / TUI / daemon
    #: kickoff paths generate via ``vault.generate_run_id()`` and call
    #: ``vault.init_run()`` to create the per-kickoff workspace.
    run_id: str | None = None
    #: Per-role context-budget overrides keyed by ``budget_role``
    #: (e.g. ``"producer"``, ``"leader-reflect"``); value is the
    #: max_input_tokens cap to use instead of the experimental default
    #: in ``context_budget.EXPERIMENTAL_DEFAULTS``. Empty → inherit
    #: defaults. ``resolve_for_dispatch`` consumes this map at the
    #: project rank, between agent-level overrides and the default
    #: table. Unknown roles preserve across round-trip but log a soft
    #: warning at validation.
    context_budgets: dict[str, int] = Field(default_factory=dict)
    #: Per-recipient inbox caps for the sparse-inbox
    #: channel. Outer dict is keyed by ``target_runner_role`` (drafter
    #: / qc / leader / engineer / etc.); inner dict carries
    #: ``"soft_cap"`` and/or ``"hard_cap"`` integers. Missing keys
    #: inherit from :data:`inboxes.INBOX_DEFAULTS`. Bound-checked at
    #: construction: ``1 ≤ soft_cap ≤ hard_cap ≤ HARD_INBOX_CEILING``.
    inbox_caps: dict[str, dict[str, int]] = Field(default_factory=dict)
    #: Per-priority decay-window overrides for the prior
    #: sparse-inbox channel. Keyed by ``"P0"`` / ``"P1"`` / ``"P2"``;
    #: value is the turn count past which a note expires. Bound-
    #: checked at construction: ``1 ≤ value ≤ 32``.
    inbox_decay_overrides: dict[str, int] = Field(default_factory=dict)
    #: goal-state compression toggle. ``True`` (default)
    #: routes Leader-reflect's state-doc emit through
    #: ``compression.emit_compaction`` — the structured JSON +
    #: versioned tree + manifest + audit surface. ``False`` reverts to
    #: the free-markdown state-doc behavior — both the
    #: Leader-reflect prompt contract AND the engine write-back path
    #: flip together (without both flipping, arm B in an A/B harness
    #: experiment isn't actually behavior, it's with
    #: the writer hobbled). Each successfully parsed Leader-reflect
    #: turn under ``False`` additionally emits a
    #: ``compaction_skipped`` audit row with
    #: ``skip_reason="disabled_by_config"`` so the compression-off
    #: provenance is visible in the run's audit trail. Parse-failed
    #: turns return on the pause path before the disabled-by-config
    #: emission and do NOT add an audit row.
    compression_enabled: bool = True
    #: #151 conditional compression. When ``compression_enabled`` is True,
    #: this gates WHETHER a given Leader-reflect turn actually compacts.
    #: Pressure = (accumulated state-doc tokens) / (model's effective input
    #: cap). Compaction runs only when pressure >= this threshold —
    #: compression is net-negative on short workloads (the break-even
    #: finding), so engaging it early is pure overhead. Range [0.0, 1.0];
    #: default ``0.0`` = compact every parseable reflect turn (the prior
    #: unconditional behavior, backward-compatible). Operators/eval raise it
    #: (e.g. 0.5) to make compaction long-horizon-only. Below threshold →
    #: a ``compaction_skipped(skip_reason="below_pressure_threshold")`` row.
    compression_pressure_threshold: float = 0.0
    #: QC-as-fixer Slice 2 / #151: per-role per-dispatch OUTPUT-token soft
    #: cap override for the circuit breaker (``dispatch_breaker``). Keyed by
    #: role (``"producer"`` / ``"qc"`` / etc.); value is the soft cap in
    #: tokens. Empty → inherit ``dispatch_breaker`` defaults (~30K producer).
    #: DISTINCT from ``context_budgets`` (which bounds INPUT context). The
    #: breaker's hard backstop derives from the soft cap, so this is the one
    #: knob an operator turns. Bound-checked at construction.
    output_budgets: dict[str, int] = Field(default_factory=dict)
    #: §5 (2026-06-03): the concurrent wave executor is ON by default —
    #: parallelism is the point of a swarm. This field is the per-project
    #: switch the A/B harness still varies as a dimension: when the
    #: ``MODULATIO_CONCURRENT_WAVES`` env var is unset, this field decides
    #: (True → concurrent, False → sequential), so a harness arm can force a
    #: sequential run by setting it False. The env var overrides the field in
    #: both directions (``=0`` is the absolute kill-switch, ``=1`` forces on).
    #: See ``Orchestrator._concurrent_waves_enabled``.
    concurrent_waves_enabled: bool = True
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)

    @field_validator("code")
    @classmethod
    def _validate_code(cls, v: str) -> str:
        # Defense in depth: vault.project_dir validates on every path
        # construction, but a Project built without ever calling vault
        # (smoke scripts, tests, direct API use) would otherwise carry
        # an unsanitized code into _scope_root and audit_path math.
        from modulatio.vault import validate_project_code
        validate_project_code(v.lower())
        return v

    @field_validator("compression_pressure_threshold")
    @classmethod
    def _validate_compression_pressure_threshold(cls, v: float) -> float:
        if not (0.0 <= v <= 1.0):
            raise ValueError(
                f"compression_pressure_threshold must be in [0.0, 1.0], got {v}"
            )
        return v

    @field_validator("output_budgets")
    @classmethod
    def _validate_output_budgets(cls, v: dict[str, int]) -> dict[str, int]:
        if not v:
            return v
        from modulatio.dispatch_breaker import (
            OUTPUT_BUDGET_CEILING,
            OUTPUT_BUDGET_MIN,
        )
        for role, tokens in v.items():
            if tokens < OUTPUT_BUDGET_MIN:
                raise ValueError(
                    f"Project.output_budgets[{role!r}]={tokens} below "
                    f"minimum {OUTPUT_BUDGET_MIN}"
                )
            if tokens > OUTPUT_BUDGET_CEILING:
                raise ValueError(
                    f"Project.output_budgets[{role!r}]={tokens} exceeds "
                    f"ceiling {OUTPUT_BUDGET_CEILING}"
                )
        return v

    @field_validator("context_budgets")
    @classmethod
    def _validate_context_budgets(cls, v: dict[str, int]) -> dict[str, int]:
        if not v:
            return v
        # Local import avoids circular load: context_budget doesn't
        # import types, but pydantic resolves validators eagerly.
        from modulatio.context_budget import (
            CTX_BUDGET_MIN_TOKENS,
            EXPERIMENTAL_DEFAULTS,
            HARD_GLOBAL_CEILING,
        )
        import logging

        for role, tokens in v.items():
            if tokens < CTX_BUDGET_MIN_TOKENS:
                raise ValueError(
                    f"Project.context_budgets[{role!r}]={tokens} below "
                    f"minimum {CTX_BUDGET_MIN_TOKENS}"
                )
            if tokens > HARD_GLOBAL_CEILING:
                raise ValueError(
                    f"Project.context_budgets[{role!r}]={tokens} exceeds "
                    f"hard ceiling {HARD_GLOBAL_CEILING}"
                )
        known = set(EXPERIMENTAL_DEFAULTS.keys())
        unknown = tuple(sorted(k for k in v.keys() if k not in known))
        if unknown:
            logger = logging.getLogger("modulatio.context_budget")
            # First sighting of this exact unknown-tuple gets WARN; later
            # round-trips (TUI render, daemon poll, status command) drop
            # to DEBUG so the noise floor stays usable. The check-then-act
            # is locked: concurrent wave threads must not both treat the
            # same tuple as a first sighting (double-WARN).
            with _SEEN_UNKNOWN_BUDGET_ROLES_LOCK:
                first_sighting = unknown not in _SEEN_UNKNOWN_BUDGET_ROLES
                if first_sighting:
                    # Flush wholesale past the cap so an endless stream of
                    # distinct unknown tuples can't grow the cache without
                    # bound. Flushing before the add keeps the just-seen
                    # tuple recorded as the sole survivor.
                    if (
                        len(_SEEN_UNKNOWN_BUDGET_ROLES)
                        >= _SEEN_UNKNOWN_BUDGET_ROLES_MAX
                    ):
                        _SEEN_UNKNOWN_BUDGET_ROLES.clear()
                    _SEEN_UNKNOWN_BUDGET_ROLES.add(unknown)
            if not first_sighting:
                logger.debug(
                    "Project.context_budgets unknown budget_role(s) %s "
                    "(repeat).", list(unknown),
                )
            else:
                logger.warning(
                    "Project.context_budgets contains unknown budget_role(s) "
                    "%s — preserved across round-trip but will not be applied "
                    "(valid roles: %s).",
                    list(unknown), sorted(known),
                )
        return v

    @field_validator("inbox_caps")
    @classmethod
    def _validate_inbox_caps(
        cls, v: dict[str, dict[str, int]],
    ) -> dict[str, dict[str, int]]:
        """Validate partial cap overrides.
        The documented contract is that ``inbox_caps[role]`` may carry
        ``soft_cap`` and/or ``hard_cap``; missing keys inherit from
        :data:`inboxes.INBOX_DEFAULTS`. Previous validator read missing
        keys as ``0`` and rejected them, breaking partial overrides like
        ``{"drafter": {"hard_cap": 20}}``. Now we merge against
        ``INBOX_DEFAULTS`` BEFORE bound-checking so partial overrides
        validate against their effective post-merge values. The stored
        dict is left as the user supplied it (partial overrides survive
        round-trip) — only the validation logic merges."""
        if not v:
            return v
        from modulatio.inboxes import HARD_INBOX_CEILING, INBOX_DEFAULTS

        for role, inner in v.items():
            if not isinstance(inner, dict):
                raise ValueError(
                    f"Project.inbox_caps[{role!r}]: must be a dict with "
                    f"'soft_cap' and/or 'hard_cap' keys"
                )
            # Merge defaults so {"hard_cap": 20} alone is a valid
            # override (soft_cap inherits the default of 8 in that
            # case, not the rejected 0).
            merged = {**INBOX_DEFAULTS, **inner}
            soft = merged["soft_cap"]
            hard = merged["hard_cap"]
            if soft < 1 or hard < 1:
                raise ValueError(
                    f"Project.inbox_caps[{role!r}]: soft_cap and "
                    f"hard_cap must each be ≥ 1 after merging defaults "
                    f"(got soft={soft}, hard={hard})"
                )
            if soft > hard:
                raise ValueError(
                    f"Project.inbox_caps[{role!r}]: soft_cap {soft} > "
                    f"hard_cap {hard}"
                )
            if hard > HARD_INBOX_CEILING:
                raise ValueError(
                    f"Project.inbox_caps[{role!r}]: hard_cap {hard} > "
                    f"HARD_INBOX_CEILING {HARD_INBOX_CEILING}"
                )
        return v

    @field_validator("inbox_decay_overrides")
    @classmethod
    def _validate_inbox_decay_overrides(
        cls, v: dict[str, int],
    ) -> dict[str, int]:
        if not v:
            return v
        allowed = {"P0", "P1", "P2"}
        for prio, turns in v.items():
            if prio not in allowed:
                raise ValueError(
                    f"Project.inbox_decay_overrides[{prio!r}]: priority "
                    f"must be one of {sorted(allowed)}"
                )
            if turns < 1 or turns > 32:
                raise ValueError(
                    f"Project.inbox_decay_overrides[{prio!r}]={turns}: "
                    f"must be in 1..32"
                )
        return v


# ─── Activity events (slice #17) ────────────────────────────────────────────

@dataclass(frozen=True)
class ActivityEvent:
    """Orchestrator phase-transition event surfaced to subscribers.

    Phases emitted (slice #17 + paper-trail expansion):

    Top-level run:
        ``kickoff_started``, ``kickoff_ended``, ``scope_drift_warning``

    Leader work:
        ``leader_decompose_started``, ``leader_decompose_ended``,
        ``leader_verify_started``, ``leader_verify_ended``

    Per-task lifecycle:
        ``task_dispatched``, ``qc_started``, ``qc_verdict``,
        ``task_completed``

    Tickets:
        ``ticket_opened``

    TUI widgets (slice #21) subscribe via
    ``Orchestrator(activity_callback=...)`` to render per-agent activity +
    team aggregate in real time. Frozen so downstream consumers cannot
    mutate a shared event by accident.
    """
    agent_id: str
    role: str
    phase: str
    task_id: str | None
    timestamp: datetime
    #: Optional structured payload for richer phases (#80): e.g.
    #: ``leader_self_fix`` carries the concern + chosen remediation + the
    #: window outcome; ``leader_fix_window_opened/_closed`` carry the notice /
    #: decision. Additive + defaulted so every pre-#80 construction and
    #: subscriber is back-compat by construction.
    detail: "object | None" = None


__all__ = [
    "ActivityEvent",
    "ArtifactEvidence",
    "AssertionEvidence",
    "Evidence",
    "EvidenceClass",
    "EvidenceRequirement",
    "Goal",
    "GoalStatus",
    "MetricEvidence",
    "Project",
    "ProjectState",
    "ReportEvidence",
    "StateTransition",
    "Task",
    "TaskStatus",
    "Ticket",
    "TicketPriority",
    "TicketStatus",
]
