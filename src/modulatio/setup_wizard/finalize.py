"""Finalize step — commits all wizard state to disk.

Writes (in order):
  1. ``~/.config/modulatio/defaults.json`` — paths + default models
  2. ``<vault>/.env`` — staged API keys (chmod 600)
  3. (deferred to slice 4 onward) Roster files for triad + workers
  4. ``~/.config/modulatio/setup-state.json`` — mark wizard complete

This is the ONLY place filesystem side effects happen — earlier steps
stage in ``state``. Backing out of confirm leaves no orphan files.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from modulatio import config, setup_state, theme, vault
from modulatio.setup_wizard import steps


def _derive_default_models(structural: list[dict], workers: list[dict]) -> dict[str, str]:
    """Map agent picks to role-default models for the kickoff CLI's flag
    defaults. Skills-first (#143): the structural roles are Leader + QC.
    The task-planning utility call (``planner``) uses the Leader's model —
    planning is the Leader's job now that the Coordinator role is gone.
    ``specialist`` falls to the first producer; ``researcher`` uses a
    producer holding a research skill if any, else falls back to specialist.
    """
    by_tier = {a.get("tier"): a for a in structural}
    out: dict[str, str] = {}
    for tier in ("leader", "qc"):
        agent = by_tier.get(tier)
        if agent and agent.get("model"):
            out[tier] = agent["model"]
    # The planner runner uses the Leader's model (post-coordinator-collapse).
    leader = by_tier.get("leader")
    if leader and leader.get("model"):
        out["planner"] = leader["model"]
    if workers:
        first_worker_model = workers[0].get("model")
        if first_worker_model:
            out["specialist"] = first_worker_model
        researcher = next(
            (
                w for w in workers
                if w.get("template_origin") == "researcher"
                or "research" in (w.get("skills") or [])
                or "researcher" in (w.get("skills") or [])
            ),
            None,
        )
        if researcher and researcher.get("model"):
            out["researcher"] = researcher["model"]
        elif first_worker_model:
            out["researcher"] = first_worker_model
    return out


def confirm(state: dict) -> Any:
    """Show summary + ask for final confirmation. Returns True/BACK/QUIT."""
    print()
    print(theme.color("  Review your setup:", "primary", bold=True))
    print()
    print(f"    Vault root:    {theme.color(state.get('vault_root', '?'), 'accent')}")
    print(f"    Shared res:    {theme.color(state.get('shared_resources_path', '?'), 'accent')}")
    print(f"    Pandoc:        {'✓ installed' if state.get('pandoc_installed') else '✗ skipped'}")
    print(f"    Providers:     {', '.join(state.get('configured_providers', [])) or '(none)'}")
    print(f"    Models:        {len(state.get('configured_models', []))} curated")
    print(f"    API keys:      {len(state.get('staged_api_keys', {}))} staged")
    # ``triad_agents`` holds the structural roles — Leader + QC only
    # (skills-first; a prior standalone planner role has been removed).
    structural = state.get("triad_agents", [])
    workers = state.get("worker_agents", [])
    derived = _derive_default_models(structural, workers)
    if derived:
        print("    Default models (derived from the team):")
        for role, model in derived.items():
            print(f"      {role:12s}  {theme.color(model, 'accent')}")
    print(f"    Structural:    {len(structural)} (Leader + QC: {', '.join(a.get('tier', '?') for a in structural)})")
    print(f"    Skill-holders: {len(workers)} — {', '.join(', '.join(a.get('skills', [])) or '?' for a in workers)}")
    print(f"    Total team:    {len(structural) + len(workers)}")
    code = state.get("first_project_code")
    if code:
        print(f"    First project: {theme.color(code, 'accent')} — {state.get('first_project_objective', '')[:60]}")
    print()

    print(steps.nav_hint())
    try:
        raw = input(theme.prompt_color("  Save and complete setup? [Y/n/b/q]: ", "highlight")).strip().lower()
    except (EOFError, KeyboardInterrupt):
        return steps.QUIT

    if raw == "b":
        return steps.BACK
    if raw == "q":
        return steps.QUIT
    if raw and raw != "y" and raw[0] != "y":
        theme.muted("Cancelled. Run setup again when you're ready.")
        return steps.QUIT
    return True


def commit(state: dict, *, version: str) -> None:
    """Write everything to disk. Atomic-ish: defaults first, env second,
    setup-state last (so a crash mid-write doesn't lock us out).
    """
    # 1. defaults.json — paths + derived default models + first project code
    triad = state.get("triad_agents", [])
    workers = state.get("worker_agents", [])
    derived_models = _derive_default_models(triad, workers)
    defaults = {
        "vault_root": state["vault_root"],
        "shared_resources_path": state["shared_resources_path"],
        "default_models": derived_models,
    }
    if state.get("first_project_code"):
        # Captured by first_project_step; bare `modulatio` (no args) reads
        # this to know which project to launch the TUI on.
        defaults["default_project_code"] = state["first_project_code"]
    # Budget caps from the optional step. Only persist axes the user
    # actually set — None entries are dropped so the JSON stays clean.
    budget_caps = state.get("budget_caps") or {}
    persisted_caps = {
        k: v for k, v in budget_caps.items() if v is not None
    }
    if persisted_caps:
        defaults["budget_caps"] = persisted_caps
    config.save_defaults(defaults)
    config.reload()       # invalidate config cache
    vault.reload()        # rebind vault.VAULT_ROOT — the auto-launch's
                          # init_project uses it; without this the call
                          # races with the at-import-time fallback path.
    theme.success(f"Wrote {config.DEFAULTS_FILE}")

    # 1b. team_template.json — wizard agent picks. Read by
    # roster.seed_default_roster on every new project so the wizard's
    # team carries forward, not the hardcoded fallback.
    team = list(triad) + list(workers)
    if team:
        config.save_team_template(team)
        theme.success(f"Wrote {len(team)}-agent team template to {config.TEAM_TEMPLATE_FILE}")

    # 2. <vault>/.env — staged API keys
    staged = state.get("staged_api_keys", {})
    if staged:
        vault_root = Path(state["vault_root"])
        vault_root.mkdir(parents=True, exist_ok=True)
        env_path = vault_root / ".env"
        existing: dict[str, str] = {}
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    existing[k.strip()] = v.strip()
        existing.update(staged)
        # write_secret_file: 0600 mode through the open(), no world-readable
        # window between create and chmod. Vault .env can carry every API
        # key the user added in the wizard.
        config.write_secret_file(
            env_path,
            "\n".join(f"{k}={v}" for k, v in existing.items()) + "\n",
        )
        theme.success(f"Wrote {len(staged)} API key(s) to {env_path} (chmod 600)")

    # 3. shared resources directory + per-role agent files (deferred to slice 4
    # for full roster integration; for now ensure the directory tree exists).
    shared = Path(state["shared_resources_path"])
    for sub in ("templates", "skills", "standards", "research"):
        (shared / sub).mkdir(parents=True, exist_ok=True)
    theme.muted(f"Initialized shared resources tree at {shared}")

    # 4. Mark wizard completed
    skipped = []
    if state.get("pandoc_skipped"):
        skipped.append("pandoc")
    if state.get("embedded_llm_skipped"):
        skipped.append("embedded_llm")
    setup_state.mark_completed(version=version, skipped_steps=skipped)
    theme.success(f"Setup completed and recorded at {setup_state.SETUP_STATE_FILE}")


__all__ = ["confirm", "commit"]
