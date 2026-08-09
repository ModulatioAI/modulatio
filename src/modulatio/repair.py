# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Repair a broken Modulatio install — reset bad settings to defaults, remove
broken presets/agents, recreate a missing vault/project, clear configs, and stop
the daemon so a clean relaunch picks up the repairs.

This module is glue: every fix reuses an existing primitive (``config``,
``model_presets``, ``setup_state``, ``vault``, ``backup``, and the uninstaller's
``Target``/``backup_plan``/``remove_target`` for the tiered config clear). The
pure functions are testable; ``run_repair`` is the thin interactive shell shared
by ``modulatio repair`` and the Install/Repair fork in ``modulatio setup``.
"""
from __future__ import annotations

import time
from pathlib import Path

from modulatio import config


# ── Config-file categories (the granular clear targets) ─────────────────────

def _settings_files() -> list[Path]:
    """Plain settings — reset without touching agents/secrets/projects."""
    from modulatio import model_presets, preferences, setup_state

    return [
        config.DEFAULTS_FILE,
        model_presets.PRESETS_FILE,
        preferences.PREFS_FILE,
        setup_state.SETUP_STATE_FILE,
        config.AUTH_ALERTS_FILE,
    ]


def _agent_files() -> list[Path]:
    return [config.TEAM_TEMPLATE_FILE]


def _secret_files() -> list[Path]:
    """Every file holding a credential, plus the labels and pins that describe
    them. The inventory comes from one place so repair, backup and uninstall
    cannot each hold a different idea of what a credential is — the way that
    drifts is that something keeps a secret the operator believed was gone."""
    from modulatio import provider_keys, telegram_notify

    return [*config.credential_files(), telegram_notify.CONFIG_FILE,
            provider_keys.LABELS_FILE, provider_keys.PINS_FILE]


# ── Diagnosis (same accessors as `modulatio doctor`) ────────────────────────

def diagnose() -> list[str]:
    """A compact health read — the breakages a repair would fix. Empty list
    means nothing obvious is wrong."""
    from modulatio import model_presets, vault as _vault

    out: list[str] = []
    presets = model_presets.load_presets()
    broken = sorted(k for k in presets if not model_presets.is_available(k))
    if broken:
        out.append(f"{len(broken)} model preset(s) not ready: {', '.join(broken)}")

    vr = config.get_vault_root()
    if not vr.is_dir():
        out.append(f"vault_root is missing: {vr}")

    code = config.get_default_project_code()
    if code:
        _vault.reload()
        try:
            pd = _vault.project_dir(code)
        except Exception:  # noqa: BLE001 — any resolution failure = treat as missing
            pd = vr / code
        if not pd.is_dir():
            out.append(f"default project '{code}' folder is missing: {pd}")
    return out


# ── Individual fixes (pure — no prompting) ──────────────────────────────────

def reset_settings_to_defaults() -> str:
    """Remove defaults.json + preferences.json so the package fallbacks apply.
    Leaves presets, agents, and the vault alone."""
    from modulatio import preferences

    removed = []
    for p in (config.DEFAULTS_FILE, preferences.PREFS_FILE):
        if p.exists():
            p.unlink()
            removed.append(p.name)
    config.reload()
    return f"reset to defaults ({', '.join(removed) or 'nothing to reset'})"


def remove_broken_presets() -> list[str]:
    """Drop every model preset that isn't ready (bad/removed auth, missing key).
    Returns the removed keys."""
    from modulatio import model_presets

    presets = model_presets.load_presets()
    broken = sorted(k for k in presets if not model_presets.is_available(k))
    for k in broken:
        model_presets.remove_preset(k)
    return broken


def reset_agents() -> bool:
    """Remove the agent team template so setup re-provisions it. Returns True if
    a template was present."""
    if config.TEAM_TEMPLATE_FILE.exists():
        config.TEAM_TEMPLATE_FILE.unlink()
        return True
    return False


def repair_vault_and_project() -> list[str]:
    """Recreate a missing vault_root dir and/or a missing default-project
    skeleton. Returns what was repaired."""
    from modulatio import vault as _vault

    out: list[str] = []
    vr = config.get_vault_root()
    if not vr.exists():
        vr.mkdir(parents=True, exist_ok=True)
        out.append(f"created vault_root: {vr}")
    code = config.get_default_project_code()
    if code:
        _vault.reload()
        try:
            pd = _vault.project_dir(code)
        except Exception:  # noqa: BLE001
            pd = vr / code
        if not pd.is_dir():
            _vault.init_project(code, code, "")
            out.append(f"recreated default project: {code}")
    return out


def stop_daemon() -> str:
    """Stop the daemon so a clean relaunch picks up the repairs (reuses the
    uninstaller's daemon-stop)."""
    from modulatio import uninstall

    return uninstall.stop_daemon()


# ── Clear configs — tiered factory reset ────────────────────────────────────

def clear_plan(*, agents: bool = False, secrets: bool = False,
               projects: bool = False, wipe_all: bool = False) -> list:
    """Build the list of ``uninstall.Target`` to remove for a config clear.

    Base = the plain settings files. The sensitive categories — agents, secrets,
    project folder — are each gated; ``wipe_all`` enables all three. Sensitive
    targets are flagged ``user_data`` so the caller backs them up first.
    """
    from modulatio import uninstall

    if wipe_all:
        agents = secrets = projects = True

    targets = [uninstall.Target(f"setting ({p.name})", p, "settings")
               for p in _settings_files() if p.exists()]
    if agents:
        targets += [uninstall.Target(f"agent config ({p.name})", p, "settings",
                                     user_data=True)
                    for p in _agent_files() if p.exists()]
    if secrets:
        targets += [uninstall.Target(f"secret config ({p.name})", p, "settings",
                                     user_data=True)
                    for p in _secret_files() if p.exists()]
    if projects:
        # Only clear the vault when Modulatio OWNS it (same guard as the
        # uninstaller's build_plan): an unowned custom folder is the user's own
        # notes (e.g. their Obsidian vault) and must never be auto-deleted, even
        # when they opt into clearing project folders.
        vr = config.get_vault_root()
        if vr.exists() and uninstall.vault_is_modulatio_owned():
            targets.append(uninstall.Target("project folder (vault)", vr,
                                            "projects", user_data=True))
    # Validate the hand-built plan through the SAME assert_safe gate build_plan
    # uses, so every Target returned is catastrophic-path-safe at plan time —
    # not only re-checked at delete time.
    return uninstall.validated_plan(targets)


def execute_clear(plan: list) -> tuple[Path | None, list[str]]:
    """Back up the user-data targets in ``plan``, remove every target, and reset
    the setup-completed marker so the wizard re-fires. Returns
    ``(backup_path_or_None, removed_labels)``."""
    from modulatio import setup_state, uninstall

    backup = None
    if any(t.user_data for t in plan):
        dest = Path.home() / f"modulatio-clear-backup-{time.strftime('%Y%m%d-%H%M%S')}.tar.gz"
        backup = uninstall.backup_plan(plan, dest)

    removed = []
    for t in plan:
        ok, _ = uninstall.remove_target(t)
        if ok:
            removed.append(t.label)
    setup_state.reset()
    config.reload()
    return backup, removed


# ── Interactive shell (shared by `modulatio repair` + the setup fork) ───────

def _ask(prompt: str, default: bool = False) -> bool:
    try:
        a = input(prompt + (" [Y/n] " if default else " [y/N] ")).strip().lower()
    except EOFError:
        return default
    return default if not a else a.startswith("y")


def run_repair() -> None:
    """Interactive repair menu. Plain stdin/stdout so it works anywhere and is
    callable from both the CLI command and the wizard fork without a cycle."""
    print("\n=== Modulatio repair ===")
    problems = diagnose()
    if problems:
        print("Detected:")
        for p in problems:
            print(f"  ! {p}")
    else:
        print("No obvious problems detected — you can still reset things below.")

    actions = [
        ("Reset settings to defaults (keeps presets, agents, vault)",
         _do_reset_settings),
        ("Remove broken model presets", _do_remove_broken),
        ("Reset the agent team", _do_reset_agents),
        ("Repair a missing vault / default project", _do_repair_vault),
        ("Clear configs (factory reset — choose what to wipe)", _do_clear_configs),
        ("Stop the daemon (so a clean relaunch picks up repairs)", _do_stop_daemon),
    ]
    while True:
        print("\nRepair actions:")
        for i, (label, _) in enumerate(actions, 1):
            print(f"  {i}) {label}")
        print("  q) Done")
        try:
            choice = input("Choose: ").strip().lower()
        except EOFError:
            return
        if choice in ("q", "", "quit", "done"):
            return
        if choice.isdigit() and 1 <= int(choice) <= len(actions):
            actions[int(choice) - 1][1]()
        else:
            print("  (unknown choice)")


def _do_reset_settings() -> None:
    if _ask("Reset settings to defaults (defaults.json + theme)?"):
        print("  " + reset_settings_to_defaults())


def _do_remove_broken() -> None:
    removed = remove_broken_presets()
    print(f"  removed {len(removed)} broken preset(s)"
          + (f": {', '.join(removed)}" if removed else ""))


def _do_reset_agents() -> None:
    if _ask("Remove the agent team template (setup will re-provision)?"):
        print("  agent team reset" if reset_agents() else "  no agent template present")


def _do_repair_vault() -> None:
    fixed = repair_vault_and_project()
    print("  " + ("; ".join(fixed) if fixed else "vault + default project look fine"))


def _do_stop_daemon() -> None:
    print("  " + stop_daemon())


def _do_clear_configs() -> None:
    print("\nClear configs — plain settings are always cleared. Choose what else:")
    agents = _ask("  Also clear AGENT configs (team template)?")
    secrets = _ask("  Also clear SECRETS (telegram token + key labels/pins)?")
    projects = _ask("  Also clear PROJECT FOLDERS (the vault + your work)?")
    wipe_all = False
    if not (agents and secrets and projects):
        wipe_all = _ask("  ...or WIPE IT ALL (everything above)?")
    plan = clear_plan(agents=agents, secrets=secrets, projects=projects,
                      wipe_all=wipe_all)
    if not plan:
        print("  nothing to clear.")
        return
    print("  Will clear:")
    for t in plan:
        print(f"    - {t.label}: {t.path}" + ("  (backed up)" if t.user_data else ""))
    if not _ask("  Proceed?"):
        print("  cancelled.")
        return
    backup, removed = execute_clear(plan)
    print(f"  cleared {len(removed)} item(s)." + (f" Backup: {backup}" if backup else ""))
    print("  setup-completed marker reset — the wizard runs on next launch.")
