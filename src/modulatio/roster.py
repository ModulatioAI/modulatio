# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Agent roster — per-project composition of skills into dispatchable agents.

An **agent** is a named composition: a skill set + a model + routing
metadata. Dispatchable units are agents, not skills — skills are
portable capabilities that agents hold.

Persisted at ``<project_vault>/agents/<agent_id>.md`` as YAML-frontmatter
markdown. Frontmatter carries the structured routing schema
(skills list, model assignment, model_tier, cost_class, capability_tags,
capacity_cap, template_origin, freshness metadata). Body is the agent's
``identity`` string — self-description injected into prompts so a
custom agent can wear a specific voice without code changes.

No shared layer in slice #6a; teams compose their own rosters. Shared
template-agent bundles are a slice #6b-or-later addition.

Slice #6c dispatch will consume ``has_skill`` / ``covers`` plus the
routing fields to match tasks to the narrowest qualifying agent.
"""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import BaseModel, Field, field_validator

from modulatio import config
from modulatio.vault import project_dir, validate_registry_name

_OWN_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)


class Agent(BaseModel):
    """A persisted agent profile — which LLM runs it and the routing
    metadata dispatch consumes.

    Fields that drive routing (since the skill-library arc, Brick 3):

    - ``skills`` — **advisory / non-routing.** A producer no longer "holds"
      skills for dispatch; it checks out whatever a task needs from the
      shared library at run-time, so any producer can run any task. This
      list is kept for backward-compat (old rosters carry it) and as a seed-
      familiarity hint surfaced in the TUI and the chat skill-body injection.
      Dispatch does NOT match against it.
    - ``model`` + ``model_tier`` — the specific LLM and the tier it serves.
      ``model_tier`` is the producer's capability tier (it now comes from the
      MODEL, not from assigned skills — see ``_caps_from_model``). ``model``
      may be ``None`` when the agent inherits the CLI-wired model for its tier.
    - ``cost_class`` — free-local / paid-cloud / premium-cloud. Routing
      prefers the cheapest qualifying producer (cron-agent policy echo).
    - ``capability_tags`` — the producer's declared strengths, sourced from
      its model. The capability floor is a SOFT preference at dispatch
      (best-available + a Product Quality Report reservation, never a block).
    - ``capacity_cap`` — max concurrent tasks; the wave scheduler reads this
      to spread a wave across idle producers.
    """

    id: str
    name: str
    identity: str = ""
    skills: list[str] = Field(default_factory=list)
    model: str | None = None
    model_tier: str | None = None
    cost_class: str | None = None
    capability_tags: list[str] = Field(default_factory=list)
    #: Ordered fallback model preset-keys for THIS seat. When this seat's
    #: primary model is unavailable for a provider reason, the whole task is
    #: restarted on the next entry (engine-level, per-task — never mid-task).
    fallbacks: list[str] = Field(default_factory=list)
    capacity_cap: int = 1
    template_origin: str | None = None
    freshness_class: str | None = None
    last_verified_at: str | None = None
    #: Organizational tier (slice #6f-F). Routing treats these differently:
    #: - ``leader``: singleton bound at CLI; not dispatched. (``coordinator``
    #:   is a legacy value still tolerated for old rosters — removed for
    #:   skills-first #143; planning is the Leader's job.)
    #: - ``producer``: goes through the skill + semantic dispatch cascade.
    #: - ``qc``: dispatched under structural constraints (different agent
    #:   than the producer for this task + capability floor on model_tier).
    #: Default ``"producer"`` keeps existing rosters working without
    #: changes — explicit tier declarations are opt-in.
    tier: str = "producer"
    #: Per-agent context-budget override (max_input_tokens for any LLM
    #: call this agent makes). Consumed by ``resolve_for_dispatch`` at
    #: the agent rank — between project overrides and the experimental
    #: default for the budget_role mapped from ``tier``. ``None`` →
    #: inherit. Opt-in only.
    context_budget: int | None = None
    #: Per-agent reasoning override for the tool-loop chat path. ``None`` →
    #: inherit the default (thinking-OFF for producers/QC: ``/no_think`` keeps
    #: reasoning tokens from churning context). Set ``false`` in the agent file
    #: to let a genuinely reasoning-heavy producer deliberate. Opt-in only.
    disable_thinking: bool | None = None

    @field_validator("capacity_cap")
    @classmethod
    def _floor_capacity_cap(cls, v: int) -> int:
        """A capacity cap below 1 is never a legitimate dispatchable
        producer — the wave scheduler reads ``max(0, capacity_cap)`` and
        a 0/negative cap would leave skill-routed tasks perpetually
        DEFERRED_CAPACITY (silent goal stall) while the single-pick
        dispatch path, which ignores capacity, happily routes the same
        agent. Bind the invariant deterministically: floor at 1 so a
        non-dispatchable cap can never enter the roster, regardless of
        construction path (parse, wizard, default seed, direct, copy).

        NOTE: Pydantic v2 ``field_validator``\\ s do NOT fire on
        ``model_copy(update=...)`` (copy is non-revalidating by design), so
        this validator alone covers ctor/parse but leaves a ``model_copy``
        hole. ``model_copy`` is overridden below to re-floor so the
        "regardless of construction path" claim actually holds."""
        return v if v >= 1 else 1

    # re-sweep finding 1: floor the cap on the copy path too. field_validator
    # does not run on Pydantic's model_copy, so ``add_model``/``clear_model``
    # (and any future update) could otherwise slip a sub-1, non-dispatchable
    # cap into the roster.
    def model_copy(self, *, update: dict | None = None, deep: bool = False) -> "Agent":
        copied = super().model_copy(update=update, deep=deep)
        if copied.capacity_cap < 1:
            object.__setattr__(copied, "capacity_cap", 1)
        return copied

    def has_skill(self, skill: str) -> bool:
        return skill in self.skills

    def covers(self, required_skills: list[str]) -> bool:
        """True iff the agent holds every required skill. Empty requirement
        list is vacuously covered — every agent can take a skill-less
        task, though dispatch probably has other ways to route those."""
        agent_set = set(self.skills)
        return all(s in agent_set for s in required_skills)

    def covers_capabilities(self, required_capabilities: list[str]) -> bool:
        """True iff the agent declares every required capability tag.

        Capabilities are the fine-grained HOW axis layered on top of
        skill cover by slice #9a dispatch. Empty requirement is
        vacuously covered — the default for tasks that declare no
        capability constraint.
        """
        agent_set = set(self.capability_tags)
        return all(c in agent_set for c in required_capabilities)


def _parse_csv(raw: str) -> list[str]:
    s = raw.strip()
    if not s:
        return []
    if s.startswith("[") and s.endswith("]"):
        s = s[1:-1]
    return [p.strip() for p in s.split(",") if p.strip()]


def _parse_int(raw: str, default: int) -> int:
    s = raw.strip()
    if not s:
        return default
    try:
        return int(s)
    except ValueError:
        return default


def _parse_file(path: Path) -> Agent:
    raw = path.read_text(encoding="utf-8", errors="replace")
    m = _OWN_FRONTMATTER_RE.match(raw)
    meta: dict[str, str] = {}
    body = raw
    if m:
        for line in m.group(1).splitlines():
            if ":" in line:
                k, _, v = line.partition(":")
                meta[k.strip()] = v.strip()
        body = raw[m.end():].lstrip()
    else:
        body = raw.lstrip()

    cb_raw = meta.get("context_budget", "").strip()
    if cb_raw:
        try:
            context_budget = int(cb_raw)
        except ValueError as exc:
            raise ValueError(
                f"agent {path.stem!r}: context_budget must be an integer, "
                f"got {cb_raw!r}"
            ) from exc
        from modulatio.context_budget import (
            CTX_BUDGET_MIN_TOKENS, HARD_GLOBAL_CEILING,
        )
        if context_budget < CTX_BUDGET_MIN_TOKENS:
            raise ValueError(
                f"agent {path.stem!r}: context_budget {context_budget} "
                f"below minimum {CTX_BUDGET_MIN_TOKENS}"
            )
        if context_budget > HARD_GLOBAL_CEILING:
            raise ValueError(
                f"agent {path.stem!r}: context_budget {context_budget} "
                f"exceeds hard ceiling {HARD_GLOBAL_CEILING}"
            )
    else:
        context_budget = None

    dt_raw = meta.get("disable_thinking", "").strip().lower()
    if dt_raw in ("true", "1", "yes", "on"):
        disable_thinking: bool | None = True
    elif dt_raw in ("false", "0", "no", "off"):
        disable_thinking = False
    elif dt_raw == "":
        disable_thinking = None
    else:
        raise ValueError(
            f"agent {path.stem!r}: disable_thinking must be true/false, "
            f"got {dt_raw!r}"
        )

    model = meta.get("model") or None
    model_tier = meta.get("model_tier") or None
    cost_class = meta.get("cost_class") or None
    capability_tags = _parse_csv(meta.get("capability_tags", ""))
    # Brick 2: a producer that declares NO capabilities of its own draws them
    # from its MODEL (the new capability source). An agent carrying literal
    # caps — old rosters, skill-derived seeds — keeps them untouched.
    if model and not capability_tags and not model_tier and not cost_class:
        capability_tags, model_tier, cost_class = _caps_from_model(model)

    return Agent(
        id=meta.get("id") or path.stem,
        name=meta.get("name") or path.stem,
        identity=body.rstrip(),
        skills=_parse_csv(meta.get("skills", "")),
        model=model,
        model_tier=model_tier,
        cost_class=cost_class,
        capability_tags=capability_tags,
        fallbacks=_parse_csv(meta.get("fallbacks", "")),
        capacity_cap=_parse_int(meta.get("capacity_cap", ""), 1),
        template_origin=meta.get("template_origin") or None,
        freshness_class=meta.get("freshness_class") or None,
        last_verified_at=meta.get("last_verified_at") or None,
        tier=meta.get("tier") or "producer",
        context_budget=context_budget,
        disable_thinking=disable_thinking,
    )


def load(agent_id: str, project_code: str) -> Agent | None:
    """Return the agent profile if it exists in the project's roster,
    else ``None``. Missing agent is not an error at the loader level —
    the caller (Leader skill-gap detection in slice #6b) decides what
    to do with absence."""
    validate_registry_name(agent_id)  # raw id → path; block traversal reads
    path = project_dir(project_code) / "agents" / f"{agent_id}.md"
    if not path.exists():
        return None
    return _parse_file(path)


def list_agents(project_code: str) -> list[Agent]:
    """Return all agents in the project's roster, sorted by ``id``.

    Empty list when no ``agents/`` directory exists or the directory is
    empty. Slice #6c dispatch uses this to build the candidate pool.
    """
    agents_dir = project_dir(project_code) / "agents"
    if not agents_dir.exists():
        return []
    agents = [_parse_file(p) for p in agents_dir.glob("*.md")]
    return sorted(agents, key=lambda a: a.id)


def model_for_tier(project_code: str, tier: str) -> str | None:
    """The model of the single roster agent at ``tier`` (e.g. ``"leader"`` /
    ``"qc"``) — the SOLE source of that seat's model.

    Both lanes of a seat (the conversational/converse runner AND the
    team/orchestration runner) resolve here, so a leader can only ever carry ONE
    model: the split-brain where leader-converse and leader-decompose ran on
    different models is impossible by construction. Returns ``None`` when no agent
    of that tier carries a model. The ``leader``/``qc`` tiers are singletons; the
    first id-sorted match wins.
    """
    for agent in list_agents(project_code):
        if agent.tier == tier and agent.model:
            return agent.model
    return None


def save(agent: Agent, project_code: str) -> Path:
    """Persist an agent profile under the project roster.

    Creates ``<project>/agents/`` if needed. Returns the written path.
    """
    # The write boundary: agent.id becomes the file stem. Validate here so a
    # traversal id can't escape the agents dir no matter how the Agent was
    # produced — including model_copy(update={"id": ...}), which bypasses the
    # field validators.
    validate_registry_name(agent.id)
    root = project_dir(project_code) / "agents"
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{agent.id}.md"

    fm_lines: list[str] = [
        f"id: {agent.id}",
        f"name: {agent.name}",
        f"tier: {agent.tier}",
        f"skills: {', '.join(agent.skills)}",
        f"model: {agent.model or ''}",
        f"model_tier: {agent.model_tier or ''}",
        f"cost_class: {agent.cost_class or ''}",
        f"capability_tags: {', '.join(agent.capability_tags)}",
        f"fallbacks: {', '.join(agent.fallbacks)}",
        f"capacity_cap: {agent.capacity_cap}",
    ]
    if agent.template_origin is not None:
        fm_lines.append(f"template_origin: {agent.template_origin}")
    if agent.freshness_class is not None:
        fm_lines.append(f"freshness_class: {agent.freshness_class}")
    if agent.last_verified_at is not None:
        fm_lines.append(f"last_verified_at: {agent.last_verified_at}")
    if agent.context_budget is not None:
        fm_lines.append(f"context_budget: {agent.context_budget}")
    if agent.disable_thinking is not None:
        fm_lines.append(f"disable_thinking: {str(agent.disable_thinking).lower()}")

    content = "---\n" + "\n".join(fm_lines) + "\n---\n\n" + agent.identity.rstrip() + "\n"
    path.write_text(content, encoding="utf-8")
    return path


_COST_RANK = {"free-local": 1, "paid-cloud": 2, "premium-cloud": 3}


def _derive_agent_caps(skill_names: list[str], project_code: str) -> tuple[list[str], str | None, str | None]:
    """Compute (capability_tags, model_tier, cost_class) for an agent
    holding ``skill_names``, reading each skill's frontmatter once.

    - ``capability_tags``: union of every held skill's ``capability_tags``
      AND ``required_capabilities``, deduped, order-preserving across
      the skill iteration order. Unioning both axes means the seeded
      agent satisfies dispatch for any task requiring what its skills
      advertise OR demand of their executor.
    - ``model_tier``: the FIRST held skill's ``model_tier`` (primary
      skill drives the agent's model tier). Multi-skill agents pick up
      the dominant tier by convention; humans can edit after seed.
    - ``cost_class``: the MAX ``cost_class`` across held skills by
      ``_COST_RANK`` ordering (premium > paid > free). Captures the
      tightest budget bracket dispatch should assume for the agent.
    """
    from modulatio import skills  # local import to avoid circular

    seen_caps: list[str] = []
    primary_tier: str | None = None
    max_cost: str | None = None
    max_cost_rank = 0
    for idx, sname in enumerate(skill_names):
        s = skills.load_with_metadata(sname, project_code=project_code)
        for cap in (*s.capability_tags, *s.required_capabilities):
            if cap and cap not in seen_caps:
                seen_caps.append(cap)
        if idx == 0 and s.model_tier:
            primary_tier = s.model_tier
        if s.cost_class:
            rank = _COST_RANK.get(s.cost_class, 0)
            if rank > max_cost_rank:
                max_cost_rank = rank
                max_cost = s.cost_class
    return seen_caps, primary_tier, max_cost


def _caps_from_model(model_key: str | None) -> tuple[list[str], str | None, str | None]:
    """Compute ``(capability_tags, model_tier, cost_class)`` for a producer
    bound to ``model_key`` — the MODEL is the capability source once producers
    stop holding skills (Brick 2 of the skill-library arc).

    An explicit per-model tag stored on the preset (wizard-set) always wins;
    otherwise a sensible default is inferred from the model id via
    :mod:`modulatio.model_capabilities`. Returns empties when the model is
    unknown and uninferrable — the caller keeps whatever the agent already had.
    """
    if not model_key:
        return [], None, None
    from modulatio import model_capabilities, model_presets

    preset = model_presets.get_preset(model_key)
    if not preset:
        return [], None, None
    explicit_caps = preset.get("capability_tags")
    if isinstance(explicit_caps, str):
        explicit_caps = _parse_csv(explicit_caps)
    inf_tier, inf_cost, inf_caps = model_capabilities.infer_for_preset(preset)
    caps = list(explicit_caps) if explicit_caps else list(inf_caps)
    tier = preset.get("model_tier") or inf_tier
    cost = preset.get("cost_class") or inf_cost
    return caps, tier, cost


# Ordered roster template. Each entry declares the agent id, the skills
# it holds, its organizational tier (drives QC/producer-escalation
# routing), and the CLI model-flag key it should bind to. Kept as data
# so adding a new default agent is a one-line addition, not a rewrite.
#
# NOTE: the ``"producer"`` agent holds the ``drafter`` skill. The
# ``drafter`` skill-name is a pre-#9 naming convention flagged in the
# deferred cosmetic-cleanup backlog — renaming it is out of scope for
# the default-roster seed.
# Skills-first: the fallback roster has NO standalone planner agent —
# that prior role was removed engine-side, and task planning is the
# Leader's job (the planner runner uses the Leader's model). Structural
# roles are the Leader (required) and an optional QC; the producer is a
# skill-holder. (Research is a
# capability the producer composes — its rigorous-sourcing + web-search skills
# already cover the research/web-search caps — not a separate role; Brick A.)
_DEFAULT_ROSTER_TEMPLATE: tuple[tuple[str, tuple[str, ...], str, str], ...] = (
    ("leader", ("leader", "leader-verify", "leader-plan", "leader-plan-approve", "leader-reflect"), "leader", "leader_model"),
    ("producer", ("drafter", "drafter-edit", "drafter-patch", "rigorous-sourcing", "web-search"), "producer", "producer_model"),
    ("qc", ("qc",), "qc", "qc_model"),
)


_DEFAULT_IDENTITY = (
    "Default seeded agent. Holds the skills listed above; identity is a "
    "placeholder — edit this body to give the agent a specific voice or "
    "domain focus for your project."
)


def add_agent(
    *,
    project_code: str,
    agent_id: str,
    name: str,
    identity: str,
    skills: list[str],
    model: str | None = None,
    tier: str = "producer",
    capacity_cap: int = 1,
) -> Agent:
    """Create a new agent under ``<project>/agents/``.

    Slice #18: wizard-facing API for the TUI's Agents tab. Derives
    ``capability_tags`` / ``model_tier`` / ``cost_class`` from the
    supplied skill names via ``_derive_agent_caps`` so the new agent
    covers every dispatch filter its skills advertise or demand — same
    policy as ``seed_default_roster``.

    Idempotency: raises ``FileExistsError`` if an agent with ``agent_id``
    already lives under the project. Wizards validate before calling.
    """
    validate_registry_name(agent_id)  # raw id → path; block traversal before the stat
    agents_dir = project_dir(project_code) / "agents"
    target = agents_dir / f"{agent_id}.md"
    if target.exists():
        raise FileExistsError(
            f"agent already exists at {target}; pick a different id "
            f"or edit the existing file"
        )

    caps, model_tier, cost_class = _derive_agent_caps(skills, project_code)
    agent = Agent(
        id=agent_id,
        name=name,
        identity=identity,
        skills=list(skills),
        model=model,
        model_tier=model_tier,
        cost_class=cost_class,
        capability_tags=caps,
        capacity_cap=capacity_cap,
        tier=tier,
    )
    save(agent, project_code)
    return agent


def add_model(
    *,
    project_code: str,
    agent_id: str,
    model: str,
) -> Agent:
    """Update an existing agent's ``model`` id.

    Slice #18: wizard-facing API for the TUI's Models tab. The rest of
    the agent (identity, skills, caps) is preserved untouched. Raises
    ``FileNotFoundError`` when the agent doesn't exist — wizards should
    validate against ``list_agents`` first.
    """
    existing = load(agent_id, project_code)
    if existing is None:
        raise FileNotFoundError(
            f"agent {agent_id!r} not found under project {project_code!r}"
        )
    updated = existing.model_copy(update={"model": model})
    save(updated, project_code)
    return updated


def set_fallbacks(
    *,
    project_code: str,
    agent_id: str,
    fallback_keys: "list[str] | tuple[str, ...]",
) -> Agent:
    """Set a seat's ordered fallback model chain. Sanitized against the agent's
    own model via :func:`modulatio.model_presets.sanitize_fallback_chain` — drops
    a self-reference, unknown keys, duplicates, and any that would route a
    protected model through OpenRouter. Mirrors :func:`add_model` (preserves the
    rest of the agent). Raises ``FileNotFoundError`` when the agent doesn't exist.
    """
    from modulatio import model_presets
    existing = load(agent_id, project_code)
    if existing is None:
        raise FileNotFoundError(
            f"agent {agent_id!r} not found under project {project_code!r}"
        )
    cleaned = model_presets.sanitize_fallback_chain(
        existing.model or "", list(fallback_keys or [])
    )
    updated = existing.model_copy(update={"fallbacks": cleaned})
    save(updated, project_code)
    return updated


def clear_model(
    *,
    project_code: str,
    agent_id: str,
) -> Agent:
    """Clear an existing agent's ``model`` assignment (sets to None).

    Slice #21: backs the Models-tab Clear button. The agent stays in
    the roster — only the model is cleared. Useful when the user wants
    to keep an agent definition but defer model assignment, or when a
    previously-assigned model preset has been removed from the registry.

    Raises ``FileNotFoundError`` when the agent doesn't exist.
    """
    existing = load(agent_id, project_code)
    if existing is None:
        raise FileNotFoundError(
            f"agent {agent_id!r} not found under project {project_code!r}"
        )
    updated = existing.model_copy(update={"model": None})
    save(updated, project_code)
    return updated


def remove_agent(*, project_code: str, agent_id: str) -> bool:
    """Delete an agent from the roster. Returns True if removed, False if it
    didn't exist. Backs the configurator's agent builder (drop a producer)."""
    validate_registry_name(agent_id)  # raw id → unlink path; block traversal deletes
    path = project_dir(project_code) / "agents" / f"{agent_id}.md"
    if not path.exists():
        return False
    path.unlink()
    return True


def seed_default_roster(
    project_code: str,
    *,
    leader_model: str | None,
    coordinator_model: str | None,
    producer_model: str | None,
    qc_model: str | None,
    # DEPRECATED/ignored — research is a capability a producer composes, not a
    # role (Brick A); accepted for back-compat (mirrors coordinator_model).
    researcher_model: str | None = None,
) -> list[Agent]:
    """Write the default agent roster to ``<project>/agents/``.

    Two seeding modes:

    1. **Wizard team template present** (``~/.config/modulatio/team_template.json``
       exists from a setup-wizard run): seed every agent in the template
       with its wizard-picked model, skills, identity, and capability_tags.
       Per-role model kwargs are ignored — the wizard's per-agent picks
       win. This is the v2-architecture path where mandatory triad +
       wizard-chosen workers carry into every new project.
    2. **No template** (pre-wizard or user deleted it): fall back to the
       hardcoded ``_DEFAULT_ROSTER_TEMPLATE`` and bind per-role model
       kwargs from the caller. Preserves out-of-the-box boot for users
       who haven't run the wizard.

    Idempotent per-agent: if ``<project>/agents/<id>.md`` already exists
    it is left alone (defensive against human pre-seeding; the normal
    call site in ``cli.kickoff`` already gates on net-new projects).

    Returns the list of agents written (or skipped) in template order.
    """
    template = config.load_team_template()
    if template:
        return _seed_from_team_template(project_code, template)
    return _seed_from_default_template(
        project_code,
        leader_model=leader_model,
        coordinator_model=coordinator_model,
        producer_model=producer_model,
        qc_model=qc_model,
    )


def create_project(code: str, objective: str = "", *, exist_ok: bool = False) -> Path:
    """Create a project folder AND seed it with the install team, so a fresh
    project is immediately ready to work in — bundling ``init_project`` +
    ``seed_default_roster`` (when a team template exists its per-agent picks
    win; the model kwargs below only feed the no-template fallback). The
    setup wizard and the PROJECTS-tab [New] button both route through here;
    the CLI ``kickoff``/stub paths pass caller-supplied models and stay
    separate. Sibling lifecycle ops live where their bundled side-effect
    demands: ``vault.init_project`` (folder), ``backup.delete_project``
    (backup + remove).

    The display name is the code. Raises ``ValueError`` on an invalid code,
    and ``FileExistsError`` if the project exists and ``exist_ok`` is False;
    ``exist_ok=True`` reuses an existing folder (idempotent seed). If seeding
    fails after a NET-NEW folder is created, it's rolled back so a half-made
    project can't strand in ``list_projects`` — a pre-existing folder reused
    under ``exist_ok`` is never rolled back. Returns the project dir.
    """
    import shutil

    from modulatio import vault

    pre_existed = vault.project_dir(code).exists()  # also validates the code
    root = vault.init_project(code, code, objective, exist_ok=exist_ok)
    seeded = False
    try:
        models = config.get_default_models()
        seed_default_roster(
            code,
            leader_model=models.get("leader"),
            coordinator_model=models.get("planner") or models.get("coordinator"),
            producer_model=models.get("producer") or models.get("specialist"),
            qc_model=models.get("qc"),
        )
        seeded = True
    finally:
        if not seeded and not pre_existed:
            shutil.rmtree(root, ignore_errors=True)
    return root


def _seed_from_default_template(
    project_code: str,
    *,
    leader_model: str | None,
    coordinator_model: str | None,
    producer_model: str | None,
    qc_model: str | None,
    # DEPRECATED/ignored — research is a capability a producer composes, not a
    # role (Brick A); accepted for back-compat (mirrors coordinator_model).
    researcher_model: str | None = None,
) -> list[Agent]:
    """Seed the hardcoded fallback roster (no wizard team template)."""
    models = {
        "leader_model": leader_model,
        "coordinator_model": coordinator_model,
        "producer_model": producer_model,
        "qc_model": qc_model,
    }
    agents_dir = project_dir(project_code) / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)

    written: list[Agent] = []
    for agent_id, skill_names, tier, model_key in _DEFAULT_ROSTER_TEMPLATE:
        target = agents_dir / f"{agent_id}.md"
        if target.exists():
            existing = _parse_file(target)
            written.append(existing)
            continue

        caps, model_tier, cost_class = _derive_agent_caps(
            list(skill_names), project_code
        )
        agent = Agent(
            id=agent_id,
            name=agent_id,
            identity=_DEFAULT_IDENTITY,
            skills=list(skill_names),
            model=models[model_key],
            model_tier=model_tier,
            cost_class=cost_class,
            capability_tags=caps,
            tier=tier,
            template_origin="default-roster",
        )
        save(agent, project_code)
        written.append(agent)
    return written


def _seed_from_team_template(
    project_code: str, template: list[dict]
) -> list[Agent]:
    """Seed agents defined by the wizard's team_template.json.

    Each entry already carries the model + skills + identity + caps the
    user picked, so this is a thin write loop. Idempotent per-agent.
    """
    agents_dir = project_dir(project_code) / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)

    written: list[Agent] = []
    for entry in template:
        agent_id = entry.get("id")
        if not agent_id:
            continue
        # Untrusted template id becomes a path below — validate before any
        # stat/read/write so a traversal id can't escape the agents dir.
        validate_registry_name(agent_id)
        target = agents_dir / f"{agent_id}.md"
        if target.exists():
            written.append(_parse_file(target))
            continue
        agent = Agent(
            id=agent_id,
            name=entry.get("name") or agent_id,
            identity=entry.get("identity") or _DEFAULT_IDENTITY,
            skills=list(entry.get("skills") or []),
            model=entry.get("model"),
            model_tier=entry.get("model_tier"),
            cost_class=entry.get("cost_class"),
            capability_tags=list(entry.get("capability_tags") or []),
            tier=entry.get("tier") or "producer",
            template_origin=entry.get("template_origin") or "wizard-team",
            # Per-agent context budget (model-agnostic, by role). Usually
            # None → the tested per-role PIANO defaults govern; set only when
            # the user deliberately overrode it in the wizard (discouraged).
            context_budget=entry.get("context_budget"),
            disable_thinking=entry.get("disable_thinking"),
        )
        save(agent, project_code)
        written.append(agent)
    return written


__all__ = ["Agent", "list_agents", "load", "model_for_tier", "save", "seed_default_roster"]
