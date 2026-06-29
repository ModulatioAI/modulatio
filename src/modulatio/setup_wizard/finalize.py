# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
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
from urllib.parse import urlparse

from modulatio import config, setup_state, theme, vault
from modulatio.setup_wizard import steps


def _derive_default_models(structural: list[dict], workers: list[dict]) -> dict[str, str]:
    """Map agent picks to role-default models for the kickoff CLI's flag
    defaults. Skills-first (#143): the structural roles are the Leader (always)
    and an optional QC — this maps whichever are present.
    The task-planning utility call (``planner``) uses the Leader's model —
    planning is the Leader's job now that the Coordinator role is gone.
    ``producer`` falls to the first worker. (Research is a capability a
    producer composes — its sourcing/web-search skills — not a separate role,
    so no ``researcher`` default is written; Brick A.)
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
            out["producer"] = first_worker_model
    return out


def _provider_from_base_url(base_url: str) -> str | None:
    """Derive a short, human provider name from a model preset's endpoint.

    ``api.openai.com`` → ``openai``; ``generativelanguage.googleapis.com`` →
    ``googleapis``; a loopback host (``127.0.0.1``/``localhost``) → ``local``.
    Skips ``api``/``www`` prefixes. Returns ``None`` for an unparseable URL so
    the caller drops it. Provider-agnostic — nothing is hardcoded to a vendor.
    """
    try:
        host = (urlparse(base_url).hostname or "").lower()
    except Exception:
        return None
    if not host:
        return None
    if host in ("127.0.0.1", "::1") or host == "localhost" or host.endswith(".localhost"):
        return "local"
    parts = [p for p in host.split(".") if p not in ("api", "www")]
    if not parts:
        return None
    # Drop a trailing public-suffix-ish segment (com/ai/io/xyz/net/org) so
    # 'api.x.ai' → 'x', 'openrouter.ai' → 'openrouter'.
    if len(parts) > 1 and parts[-1] in ("com", "ai", "io", "xyz", "net", "org", "dev", "co"):
        parts = parts[:-1]
    name = parts[-1] if len(parts) == 1 else parts[0]
    return name or None


def _derive_providers(state: dict) -> list[str]:
    """Provider names for the summary line. The provider step never writes a
    ``configured_providers`` key (models are self-contained endpoint+auth+id
    entries), so the prior read of that phantom key always rendered ``(none)``.

    Two sources, unioned: (1) ``staged_api_keys`` keyed by env var
    (``OPENAI_API_KEY`` → ``openai``; strip the ``_API_KEY`` suffix and
    lowercase), and (2) the configured model presets' endpoints — so an
    OAuth-only or local-only setup (which stages NO api keys) is still
    represented by the provider/endpoint behind each model. Provider-agnostic:
    nothing is hardcoded — whatever the user configured appears. Returns a
    sorted, de-duplicated list.
    """
    providers: set[str] = set()
    for env_var in state.get("staged_api_keys", {}):
        name = str(env_var).strip()
        # re-sweep: the neutral 'API_KEY' sentinel (the malformed-base-url
        # fallback) carries no provider — it's 7 chars so it doesn't end with
        # '_API_KEY' (8) and would otherwise render a bogus 'api_key' provider
        # on the confirm line. Skip it.
        if name.upper() == "API_KEY":
            continue
        if name.upper().endswith("_API_KEY"):
            name = name[: -len("_API_KEY")]
        name = name.strip("_").lower()
        if name:
            providers.add(name)
    # Endpoints behind the configured models — covers OAuth/local presets that
    # stage no api key. Only the presets the user actually picked this run
    # (``configured_models``, a list of preset keys) are represented; an
    # empty/absent list contributes nothing. Best-effort: a presets-load
    # failure must not break the summary, so it degrades to staged-keys-only.
    configured = set(state.get("configured_models") or ())
    if configured:
        try:
            from modulatio import model_presets

            presets = model_presets.load_presets()
        except Exception:
            presets = {}
        for key in configured:
            preset = presets.get(key)
            if not isinstance(preset, dict):
                continue
            name = _provider_from_base_url(str(preset.get("base_url", "")))
            if name:
                providers.add(name)
    return sorted(providers)


def _producer_label(agent: dict) -> str:
    """Short summary identity for a producer in the confirm line. Producers are
    model endpoints (no held skills), so prefer the model id, then capability
    tags, then the user-given name — never a bare ``?``.
    """
    model = str(agent.get("model") or "").strip()
    if model:
        return model
    caps = agent.get("capability_tags") or []
    if caps:
        return ", ".join(str(c) for c in caps)
    name = str(agent.get("name") or "").strip()
    return name or "?"


def confirm(state: dict) -> Any:
    """Show summary + ask for final confirmation. Returns True/BACK/QUIT."""
    print()
    print(theme.color("  Review your setup:", "primary", bold=True))
    print()
    print(f"    Vault root:    {theme.color(state.get('vault_root', '?'), 'accent')}")
    print(f"    Shared res:    {theme.color(state.get('shared_resources_path', '?'), 'accent')}")
    print(f"    Pandoc:        {'✓ installed' if state.get('pandoc_installed') else '✗ skipped'}")
    print(f"    Providers:     {', '.join(_derive_providers(state)) or '(none)'}")
    print(f"    Models:        {len(state.get('configured_models', []))} curated")
    print(f"    API keys:      {len(state.get('staged_api_keys', {}))} staged")
    # ``triad_agents`` holds the structural roles — the Leader (always) and an
    # optional QC (skills-first; a prior standalone planner role has been removed).
    structural = state.get("triad_agents", [])
    workers = state.get("worker_agents", [])
    derived = _derive_default_models(structural, workers)
    if derived:
        print("    Default models (derived from the team):")
        for role, model in derived.items():
            print(f"      {role:12s}  {theme.color(model, 'accent')}")
    print(f"    Structural:    {len(structural)} ({', '.join(a.get('tier', '?') for a in structural) or 'none'})")
    # Producers are model endpoints (skills are checked out from the shared
    # library per task — Agent.skills is always empty here), so identify each
    # by its model, falling back to capability tags or name. The old line read
    # ``a.get('skills', [])`` and rendered ``?, ?, ?`` for every producer.
    print(f"    Producers:     {len(workers)} — {', '.join(_producer_label(a) for a in workers) or '(none)'}")
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
            for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
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


__all__ = ["confirm", "commit", "_derive_providers", "_provider_from_base_url", "_producer_label"]
