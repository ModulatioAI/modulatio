# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Modulatio setup wizard package — first-run + re-invocation flow.

Entry point: ``run_setup()``. Called by ``modulatio setup`` CLI subcommand
and the TUI's first-launch detection / ``/setup`` slash-command.

Wizard step order (8 steps; step 5 added 2026-04-30 for budget caps):

    1. pandoc check + cross-OS install panel + auto-install
       (also prints the welcome / re-config banner — no separate intro)
       plus clipboard + SVG-renderer + WebOS-install tooling checks (1a/1b/1c)
    2. vault path (Obsidian-detection + neutral fallback)
    3. models — combined endpoint + auth + model-id step. Quick-add rows
       for detected OAuth credentials + local services. Each entry is
       fully self-contained.
    4. agent provisioning (Leader required; QC + producers optional, 1-10
       agents). Each agent gets one model prompt; per-role default-models dict
       is derived from these picks at finalize, not asked separately.
    5. budget defaults (optional) — y/N gate, then three numeric prompts
       for wall-clock / token / cost caps. Each axis independently None
       (unbounded) or set. New plans inherit at draft time.
    6. first project capture (code + objective for the auto-launch handoff)
    7. embedded LLM prefetch (silent if cached, default-yes if missing) —
       runs before confirm so the prefetch (a pure cache warm) can't sit
       between a confirmed save and the commit write
    8. confirm + finalize (writes defaults.json + model_presets.json +
       team_template.json + .env); commit fires immediately after confirm

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
    budget_step,
    clipboard_step,
    embedded_llm_step,
    finalize,
    first_project_step,
    pandoc_step,
    renderer_step,
    steps,
    vault_path_step,
    webos_step,
)

# Bump this when the wizard layout/output schema changes meaningfully —
# allows future re-invocation logic to detect "you ran wizard with old
# Modulatio version, suggest re-running."
WIZARD_VERSION = "2.0.0"


_STEP_TITLES = {
    "pandoc": "1. Check pandoc",
    "clipboard": "1a. Check clipboard backend",
    "renderer": "1b. Check SVG renderer",
    "webos": "1c. Install WebOS (optional)",
    "vault_path": "2. Vault paths",
    # Models + agents are configured in the TUI Config tab now, not here.
    "budget": "3. Budget defaults (optional)",
    "first_project": "4. Your first project",
    # embedded_llm now runs before confirm so the
    # confirmed save commits immediately — labels follow the new order.
    "embedded_llm": "5. Prefetch embedded LLM",
    "confirm": "6. Review and finalize",
}


def _dispatch(step_name: str, state: dict) -> Any:
    if step_name == "pandoc":
        return pandoc_step.run(state)
    if step_name == "clipboard":
        return clipboard_step.run(state)
    if step_name == "renderer":
        return renderer_step.run(state)
    if step_name == "webos":
        return webos_step.run(state)
    if step_name == "vault_path":
        return vault_path_step.run(state)
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
        "renderer": ["svg_renderer_installed", "svg_renderer_skipped"],
        "webos": ["webos_installed", "webos_skipped"],
        "vault_path": ["vault_root", "shared_resources_path"],
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

    # Same init+seed-with-defaults pair as the PROJECTS-tab [New] button —
    # routed through the one helper. exist_ok=True: the wizard is idempotent
    # on a pre-existing first project (the modal path refuses-on-existing).
    roster.create_project(code, objective, exist_ok=True)
    theme.success(f"Initialized project '{code}' at {vault.project_dir(code)}")
    print()
    theme.info("Launching Modulatio TUI...")
    print()

    from modulatio.tui.app import ModulatioApp
    ModulatioApp(project_code=code, stub=False, splash=True).run()


def run_setup() -> bool:
    """Drive the wizard. Returns True on successful completion, False on quit/abort.

    Safe to re-invoke — existing state pre-populates each step. When a previous
    install is detected, opens with an Install-or-Repair choice; Repair routes to
    the shared repair flow instead of the wizard.
    """
    theme.enter_dark_screen()
    try:
        if _existing_config():
            from modulatio.setup_wizard import steps
            choice = steps.pick_option(
                "Existing Modulatio config detected — Install or Repair?",
                [("Install / reconfigure (run the setup wizard)", "install"),
                 ("Repair (fix a broken install)", "repair")],
                default_index=0,
            )
            if choice in (steps.BACK, steps.QUIT):
                return False
            if choice == "repair":
                from modulatio import repair
                repair.run_repair()
                return True
        return _run_setup_body()
    finally:
        theme.exit_dark_screen()


def _existing_config() -> bool:
    """True if Modulatio has been set up before — gate for the Install/Repair
    fork (a truly fresh install skips straight to the wizard)."""
    from modulatio import setup_state
    return config.defaults_exist() or setup_state.setup_completed()


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


def _system_tools_snapshot() -> dict[str, bool]:
    """Whether pandoc / the clipboard backend are present on the system.

    The pandoc + clipboard steps can run ``try_auto_install`` (or have the
    user install manually during the recheck loop), which mutates the system
    via its package manager *before* any wizard configuration is written. That
    is a side effect the abort message must not paper over with a blanket
    "No changes written." Snapshotting installed-ness at wizard start and again
    at abort lets the message tell the truth about a system tool that appeared
    during this run. Never raises — a probe failure is treated as "absent" so
    the abort path can't crash.
    """
    snapshot: dict[str, bool] = {}
    for name, module in (("pandoc", pandoc_step), ("clipboard", clipboard_step),
                         ("renderer", renderer_step), ("webos", webos_step)):
        try:
            snapshot[name] = bool(module.is_installed())
        except Exception:
            snapshot[name] = False
    return snapshot


def _embedded_model_snapshot() -> tuple[str, bool]:
    """The active embedding model id + whether it's already in the fastembed
    cache.

    The embedded_llm prefetch step (now step 7, before
    confirm) calls ``prefetch()``, which downloads the routing
    embedder (potentially hundreds of MB) into fastembed's cache — a durable,
    reusable on-disk side effect. The abort message must not paper that over
    with "No changes written" on a run where the on-disk presets / system tools
    are otherwise unchanged. Snapshotting cached-ness at wizard start and again
    at abort (mirrors ``_system_tools_snapshot``) lets the message tell the
    truth about a cache warm that happened during this run. Never raises — a
    probe failure is treated as "not cached" so the abort path can't crash.
    """
    from modulatio.setup_wizard import embedded_llm_step

    try:
        model_id = config.get_embedding_model()
    except Exception:
        return ("", False)
    try:
        return (model_id, bool(embedded_llm_step.is_cached(model_id)))
    except Exception:
        return (model_id, False)


def _join_tool_names(names: list[str]) -> str:
    """Render a human-readable list of installed system tool names.

    "pandoc" -> "pandoc"; ["pandoc", "clipboard"] -> "pandoc and clipboard".
    """
    labels = {"pandoc": "pandoc", "clipboard": "a clipboard backend",
              "renderer": "an SVG renderer", "webos": "the WebOS"}
    rendered = [labels.get(n, n) for n in names]
    if len(rendered) <= 1:
        return rendered[0] if rendered else ""
    return " and ".join((", ".join(rendered[:-1]), rendered[-1]))


def _run_setup_body() -> bool:
    state = _load_existing_state()
    presets_at_start = _presets_snapshot()
    tools_at_start = _system_tools_snapshot()
    embed_model, embed_cached_at_start = _embedded_model_snapshot()

    # embedded_llm runs BEFORE confirm. The confirm
    # step prompts "Save and complete setup?" and ``commit`` fires the instant
    # the machine finishes — so nothing slow may sit between confirm and the
    # write. The embedded-LLM prefetch is a pure, reusable cache warm with no
    # dependency on commit (it populates fastembed's own cache root), so it is
    # safe to run ahead of confirm. This closes the long unsaved window where a
    # user could answer Y and then have a multi-minute model download
    # interrupted before anything was persisted.
    # Models + agents are NO LONGER configured in the wizard — that lives in the
    # TUI Config tab (it works better, and the roster there is the single source
    # of every seat's model). The wizard just gets the install bootable; the
    # operator configures providers, models, and the team in-app. A console
    # message typed before anything is configured nudges them to the Config tab.
    step_order = [
        "pandoc",
        "clipboard",
        "renderer",
        "webos",
        "vault_path",
        "budget",
        "first_project",
        "embedded_llm",
        "confirm",
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
        #
        # The pandoc / clipboard steps can also install a system package via
        # the OS package manager before this point — a real, durable side
        # effect that survives the abort. Report any system tool that became
        # available during this run so the abort message can't claim "No
        # changes written" after mutating the system.
        tools_at_end = _system_tools_snapshot()
        newly_installed = [
            name
            for name, present in tools_at_end.items()
            if present and not tools_at_start.get(name, False)
        ]
        presets_at_end = _presets_snapshot()
        presets_changed = presets_at_end != presets_at_start
        # remove_preset() writes through immediately too, so an abort after a
        # removal also flips presets_changed. Distinguish a purely-additive
        # delta (only safe to call "saved and remain available") from one that
        # dropped a preexisting preset — comparing key sets tells them apart.
        presets_removed = bool(set(presets_at_start) - set(presets_at_end))

        # The embedded-LLM prefetch can download the
        # routing embedder into fastembed's cache before confirm. If it flipped
        # from not-cached to cached during this run, the abort message must own
        # that durable (but reusable) on-disk side effect rather than claim "No
        # changes written."
        _, embed_cached_at_end = _embedded_model_snapshot()
        embed_newly_cached = embed_cached_at_end and not embed_cached_at_start

        def _preset_clause() -> str:
            # Lowercase-start so it composes mid-sentence; the lead clause is
            # re-capitalized below. Prior wording preserved verbatim.
            if presets_removed:
                return "model changes (including removals) were written to disk"
            return "configured models were saved and remain available"

        # Compose the message from whichever side effects actually occurred so a
        # new dimension doesn't multiply the branch count combinatorially.
        clauses: list[str] = []
        if presets_changed:
            clauses.append(_preset_clause())
        if newly_installed:
            clauses.append(
                f"{_join_tool_names(newly_installed)} was installed on your system"
            )
        if embed_newly_cached:
            model_label = f" ({embed_model})" if embed_model else ""
            clauses.append(
                f"the embedded routing model{model_label} was downloaded to a "
                "reusable cache"
            )

        if not clauses:
            theme.muted("Setup aborted. No changes written.")
        else:
            if len(clauses) > 1:
                body = ", and ".join((", ".join(clauses[:-1]), clauses[-1]))
            else:
                body = clauses[0]
            # Capitalize the lead unless the LEADING clause is a verbatim
            # tool-name match (a tool-install clause leads only when no presets
            # changed) — those stay lowercase so callers/tests can match the
            # name verbatim ("pandoc was installed ..."). The preset clause and
            # an embed-led clause (cache warm with no preset/install ahead of
            # it) are prose, so they get capitalized.
            # An embed-cache-only abort used to read
            # "Setup aborted. the embedded routing model ..." because the lead
            # was only capitalized on presets_changed.
            tool_install_leads = bool(newly_installed) and not presets_changed
            if not tool_install_leads:
                body = f"{body[0].upper()}{body[1:]}"
            # A config/cache side effect ("other settings") vs a system-only
            # install ("no configuration") — mirrors the prior tail wording.
            tail = (
                "no other settings were written"
                if (presets_changed or embed_newly_cached)
                else "no configuration was written"
            )
            theme.muted(f"Setup aborted. {body}; {tail}.")
        return False

    finalize.commit(state, version=WIZARD_VERSION)

    print()
    _auto_launch_tui(state)
    return True


__all__ = ["run_setup", "WIZARD_VERSION"]
