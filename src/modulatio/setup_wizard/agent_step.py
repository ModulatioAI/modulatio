# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Team-formation step — skills-first (#143).

The user first chooses WHAT THE TEAM SHOULD BE ABLE TO DO (skills) and a
model to power them; skills sharing a model collapse into one producer
(skill-holder). Then the two structural roles are bound:
- Producers (skill-holders) come first — the primary act of forming a team.
- Structural roles: Leader (plans + decides) and QC (verifier). A prior
  standalone planner role was removed engine-side; planning is now the
  Leader's job.
- Floor: the Leader is the one REQUIRED role; QC and producers are optional —
  recommended for a /kickoff swarm, skippable for a solo-Leader setup. Soft cap 10.
"""

from __future__ import annotations

from typing import Any

from modulatio import model_presets, templates as templates_pkg, theme
from modulatio.setup_wizard import steps


MIN_AGENTS = 1  # the Leader is the one required role; QC + producers optional (#13)
MAX_AGENTS = 10

# Position of this (agents) step in the wizard's step machine, for the
# step header. The driver (steps.run_step_machine) renders the header from
# the live (step_idx+1, total); these sub-screens clear+re-render their own
# header, so they must match. Kept here (not threaded through run()) to avoid
# a circular import on setup_wizard.__init__, which imports this module — see
# step_order in setup_wizard.__init__._run_setup_body (agents is the 5th of 9).
_STEP_NUMBER = 5
_STEP_TOTAL = 9


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

    role_default = current or default_models.get(role) or default_models.get("producer") or default_models.get("specialist")
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


def _producer_caps_for_model(model: str) -> tuple[str | None, str | None, list[str]]:
    """Resolve a model's (model_tier, cost_class, capability_tags) and let the
    user confirm or override — the "quick tag" (skill-library Brick 3). An
    explicit tag already on the preset wins; otherwise a default is inferred
    from the model id (modulatio.model_capabilities). Confirming is one
    keystroke; declining lets the user type the strengths."""
    from modulatio import model_capabilities, model_presets

    preset = model_presets.get_preset(model) or {}
    tier = preset.get("model_tier")
    cost = preset.get("cost_class")
    caps = preset.get("capability_tags")
    if not (tier or cost or caps):
        tier, cost, caps = (
            model_capabilities.infer_for_preset(preset)
            if preset else model_capabilities.infer(model)
        )
    caps = list(caps or ())

    shown = ", ".join(caps) if caps else "(none)"
    print(theme.color(
        f"  Detected: tier={tier or '?'}, cost={cost or '?'}, strengths=[{shown}]",
        "muted",
    ))
    if steps.confirm_yn("  Use these capabilities for this model?", default=True):
        return tier, cost, caps
    # Override: let the user name the strengths (free text; the known vocab is
    # offered as a hint). Tier/cost keep the detected values.
    print(theme.color(
        f"  Known strengths: {', '.join(model_capabilities.CAPABILITY_TAGS)}",
        "muted",
    ))
    raw = steps.prompt_nav(
        "Capabilities (comma-separated)", default=shown if caps else "",
        required=False,
    )
    if raw in (steps.BACK, steps.QUIT):
        return tier, cost, caps
    edited = [c.strip() for c in str(raw or "").split(",") if c.strip()]
    return tier, cost, (edited or caps)


def _build_producer(model: str, *, index: int | None = None) -> dict:
    """Materialize a producer wizard-state dict — a pure MODEL endpoint
    (skill-library Brick 3). It holds NO skills: it checks out whatever a task
    needs from the shared library at run-time, so any task can route to it. Its
    capabilities come from the model (confirmed via the quick tag)."""
    tier, cost, caps = _producer_caps_for_model(model)
    pid = "producer" if index is None else f"producer_{index}"
    name = "Producer" if index is None else f"Producer {index}"
    return {
        "id": pid,
        "name": name,
        "role": "Producer",
        "identity": (
            "You are a producer on this team — a model endpoint. You check out "
            "whatever skills a task needs from the shared library; any task can "
            "be routed to you."
        ),
        "skills": [],  # no held skills — checked out from the library per task
        "capability_tags": list(caps),
        "model_tier": tier,
        "cost_class": cost,
        "tier": "producer",
        "model": model,
        "template_origin": "producer-endpoint",
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

    # Walk the roles by index so BACK steps back ONE role rather than
    # bubbling out of the whole agents step (which would discard a
    # just-chosen Leader). BACK on the first role (Leader) bubbles up to the
    # previous wizard step; QUIT always bubbles up. Prior picks are preserved
    # via ``by_tier`` so stepping back re-seeds the picker's default.
    tiers = ("leader", "qc")
    i = 0
    while i < len(tiers):
        tier = tiers[i]
        # QC is OPTIONAL — the Leader is the one required role (#13). Offer to skip
        # it: recommended for a /kickoff swarm, omittable for a solo-Leader setup.
        # On a reconfigure a saved QC is already in ``by_tier`` (re-confirm it
        # rather than re-asking). The Leader (i=0) is always walked.
        if tier == "qc" and tier not in by_tier:
            theme.clear_screen()
            theme.step_header(_STEP_NUMBER, _STEP_TOTAL, "Structural role — Quality Control (optional)")
            if not steps.confirm_yn(
                "Add a QC verifier? Recommended — QC checks every producer's "
                "output. A solo-Leader setup can skip it and add it later via "
                "`modulatio setup`.", default=True,
            ):
                break  # no QC — the structural roster is Leader-only
        theme.clear_screen()
        label = "Leader (plans + decides)" if tier == "leader" else "Quality Control (verifier)"
        requirement = "required" if tier == "leader" else "optional"
        theme.step_header(_STEP_NUMBER, _STEP_TOTAL, f"Structural role — {label} ({requirement})")

        current_template = by_tier.get(tier, {}).get("template_origin")
        template_id = _pick_template_for_tier(tier, current=current_template)
        if template_id is steps.QUIT:
            return steps.QUIT
        if template_id is steps.BACK:
            if i == 0:
                return steps.BACK
            i -= 1
            continue

        current_model = by_tier.get(tier, {}).get("model")
        model = _pick_model(tier, default_models, staged_keys=state.get("staged_api_keys"), current=current_model)
        if model is steps.QUIT:
            return steps.QUIT
        if model is steps.BACK:
            if i == 0:
                # re-sweep (:237): BACK at the FIRST role's (Leader) model
                # picker re-shows the Leader's TEMPLATE picker rather than
                # bubbling out of the whole agents step — bubbling would discard
                # the just-picked Leader template (by_tier[tier] is set only
                # AFTER the model is chosen, below) AND pop the just-provisioned
                # worker pool. Re-seed the template default from the just-picked
                # template_id so the re-show lands on the user's current choice;
                # the {**...} spread preserves any prior context_budget. (For
                # i>0, BACK steps to the previous role, the tested behavior.)
                by_tier[tier] = {**by_tier.get(tier, {}), "template_origin": template_id}
                continue
            i -= 1
            continue

        built = _build_agent_from_template(template_id, model)
        # Re-invocation edit/keep: carry forward a per-role context_budget the
        # user customized in a prior run (pre-seeded into ``by_tier`` from the
        # saved team template). The rebuild above does not re-derive it, so
        # without this it would be silently dropped on a reconfigure.
        prior_cb = by_tier.get(tier, {}).get("context_budget")
        if prior_cb is not None:
            built["context_budget"] = prior_cb
        by_tier[tier] = built
        i += 1

    state["triad_agents"] = [by_tier["leader"]] + (
        [by_tier["qc"]] if "qc" in by_tier else []
    )
    return "configured"


def _provision_workers(state: dict, default_models: dict[str, str]) -> Any:
    """Producer-pool provisioning (skill-library Brick 3). A producer is just
    a MODEL endpoint — you assign an LLM and confirm its capability tag; there
    are NO skills to pick. Skills are checked out from the shared library per
    task, so any producer can run any task and the dispatcher load-balances
    across the pool. Adds up to ``MAX_AGENTS - 2`` (Leader + QC) producers.
    Sets ``state['worker_agents']``."""
    staged_keys = state.get("staged_api_keys")
    # The Leader is always provisioned; QC is optional (#13). Reserve 2 seats
    # (Leader + a possible QC) as a constant so a saved triad with a missing /
    # extra / unknown-tier entry can't widen ``max_producers`` past MAX_AGENTS.
    max_producers = max(1, MAX_AGENTS - 2)

    # Re-invocation edit/keep: seed the producer picker defaults from the saved
    # worker pool so a reconfigure starts on the current picks instead of an
    # empty re-provision (mirrors how _provision_triad seeds from ``by_tier``).
    prior_workers = [
        a for a in state.get("worker_agents", []) if a.get("model")
    ][:max_producers]
    prior_models = [a.get("model") for a in prior_workers]

    theme.clear_screen()
    theme.step_header(_STEP_NUMBER, _STEP_TOTAL, "Build your team — add producers")
    print(theme.color(
        "  A producer is a model endpoint. Assign an LLM and confirm what it's "
        "good at — skills are drawn from the shared library per task, so any "
        "producer can run any task and work spreads across the pool.", "muted",
    ))
    print()

    # Producers are OPTIONAL — the Leader is the one required role (#13). A
    # solo-Leader / coding setup can skip them; a /kickoff swarm needs at least
    # one (the engine opens a ROSTER_GAP ticket if a kickoff finds no cover). On
    # a reconfigure (saved pool present) skip the gate and re-confirm the pool.
    if not prior_models and not steps.confirm_yn(
        "Add producers now? They run the tasks in a /kickoff swarm. "
        "Skip for a solo-Leader setup — add them later via `modulatio setup`.",
        default=True,
    ):
        state["worker_agents"] = []
        return "configured"

    producers: list[dict] = []
    while len(producers) < max_producers:
        n = len(producers) + 1
        theme.clear_screen()
        theme.step_header(_STEP_NUMBER, _STEP_TOTAL, f"Producer {n} of up to {max_producers}")
        prior = prior_models[n - 1] if n - 1 < len(prior_models) else None
        model = _pick_model(
            f"producer {n}", default_models, staged_keys=staged_keys, current=prior
        )
        if model in (steps.BACK, steps.QUIT):
            # BACK before any producer is added bubbles up; otherwise just
            # stop adding and keep what we have (>=1 enforced below).
            if not producers:
                return model
            if model is steps.QUIT:
                return model
            break
        built = _build_producer(model, index=n)
        # Carry forward a per-producer context_budget customized in a prior run
        # (same index in the saved pool); the rebuild does not re-derive it, so
        # without this a reconfigure would silently drop it.
        prior_cb = (
            prior_workers[n - 1].get("context_budget")
            if n - 1 < len(prior_workers) else None
        )
        if prior_cb is not None:
            built["context_budget"] = prior_cb
        producers.append(built)
        if len(producers) >= max_producers:
            theme.muted(f"  Reached the {MAX_AGENTS}-member team cap.")
            break
        # While prior picks remain to re-confirm (edit/keep re-invocation),
        # keep walking them without the "add another?" gate so a reconfigure
        # presents the WHOLE saved pool rather than stopping after the first.
        if len(producers) < len(prior_models):
            continue
        if not steps.confirm_yn("Add another producer to the pool?", default=False):
            break

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
    theme.step_header(_STEP_NUMBER, _STEP_TOTAL, "Context budgets (tuned — change is discouraged)")
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

    # The Leader is the one required role (#13); QC and producers are optional.
    # Assert the Leader is present so a future provisioner change that drops it
    # is caught here rather than shipping a Leaderless roster.
    triad = state["triad_agents"]
    tiers = {a.get("tier") for a in triad}
    if "leader" not in tiers:
        theme.error(
            "A team needs a Leader (the one required role). "
            "Restarting team formation."
        )
        return steps.BACK

    # Tuned per-role context budgets — discouraged opt-in to override.
    _maybe_customize_context_budgets(state)
    return "provisioned"


__all__ = ["run", "MIN_AGENTS", "MAX_AGENTS"]
