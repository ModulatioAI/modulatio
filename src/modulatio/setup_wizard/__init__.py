# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Modulatio setup wizard package — first-run + re-invocation flow.

Entry point: ``run_setup()``. Called by ``modulatio setup`` CLI subcommand
and the TUI's first-launch detection / ``/setup`` slash-command.

Wizard step order (8 steps; step 5 added 2026-04-30 for budget caps):

    1. pandoc check + cross-OS install panel + auto-install
       (also prints the welcome / re-config banner — no separate intro)
    2. vault path (Obsidian-detection + neutral fallback)
    3. models — combined endpoint + auth + model-id step. Quick-add rows
       for detected OAuth credentials + local services. Each entry is
       fully self-contained.
    4. agent provisioning (mandatory triad + workers loop, 4-10 agents).
       Each agent gets one model prompt; per-role default-models dict is
       derived from these picks at finalize, not asked separately.
    5. budget defaults (optional) — y/N gate, then three numeric prompts
       for wall-clock / token / cost caps. Each axis independently None
       (unbounded) or set. New plans inherit at draft time.
    6. first project capture (code + objective for the auto-launch handoff)
    7. confirm + finalize (writes defaults.json + model_presets.json +
       team_template.json + .env)
    8. embedded LLM prefetch (silent if cached, default-yes if missing)

After successful completion, the wizard initializes the captured first
project's vault + seeds the roster from the team template, then launches
the TUI on it. The user lands in Modulatio, not a shell prompt.

Re-invocation: pre-populates ``state`` from existing defaults.json +
setup-state.json + providers.json + model_presets.json + team_template.json.
Each step starts on the current value with edit/keep semantics.

Per locked design: agnostic harness — no built-in providers, no built-in
models. Step 3 + step 4 floor at ≥1 entry each.
"""

from __future__ import annotations

from typing import Any

# readline gives every input() prompt arrow-key cursor movement, Delete,
# Home/End, Ctrl-A/E, and command history. Without it, Python's input()
# is backspace-only — typos in the middle of a long URL / model id
# require deleting back to the typo. Wrapped in try/except for Windows
# (readline is Unix-only; pyreadline3 is the Windows alternative).
try:
    import readline  # noqa: F401  # side effect: enables line editing
except ImportError:
    pass

from modulatio import config, theme
from modulatio.setup_wizard import (
    agent_step,
    budget_step,
    clipboard_step,
    embedded_llm_step,
    finalize,
    first_project_step,
    pandoc_step,
    provider_step,
    steps,
    vault_path_step,
)

# Bump this when the wizard layout/output schema changes meaningfully —
# allows future re-invocation logic to detect "you ran wizard with old
# Modulatio version, suggest re-running."
WIZARD_VERSION = "2.0.0"


_STEP_TITLES = {
    "pandoc": "1. Check pandoc",
    "clipboard": "1a. Check clipboard backend",
    "vault_path": "2. Vault paths",
    "models": "3. Configure models",
    "agents": "4. Provision agents (triad + workers)",
    "budget": "5. Budget defaults (optional)",
    "first_project": "6. Your first project",
    "confirm": "7. Review and finalize",
    "embedded_llm": "8. Prefetch embedded LLM",
}


def _dispatch(step_name: str, state: dict) -> Any:
    if step_name == "pandoc":
        return pandoc_step.run(state)
    if step_name == "clipboard":
        return clipboard_step.run(state)
    if step_name == "vault_path":
        return vault_path_step.run(state)
    if step_name == "models":
        return provider_step.run(state)  # combined-models step lives in provider_step.py
    if step_name == "agents":
        return agent_step.run(state)
    if step_name == "budget":
        return budget_step.run(state)
    if step_name == "first_project":
        return first_project_step.run(state)
    if step_name == "confirm":
        result = finalize.confirm(state)
        if result is True:
            return "confirmed"
        return result  # BACK or QUIT
    if step_name == "embedded_llm":
        return embedded_llm_step.run(state)
    raise ValueError(f"Unknown wizard step: {step_name}")


def _pop_state(step_name: str, state: dict) -> None:
    """Remove answers associated with a given step from state on BACK.

    Note: provider/model removals on BACK do NOT delete the providers.json /
    model_presets.json files themselves — those are persistent across
    wizard re-invocations. Only the in-memory wizard state is cleared.
    """
    keys_per_step = {
        "pandoc": ["pandoc_installed", "pandoc_skipped"],
        "clipboard": ["clipboard_backend_installed", "clipboard_skipped"],
        "vault_path": ["vault_root", "shared_resources_path"],
        "models": ["staged_api_keys", "configured_models"],
        "agents": ["triad_agents", "worker_agents"],
        "budget": ["budget_caps"],
        "first_project": ["first_project_code", "first_project_objective"],
        "confirm": [],
        "embedded_llm": ["embedded_llm_cached", "embedded_llm_skipped"],
    }
    for key in keys_per_step.get(step_name, []):
        state.pop(key, None)


def _load_existing_state() -> dict:
    """Pre-populate wizard state from existing config (re-invocation path)."""
    state: dict[str, Any] = {}

    defaults = config._load_defaults()
    if defaults.get("vault_root"):
        state["vault_root"] = defaults["vault_root"]
    if defaults.get("shared_resources_path"):
        state["shared_resources_path"] = defaults["shared_resources_path"]
    if defaults.get("default_models"):
        # Pre-populated for legacy (pre-team-template) re-invocations only.
        # New wizard runs derive default_models at finalize from agent picks
        # and ignore this key during the wizard itself.
        state["default_models"] = dict(defaults["default_models"])
    # Re-invocation pre-fill for the budget step. The step's y/N gate
    # defaults to 'y' when any cap is already set, so the user can edit
    # prior values without retyping; defaults to 'n' when none are set.
    pre_caps = config.get_default_budget_caps()
    if any(v is not None for v in pre_caps.values()):
        state["budget_caps"] = pre_caps

    # Re-invocation pre-fill for the agents step (step 4). Split the saved
    # team template into the structural pair (Leader + QC) and the producer
    # pool by ``tier``, so the agent step starts on the current picks with
    # edit/keep semantics instead of an empty re-provision. Tier-based (not
    # positional) so it stays robust to template ordering. Unknown tiers are
    # dropped from the pre-fill rather than misclassified.
    team = config.load_team_template()
    if team:
        triad = [a for a in team if a.get("tier") in ("leader", "qc")]
        workers = [a for a in team if a.get("tier") == "producer"]
        if triad:
            state["triad_agents"] = triad
        if workers:
            state["worker_agents"] = workers

    return state


def _auto_launch_tui(state: dict) -> None:
    """After finalize, init the captured first project + seed its roster
    + launch the TUI on it. Replaces the wizard's exit-to-shell behavior
    so first-run users land on a usable screen.

    Falls through silently on import errors so the wizard's main return
    value still reflects setup success.
    """
    code = state.get("first_project_code")
    if not code:
        return

    objective = state.get("first_project_objective", "")
    from modulatio import roster, vault

    vault.init_project(code, code, objective, exist_ok=True)
    defaults_models = config.get_default_models()
    roster.seed_default_roster(
        code,
        leader_model=defaults_models.get("leader"),
        # accepted-but-unused (no coordinator agent); planner-with-legacy
        # fallback so a future reader doesn't suspect a stale-key leak.
        coordinator_model=defaults_models.get("planner") or defaults_models.get("coordinator"),
        producer_model=defaults_models.get("producer") or defaults_models.get("specialist"),
        qc_model=defaults_models.get("qc"),
    )
    theme.success(f"Initialized project '{code}' at {vault.project_dir(code)}")
    print()
    theme.info("Launching Modulatio TUI...")
    print()

    from modulatio.tui.app import ModulatioApp
    ModulatioApp(project_code=code, stub=False).run()


def run_setup() -> bool:
    """Drive the wizard. Returns True on successful completion, False on quit/abort.

    Safe to re-invoke — existing state pre-populates each step.
    """
    theme.enter_dark_screen()
    try:
        return _run_setup_body()
    finally:
        theme.exit_dark_screen()


def _presets_snapshot() -> dict:
    """On-disk model_presets.json contents, or {} if unreadable/absent.

    The models step persists model_presets.json *immediately* (add_preset /
    remove_preset write through), before finalize. So an abort in a later
    step still leaves any presets added during this run on disk. Snapshotting
    at start lets the abort message tell the truth about what survived rather
    than blanket-claiming "No changes written." Never raises — a missing or
    malformed presets file is treated as an empty snapshot.
    """
    from modulatio import model_presets

    try:
        return dict(model_presets.load_presets())
    except Exception:
        return {}


def _run_setup_body() -> bool:
    state = _load_existing_state()
    presets_at_start = _presets_snapshot()

    step_order = [
        "pandoc",
        "clipboard",
        "vault_path",
        "models",
        "agents",
        "budget",
        "first_project",
        "confirm",
        "embedded_llm",
    ]

    try:
        steps.run_step_machine(
            state,
            step_order,
            _dispatch,
            titles=_STEP_TITLES,
            pop_state=_pop_state,
        )
    except steps.WizardAborted:
        # The models step writes model_presets.json through immediately, so an
        # abort here may still leave presets on disk. Only claim "No changes
        # written" when the on-disk presets are unchanged from wizard start;
        # otherwise be honest that the configured models persist (intended —
        # presets survive re-invocation by design).
        if _presets_snapshot() != presets_at_start:
            theme.muted(
                "Setup aborted. Configured models were saved and remain available; "
                "no other settings were written."
            )
        else:
            theme.muted("Setup aborted. No changes written.")
        return False

    finalize.commit(state, version=WIZARD_VERSION)

    print()
    _auto_launch_tui(state)
    return True


__all__ = ["run_setup", "WIZARD_VERSION"]
