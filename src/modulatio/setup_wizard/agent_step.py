"""Team-formation step — skills-first (#143).

The user first chooses WHAT THE TEAM SHOULD BE ABLE TO DO (skills) and a
model to power them; skills sharing a model collapse into one producer
(skill-holder). Then the two structural roles are bound:
- Producers (skill-holders) come first — the primary act of forming a team.
- Structural roles: Leader (plans + decides) and QC (verifier). A prior
  standalone planner role was removed engine-side; planning is now the
  Leader's job.
- Floor: Leader + QC + at least one skill-holder = 3; soft cap 10.
"""

from __future__ import annotations

from typing import Any

from modulatio import model_presets, skills as skills_mod, templates as templates_pkg, theme
from modulatio.setup_wizard import steps


MIN_AGENTS = 3  # structural roles (Leader + QC) + 1 producer/skill-holder
MAX_AGENTS = 10


def _pick_template_for_tier(tier: str, current: str | None = None) -> Any:
    """Pick a template for a structural-role tier (leader / qc).

    Returns the chosen template_id, BACK, or QUIT.
    """
    candidates = [t for t in templates_pkg.list_templates(mandatory_only=True) if t.tier == tier]
    if not candidates:
        theme.error(f"No templates found for tier '{tier}'. Aborting.")
        return steps.QUIT

    options = [(f"{t.name:30s}  {theme.color(t.description, 'muted')}", t.template_id) for t in candidates]
    default_idx = 0
    if current:
        for i, t in enumerate(candidates):
            if t.template_id == current:
                default_idx = i
                break
    return steps.pick_option(f"{tier.title()} template", options, default_index=default_idx)


def _available_presets() -> list[tuple[str, dict]]:
    """Return [(preset_key, preset_dict)] for every model preset added in
    the wizard's models step. The models-step floor (≥1 added) guarantees
    this list is non-empty by the time agent provisioning runs."""
    return sorted(model_presets.load_presets().items())


def _resolve_endpoint_label(preset_key: str) -> str:
    """Render a short endpoint hint for the agent's model picker."""
    p = model_presets.get_preset(preset_key) or {}
    return f"{p.get('api_format', '?')}/{p.get('model', '?')}"


def _pick_model(
    role: str,
    default_models: dict[str, str],
    *,
    staged_keys: dict[str, str] | None = None,
    current: str | None = None,
) -> Any:
    """Pick a model preset key for an agent. Shows a numbered picker of
    every preset registered in step 4. The agent stores the preset KEY,
    not the bare model id — dispatch resolves provider + format at call
    time.

    Returns the chosen preset key (str), BACK, or QUIT.
    """
    available = _available_presets()
    if not available:
        # The models-step floor should prevent this. Defensive only.
        theme.error(
            "No models curated. Go back to step 4 and add at least one before "
            "provisioning agents."
        )
        return steps.BACK

    role_default = current or default_models.get(role) or default_models.get("specialist")
    options: list[tuple[str, str]] = []
    default_idx = 0
    for i, (key, preset) in enumerate(available):
        endpoint = _resolve_endpoint_label(key)
        marker = "  ← role default" if key == role_default else ""
        label = f"{preset.get('label', key):42s}  → {endpoint[:24]:24s}  ({key}){marker}"
        options.append((label, key))
        if key == role_default:
            default_idx = i

    print(theme.color(f"  Pick the model for {role}:", "primary", bold=True))
    return steps.pick_option(f"{role} model", options, default_index=default_idx)


# Structural-role skills are not "team skills" the user assigns to
# producers — they belong to the Leader/QC seats picked separately.
_STRUCTURAL_SKILLS = frozenset({
    "leader", "leader-verify", "leader-iterate", "qc", "coordinator",
})


def _select_from_skill_list(available: list[str], *, empty_msg: str) -> Any:
    """Render a numbered skill list + read a comma-separated / 'all' choice.
    Shared by team-skill selection and per-producer subset selection so the
    two can never drift in parsing/validation. Returns list[str]
    (order-preserving, de-duped), BACK, or QUIT."""
    for i, sname in enumerate(available, 1):
        skill = skills_mod.load_with_metadata(sname)
        desc = (skill.description or "")[:50]
        print(f"    {theme.color(f'{i:>2}', 'highlight')}) {sname:18s}  {theme.color(desc, 'muted')}")
    print()
    print(theme.color("  Enter comma-separated numbers (e.g. 1,3,5) or 'all'.", "muted"))

    while True:
        try:
            raw = input(theme.prompt_color("  Skills: ", "highlight")).strip().lower()
        except (EOFError, KeyboardInterrupt):
            return steps.QUIT
        if raw in ("b", "q"):
            return steps.BACK if raw == "b" else steps.QUIT
        if raw == "all":
            return list(available)
        if raw == "":
            theme.error(empty_msg)
            continue
        try:
            picks = [int(p.strip()) for p in raw.split(",") if p.strip()]
        except ValueError:
            theme.error("Enter comma-separated numbers or 'all'.")
            continue
        if any(p < 1 or p > len(available) for p in picks):
            theme.error(f"Pick numbers 1-{len(available)} only.")
            continue
        # de-dupe, preserve order
        seen: list[str] = []
        for p in picks:
            s = available[p - 1]
            if s not in seen:
                seen.append(s)
        return seen


def _pick_team_skills() -> Any:
    """Skills-first (#143): ask which SKILLS the team needs. Multi-select
    from the shared skill registry (structural-role skills filtered out).
    Returns list[str], BACK, or QUIT.

    UX: numbered list + comma-separated number input, plus 'all' shortcut.
    These skills become producers (skill-holders) the team routes work to.
    """
    available = sorted(s for s in skills_mod.list_skills() if s not in _STRUCTURAL_SKILLS)
    print()
    if not available:
        theme.warn("No shared work skills are installed yet.")
        theme.muted("Add skills via the Skills tab or `modulatio kickoff` after install.")
        try:
            input(theme.prompt_color("  Press Enter to continue...", "muted"))
        except (EOFError, KeyboardInterrupt):
            return steps.QUIT
        return []

    print(theme.color("  What should your team be able to do?", "primary", bold=True))
    print(theme.color("  Pick the skills your producers will hold — tasks route to whoever holds the matching skill.", "muted"))
    print()
    return _select_from_skill_list(
        available,
        empty_msg="Pick at least one skill — the team needs at least one producer.",
    )


def _build_skill_holder(skill_names: list[str], model: str, *, index: int | None = None) -> dict:
    """Materialize a producer (skill-holder) wizard-state dict from a set of
    skills + a model. capability_tags = union of each skill's tags +
    required_capabilities (same derivation as roster.seed_default_roster)."""
    capability_tags: list[str] = []
    for sname in skill_names:
        skill = skills_mod.load_with_metadata(sname)
        for tag in list(skill.capability_tags) + list(skill.required_capabilities):
            if tag and tag not in capability_tags:
                capability_tags.append(tag)
    pid = "producer" if index is None else f"producer_{index}"
    name = "Producer" if index is None else f"Producer {index}"
    return {
        "id": pid,
        "name": name,
        "role": "Producer",
        "identity": (
            f"You are a skill-holder on this team. Your skills are "
            f"{', '.join(skill_names)}. Work requiring those skills will be "
            f"routed to you."
        ),
        "skills": list(skill_names),
        "capability_tags": capability_tags,
        "model_tier": "generalist",
        "cost_class": "paid-cloud",
        "tier": "producer",
        "model": model,
        "template_origin": "skill-holder",
    }


def _build_agent_from_template(template_id: str, model: str) -> dict:
    """Materialize a template into a wizard-state agent dict (not yet a roster.Agent)."""
    t = templates_pkg.get_template(template_id)
    if t is None:
        raise ValueError(f"Template '{template_id}' not found")
    return {
        "id": template_id,
        "name": t.name,
        "role": t.name,
        "identity": t.identity,
        "skills": list(t.default_skills),
        "capability_tags": list(t.default_capability_tags),
        "model_tier": t.default_model_tier,
        "cost_class": t.cost_class,
        "tier": t.tier,
        "model": model,
        "template_origin": t.template_id,
    }


def _provision_triad(state: dict, default_models: dict[str, str]) -> Any:
    """Walk the mandatory structural roles: Leader → QC.

    Skills-first (#143): these are the only two structural roles. The
    Leader is the one deliberative seat (it also drives task planning);
    QC is the verifier. Everything else is a producer/skill-holder,
    provisioned in the skills step. (A prior standalone planner role
    was removed engine-side; planning is the Leader's job.) The state
    key stays ``triad_agents`` for back-compat with finalize; it now
    holds the [leader, qc] pair.
    """
    structural: list[dict] = state.get("triad_agents", [])
    by_tier = {a["tier"]: a for a in structural}

    for tier in ("leader", "qc"):
        theme.clear_screen()
        label = "Leader (plans + decides)" if tier == "leader" else "Quality Control (verifier)"
        theme.step_header(4, 7, f"Structural role — {label} (mandatory)")

        current_template = by_tier.get(tier, {}).get("template_origin")
        template_id = _pick_template_for_tier(tier, current=current_template)
        if template_id in (steps.BACK, steps.QUIT):
            return template_id

        current_model = by_tier.get(tier, {}).get("model")
        model = _pick_model(tier, default_models, staged_keys=state.get("staged_api_keys"), current=current_model)
        if model in (steps.BACK, steps.QUIT):
            return model

        by_tier[tier] = _build_agent_from_template(template_id, model)

    state["triad_agents"] = [by_tier["leader"], by_tier["qc"]]
    return "configured"


def _pick_skill_subset(team_skills: list[str], *, producer_label: str) -> Any:
    """Pick which of the team's already-chosen skills one pool producer holds
    (the subset path; the caller offers 'all' first for the common case).
    Constrained to ``team_skills`` so a producer can't acquire a skill the
    team never picked. Returns list[str], BACK, or QUIT."""
    print()
    print(theme.color(f"  Which skills should {producer_label} hold?", "primary", bold=True))
    return _select_from_skill_list(
        team_skills,
        empty_msg="Pick at least one skill for this producer.",
    )


def _provision_producer_pool(
    team_skills: list[str],
    default_models: dict[str, str],
    *,
    staged_keys: dict[str, str] | None,
    reserved: int,
) -> Any:
    """Build a POOL of producers, one at a time. Each gets its own model and
    either ALL the team skills or a chosen subset. Capped so
    Leader + QC + producers <= ``MAX_AGENTS``.

    The payoff is concurrency: tasks route to whichever pool member holds the
    matching skill, and under concurrent waves
    (``Project.concurrent_waves_enabled`` / ``MODULATIO_CONCURRENT_WAVES``)
    the scheduler reserves per-agent capacity and runs independent tasks
    across the pool in parallel. Under the sequential default a redundant
    pool is harmless but idle — the picker resolves identical-skill agents to
    one of them, so the others wait their turn.

    Returns list[dict] (>=1 producer), BACK, or QUIT."""
    max_producers = max(1, MAX_AGENTS - reserved)
    producers: list[dict] = []
    while len(producers) < max_producers:
        n = len(producers) + 1
        theme.clear_screen()
        theme.step_header(4, 7, f"Producer pool — member {n} of up to {max_producers}")
        model = _pick_model(f"producer {n}", default_models, staged_keys=staged_keys)
        if model in (steps.BACK, steps.QUIT):
            return model
        if steps.confirm_yn(
            f"Hold ALL {len(team_skills)} team skills? (No = pick a subset)",
            default=True,
        ):
            sks = list(team_skills)
        else:
            sks = _pick_skill_subset(team_skills, producer_label=f"producer {n}")
            if sks in (steps.BACK, steps.QUIT):
                return sks
        producers.append(_build_skill_holder(sks, model, index=n))
        if len(producers) >= max_producers:
            theme.muted(f"  Reached the {MAX_AGENTS}-member team cap.")
            break
        if not steps.confirm_yn("Add another producer to the pool?", default=False):
            break

    # Coverage check: with the subset path, the pool may not hold every team
    # skill. An uncovered skill means tasks requiring it route to no producer
    # and gap — warn (non-blocking; the user can re-run setup or edit the
    # roster). The one/per shapes can't hit this since every skill gets a model.
    covered = {s for p in producers for s in p.get("skills", [])}
    missing = [s for s in team_skills if s not in covered]
    if missing:
        theme.warn(
            f"  No pool producer holds: {', '.join(missing)}. Tasks needing "
            f"those skills won't route until a producer holds them."
        )
    return producers


def _provision_workers(state: dict, default_models: dict[str, str]) -> Any:
    """Skills-first (#143) producer provisioning. Instead of provisioning
    named worker agents by role/template, the user picks the SKILLS the
    team needs and assigns a model to power them. Skills sharing a model
    collapse into one producer (skill-holder); tasks route to whichever
    producer holds the matching skill. Sets ``state['worker_agents']``."""
    staged_keys = state.get("staged_api_keys")

    theme.clear_screen()
    theme.step_header(4, 7, "Build your team — start with skills")
    print(theme.color(
        "  A Modulatio team is defined by what it can DO. Pick your producers' "
        "skills first — the two fixed seats (Leader + QC) are configured right "
        "after.", "muted",
    ))
    print()

    # 1. Which skills does the team need?
    picked = _pick_team_skills()
    if picked in (steps.BACK, steps.QUIT):
        return picked
    if not picked:
        theme.error("Pick at least one skill — the team needs at least one producer.")
        return steps.BACK

    # 2. How should producers be staffed? Three shapes:
    #    one  — a single producer holds all picked skills (one model)
    #    per  — one producer per skill, each its own model (disjoint skills)
    #    pool — a pool of producers, each its own model + chosen skills (the
    #           redundant-pool shape: tasks load-balance across it, and under
    #           concurrent waves independent tasks run in parallel on it)
    reserved = len(state.get("triad_agents", [])) or 2  # Leader + QC seats
    shape = steps.pick_option(
        "How should producers be staffed?",
        [
            ("One model powers all the skills (a single producer)", "one"),
            ("A different model per skill (one producer each)", "per"),
            (f"A pool of producers — each its own model + skills "
             f"(up to {max(1, MAX_AGENTS - reserved)}; the team shares the work)",
             "pool"),
        ],
        default_index=0,
    )
    if shape in (steps.BACK, steps.QUIT):
        return shape

    if shape == "pool":
        producers = _provision_producer_pool(
            picked, default_models, staged_keys=staged_keys, reserved=reserved,
        )
        if producers in (steps.BACK, steps.QUIT):
            return producers
    else:
        skill_to_model: dict[str, str] = {}
        if shape == "per":
            for sk in picked:
                theme.clear_screen()
                theme.step_header(4, 7, f"Producers — model for skill '{sk}'")
                model = _pick_model(f"skill '{sk}'", default_models, staged_keys=staged_keys)
                if model in (steps.BACK, steps.QUIT):
                    return model
                skill_to_model[sk] = model
        else:  # "one"
            theme.clear_screen()
            theme.step_header(4, 7, "Producers — model for all producer skills")
            model = _pick_model("all producer skills", default_models, staged_keys=staged_keys)
            if model in (steps.BACK, steps.QUIT):
                return model
            for sk in picked:
                skill_to_model[sk] = model

        # Group skills by model → one producer (skill-holder) per distinct model.
        by_model: dict[str, list[str]] = {}
        for sk in picked:  # preserve pick order
            by_model.setdefault(skill_to_model[sk], []).append(sk)
        multi = len(by_model) > 1
        producers = []
        for i, (model, sks) in enumerate(by_model.items(), 1):
            producers.append(_build_skill_holder(sks, model, index=i if multi else None))

    # Soft cap: structural roles + producers shouldn't exceed MAX_AGENTS.
    if len(state.get("triad_agents", [])) + len(producers) > MAX_AGENTS:
        theme.warn(
            f"That's more than {MAX_AGENTS} team members. Consider sharing a "
            f"model across skills to keep the team lean."
        )

    state["worker_agents"] = producers
    return "configured"


# Headline tested per-role context budgets (model-agnostic, by role). These
# mirror context_budget.EXPERIMENTAL_DEFAULTS — the Leader runs 8k–16k by call
# pattern, 12k is the reflect anchor (Lovecraft's number, at the Stanford
# "Lost in the Middle" onset). Shown to the user as the discouraged baseline.
_TESTED_ROLE_BUDGETS = (
    ("leader", 12_000, "reflect anchor; 8k–16k across call patterns"),
    ("producer", 16_000, "drafting"),
    ("qc", 8_000, "verification"),
)


def _prompt_role_budget(role: str, default_tokens: int) -> int | None:
    """Prompt one role's context budget. Blank → ``None`` (keep the tested
    default). Validates against the same thresholds the CLI/roster enforce:
    refuses < MIN or > HARD_GLOBAL_CEILING, confirms above CONFIRM_THRESHOLD.
    Returns the chosen int, or ``None`` to keep the default."""
    from modulatio.context_budget import (
        CTX_BUDGET_CONFIRM_THRESHOLD,
        CTX_BUDGET_MIN_TOKENS,
        HARD_GLOBAL_CEILING,
    )
    while True:
        try:
            raw = input(theme.prompt_color(
                f"  {role} budget [{default_tokens} — Enter to keep]: ", "highlight",
            )).strip()
        except (EOFError, KeyboardInterrupt):
            return None
        if raw == "":
            return None
        try:
            val = int(raw.replace("_", "").replace(",", ""))
        except ValueError:
            theme.error("Enter a whole number of tokens, or Enter to keep the default.")
            continue
        if val < CTX_BUDGET_MIN_TOKENS:
            theme.error(f"Too small — minimum is {CTX_BUDGET_MIN_TOKENS} tokens.")
            continue
        if val > HARD_GLOBAL_CEILING:
            theme.error(
                f"Refused — {val} exceeds the {HARD_GLOBAL_CEILING}-token hard "
                f"ceiling. Large windows broke the engine in testing; that ceiling "
                f"is deliberate."
            )
            continue
        if val > CTX_BUDGET_CONFIRM_THRESHOLD:
            if not steps.confirm_yn(
                f"  {val} is past the {CTX_BUDGET_CONFIRM_THRESHOLD}-token "
                f"measured degradation threshold. Use it anyway?",
                default=False,
            ):
                continue
        return val


def _maybe_customize_context_budgets(state: dict) -> None:
    """Discouraged opt-in to override the tested per-role context budgets.

    Context is allocated BY ROLE, model-agnostic. The defaults are the tuned
    Project-Sid/PIANO values from two days of testing — large context windows
    broke the engine; small role-bounded windows are the design. So the
    default path keeps them (sets nothing → the engine's per-role defaults
    govern); customization is gated behind a warn and defaults to No."""
    theme.clear_screen()
    theme.step_header(4, 7, "Context budgets (tuned — change is discouraged)")
    print(theme.color(
        "  Modulatio allocates context BY ROLE, not by model. These per-role "
        "budgets are tuned from extensive Project-Sid testing — large context "
        "windows broke the engine; small, role-bounded windows are the design.",
        "muted",
    ))
    print()
    for role, tokens, note in _TESTED_ROLE_BUDGETS:
        print(f"    {theme.color(role, 'highlight'):20s} {tokens:>7,} tokens  "
              f"{theme.color(note, 'muted')}")
    print()
    theme.warn(
        "  Changing these is discouraged. "
        "Override only if you have a specific, measured reason."
    )
    if not steps.confirm_yn(
        "Customize context budgets? (tested defaults strongly recommended)",
        default=False,
    ):
        return  # keep tested defaults — agents carry no override

    print()
    theme.warn("  Overriding tuned budgets. Blank keeps the tested default for that role.")
    for role, tokens, _note in _TESTED_ROLE_BUDGETS:
        chosen = _prompt_role_budget(role, tokens)
        if chosen is None:
            continue
        if role == "leader":
            for a in state.get("triad_agents", []):
                if a.get("tier") == "leader":
                    a["context_budget"] = chosen
        elif role == "qc":
            for a in state.get("triad_agents", []):
                if a.get("tier") == "qc":
                    a["context_budget"] = chosen
        elif role == "producer":
            for a in state.get("worker_agents", []):
                a["context_budget"] = chosen


def run(state: dict) -> Any:
    """Execute team formation. Skills-first (#143, Lovecraft review): the
    PRIMARY act is choosing what the team can do — provision the producers
    (skill-holders) FIRST, then bind the two structural roles (Leader + QC).
    Mutates state with ``worker_agents`` (skill-holders) and ``triad_agents``
    (the Leader+QC pair; key kept for finalize back-compat)."""
    default_models = state.get("default_models", {})

    result = _provision_workers(state, default_models)
    if result in (steps.BACK, steps.QUIT):
        return result

    result = _provision_triad(state, default_models)
    if result in (steps.BACK, steps.QUIT):
        return result

    total = len(state["triad_agents"]) + len(state["worker_agents"])
    if total < MIN_AGENTS:
        theme.error(
            f"A team needs at least {MIN_AGENTS} members (Leader + QC + one "
            f"skill-holder). Got {total}. Restarting team formation."
        )
        return steps.BACK

    # Tuned per-role context budgets — discouraged opt-in to override.
    _maybe_customize_context_budgets(state)
    return "provisioned"


__all__ = ["run", "MIN_AGENTS", "MAX_AGENTS"]
