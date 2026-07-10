# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Uninstall planning + execution — the single source of truth for everything
Modulatio writes to disk, so an uninstall can return the system to a clean
state without guessing paths.

Design: pure logic here (web-UI-safe — no terminal coupling, no prompting); the
``modulatio uninstall`` CLI subcommand and the standalone ``uninstall.sh`` script
each consume this independently. Removal is split into:

  * **core** (always removed) — the rebuildable install footprint: the vector
    cache, the embedded-model (fastembed) cache, and the daemon's runtime
    pid/log. No user value, always safe to drop.
  * **optional** (per-user opt-in) — ``settings`` (``~/.config/modulatio``, which
    also holds secret config) and ``projects`` (the vault, which holds the user's
    work + the secret ``.env``). Each is backed up before removal.
  * **pandoc** — detected, removed only on opt-in (it's a general-purpose tool a
    user may want; system-package removal needs sudo).
  * **preserved** — reported, never auto-removed: deliverables, backups, and
    other tools' credential files (``~/.claude`` etc., which Modulatio only reads).

The catastrophic-path guard (``assert_safe``) is the engine binding: a malformed
config (e.g. ``vault_root = "/"``) can never turn an uninstall into ``rm -rf /``.
"""
from __future__ import annotations

import os
import shutil
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path

from modulatio import config


class UnsafeRemovalError(RuntimeError):
    """A path failed the catastrophic-removal guard — never deleted."""


@dataclass(frozen=True)
class Target:
    """One filesystem location the uninstaller knows about.

    ``category``: ``cache`` | ``model`` | ``daemon`` | ``settings`` | ``projects``.
    ``optional``: True if removal is a per-user opt-in (settings / projects).
    ``user_data``: True if it holds user work/secrets → back up before removing.
    """

    label: str
    path: Path
    category: str
    optional: bool = False
    user_data: bool = False


def assert_safe(path: Path) -> None:
    """Raise ``UnsafeRemovalError`` for any path that must never be removed.

    Guards against a malformed config widening a delete to the home dir, a
    filesystem root, or any too-shallow path (fewer than 3 components below the
    root). This is the hard invariant — the engine refuses, regardless of what
    the plan asked for.
    """
    p = path.expanduser()
    try:
        rp = p.resolve()
    except OSError:
        rp = p
    home = Path.home().resolve()
    if rp == Path(rp.anchor):  # a filesystem root, e.g. "/"
        raise UnsafeRemovalError(f"refusing to remove filesystem root: {rp}")
    if rp == home or rp in home.parents:
        raise UnsafeRemovalError(f"refusing to remove home or an ancestor: {rp}")
    if len(rp.parts) <= 2:  # e.g. "/home", "/usr" — too shallow to be ours
        raise UnsafeRemovalError(f"refusing to remove too-shallow path: {rp}")
    # Never remove a source / code checkout (carries .git or a packaging
    # manifest). Defence in depth: even if a path collision (DATA_HOME resolving
    # onto ~/modulatio, or a "modulatio" substring match) feeds a repo dir here,
    # refuse it rather than delete a code tree.
    if rp.is_dir() and any(
        (rp / marker).exists() for marker in (".git", "pyproject.toml", "setup.py")
    ):
        raise UnsafeRemovalError(f"refusing to remove a source/repo checkout: {rp}")


def fastembed_cache_dirs() -> list[Path]:
    """The cache dirs where fastembed may have stored the embedded model.

    Modulatio passes no explicit ``cache_dir`` to fastembed, so the model lands
    in fastembed's default location, which varies by version/env. We target all
    plausible ones. These are fastembed-namespaced (NOT the shared HuggingFace
    hub cache), so removing them never touches other tools' models. Re-downloads
    on next routing use.
    """
    out: list[Path] = []
    seen: set[Path] = set()
    candidates = []
    env = os.environ.get("FASTEMBED_CACHE_PATH", "").strip()
    # Only honor an env override whose basename is EXACTLY a fastembed-owned cache
    # name. A substring (``customer-fastembed-notes``) or a parent component
    # containing "fastembed" must NOT make an arbitrary work dir an always-removed
    # target — ownership is not established by being named in this env var.
    if env and Path(env).name.lower() in {"fastembed", "fastembed_cache"}:
        candidates.append(Path(env).expanduser())
    candidates.append(Path.home() / ".cache" / "fastembed")
    candidates.append(Path(tempfile.gettempdir()) / "fastembed_cache")
    for d in candidates:
        if d not in seen:
            seen.add(d)
            out.append(d)
    return out


def core_targets() -> list[Target]:
    """Always-removed footprint: vector cache, embedded-model cache, daemon log.

    The daemon *process* + its pid file are handled by ``stop_daemon`` (it kills
    then unlinks the pid); the log is a plain file removed here.
    """
    targets = [Target("Vector cache (semantic / qc-history / team-memory)",
                      config.get_cache_root(), "cache")]
    for d in fastembed_cache_dirs():
        targets.append(Target("Embedded-model cache (fastembed)", d, "model"))
    targets.append(Target("Daemon log", config.CONFIG_DIR / "daemon.log", "daemon"))
    return targets


def settings_target() -> Target:
    """The settings directory — config, presets, key labels, telegram + the
    auth/setup state. Holds secret config, so it is user-data (back up first)."""
    return Target("Settings + secret config (~/.config/modulatio)",
                  config.CONFIG_DIR, "settings", optional=True, user_data=True)


def projects_target() -> Target:
    """The vault — the user's projects, plus the secret ``.env`` and the
    heartbeat/cron queues. Always user-data."""
    return Target("Project folder / vault (your work + secrets)",
                  config.get_vault_root(), "projects", optional=True, user_data=True)


def data_home_target() -> Target:
    """The Modulatio data namespace (``$XDG_DATA_HOME/modulatio``) — holds the
    default vault + shared resources. Removed with the projects tier so a
    default-vault wipe doesn't leave an empty data-home behind."""
    return Target("Data home (default vault + shared resources)",
                  config._xdg_data_home() / "modulatio", "projects",
                  optional=True, user_data=True)


def deliverables_target() -> Target:
    """Rendered deliverables (``~/Documents/Modulatio`` by default). Preserved by
    a normal uninstall; removable for a ``--pristine`` reset."""
    from modulatio import delivery

    return Target("Deliverables", delivery.delivery_root(), "deliverables",
                  optional=True, user_data=True)


def vault_is_custom() -> bool:
    """True when the configured vault points somewhere other than the default
    XDG data location — i.e. a user-chosen folder (e.g. an Obsidian vault) that
    likely holds real work, so a pristine wipe should WARN before removing it."""
    default = Path(config._fallback_vault_root()).expanduser()
    try:
        return config.get_vault_root().resolve() != default.resolve()
    except OSError:
        return False


def vault_is_modulatio_owned() -> bool:
    """True if the configured vault is a folder Modulatio created — its path has
    a ``modulatio`` component (the XDG default ``…/modulatio/projects`` and the
    wizard's ``~/Obsidian/Modulatio/projects`` both qualify). A custom vault
    WITHOUT that marker is the user's own folder (their notes); the uninstaller
    must NEVER auto-delete it, even under ``--pristine --yes``.

    FUTURE: this is a CREATOR-NAME heuristic, not a true ownership record — it
    holds because the wizard is the only thing that makes these folders today, so
    the ``modulatio`` component is present iff we created it. It has one sharp
    edge: a user who literally names their own notes folder ``modulatio`` would be
    treated as engine-owned. Before the uninstaller is ever used as the substrate
    for an AUTOMATED reset (CI teardown, a ``--ci`` flag), replace the heuristic
    with an explicit ownership marker the wizard writes at vault-create time (a
    ``.modulatio-owned`` stamp / a setup_state entry) — a real ownership record,
    not a path-name guess. Interactive use is safe today (the operator confirms)."""
    try:
        vault = config.get_vault_root().resolve()
    except OSError:
        return False
    return any(part.lower() == "modulatio" for part in vault.parts)


def preserved_targets() -> list[Target]:
    """Paths the uninstaller reports but NEVER auto-removes: finished
    deliverables, export backups, and other tools' credentials."""
    from modulatio import delivery, preferences

    out = [
        Target("Deliverables", delivery.delivery_root(), "preserve"),
        Target("Export backups", Path(preferences.get_backup_dir()), "preserve"),
    ]
    from modulatio import oauth_helpers

    for label, attr in (
        ("Claude CLI credentials", "ANTHROPIC_CREDENTIALS_FILE"),
        ("Codex CLI credentials", "OPENAI_CODEX_CREDENTIALS_FILE"),
        ("Grok CLI credentials", "XAI_GROK_CREDENTIALS_FILE"),
    ):
        p = getattr(oauth_helpers, attr, None)
        if p is not None:
            out.append(Target(f"{label} (other tool — read-only)", Path(p), "preserve"))
    return out


def build_plan(
    *, remove_settings: bool = False, remove_projects: bool = False,
    remove_deliverables: bool = False,
) -> list[Target]:
    """The list of targets to actually remove, given the per-user opt-ins.

    Always includes the core footprint; adds settings / projects / deliverables
    on request. Only existing paths that pass ``assert_safe`` are returned — a
    target that fails the guard is dropped (never silently widened).
    """
    candidates = list(core_targets())
    if remove_settings:
        candidates.append(settings_target())
    if remove_projects:
        data_home = data_home_target()
        candidates.append(data_home)  # Modulatio's namespace — always ours
        # The configured vault is added only when Modulatio OWNS it. An unowned
        # custom folder (the user's own notes) is never auto-deleted — the guard
        # is in the plan, not a prompt. A vault inside the data home is already
        # covered by data_home, so only add it when it lives outside.
        if vault_is_modulatio_owned():
            vault = projects_target()
            try:
                vault.path.resolve().relative_to(data_home.path.resolve())
            except ValueError:
                candidates.append(vault)
    if remove_deliverables:
        candidates.append(deliverables_target())

    return validated_plan(candidates)


def validated_plan(candidates: list[Target]) -> list[Target]:
    """Keep the candidates that exist AND pass ``assert_safe`` (catastrophic-path
    guard — raises loud on a bad one). The single plan-validation site shared by
    ``build_plan`` and ``repair.clear_plan`` so every returned plan carries the
    same invariant: a caller may delete any Target in it without re-checking —
    the delete-time re-check in ``remove_target`` is not the only guard."""
    plan: list[Target] = []
    for t in candidates:
        if not t.path.exists():
            continue
        assert_safe(t.path)  # raises on a catastrophic path — fail loud
        plan.append(t)
    return plan


def backup_plan(plan: list[Target], dest: Path) -> Path | None:
    """Tar up the user-data targets in ``plan`` to ``dest`` before removal.

    Returns the archive path, or ``None`` if there is no user-data to back up.
    Core/cache/model targets are rebuildable and not archived.
    """
    user_targets = [t for t in plan if t.user_data and t.path.exists()]
    if not user_targets:
        return None
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(dest, "w:gz") as tar:
        for t in user_targets:
            tar.add(t.path, arcname=t.path.name)
    return dest


def remove_target(target: Target) -> tuple[bool, str]:
    """Remove one target (file or dir). Returns ``(ok, detail)``.

    Re-checks ``assert_safe`` immediately before deleting — defence in depth, so
    a caller that hand-builds a Target still can't trigger a catastrophic delete.
    """
    p = target.path
    if not p.exists():
        return True, "already absent"
    assert_safe(p)
    try:
        if p.is_dir() and not p.is_symlink():
            shutil.rmtree(p)
        else:
            p.unlink()
        return True, "removed"
    except OSError as e:
        return False, str(e)


def stop_daemon() -> str:
    """Stop the running daemon (kills the process + unlinks its pid file) and
    drop a stale pid file if one remains. Returns a short status string."""
    from modulatio import daemon

    try:
        running = daemon.is_running()
        if running:
            daemon.stop()
        pid = config.CONFIG_DIR / "daemon.pid"
        if pid.exists():
            assert_safe(config.CONFIG_DIR)  # pid lives under a safe dir
            pid.unlink()
        return "daemon stopped" if running else "no daemon running"
    except Exception as e:  # noqa: BLE001 — uninstall must never abort on this
        return f"daemon stop best-effort ({e})"


@dataclass(frozen=True)
class PandocInfo:
    """How pandoc is installed, so a removal can pick the right mechanism."""

    present: bool
    method: str  # 'apt' | 'dnf' | 'brew' | 'binary' | 'none'
    location: str  # binary path, or '' when absent


def detect_pandoc() -> PandocInfo:
    """Detect whether pandoc is present and how it was installed.

    Distinguishes a system-package install (apt/dnf/brew — Modulatio's wizard
    installs it this way) from a standalone binary a user dropped on PATH. The
    caller decides whether/how to remove it; system-package removal needs sudo.
    """
    path = shutil.which("pandoc")
    if not path:
        return PandocInfo(False, "none", "")
    # dpkg/rpm/brew ownership check — is this binary a managed package?
    real = os.path.realpath(path)
    if shutil.which("dpkg") and _owned_by(["dpkg", "-S", real]):
        return PandocInfo(True, "apt", path)
    if shutil.which("rpm") and _owned_by(["rpm", "-qf", real]):
        return PandocInfo(True, "dnf", path)
    if "/Cellar/" in real or "/homebrew/" in real:
        return PandocInfo(True, "brew", path)
    return PandocInfo(True, "binary", path)


def _owned_by(query_cmd: list[str]) -> bool:
    """True if a package-manager query says the file belongs to a package."""
    import subprocess

    try:
        r = subprocess.run(query_cmd, capture_output=True, timeout=10)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False
