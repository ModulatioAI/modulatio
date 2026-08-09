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


class BackupVerificationError(RuntimeError):
    """The backup archive could not be confirmed to hold anything, so the
    removal it was meant to make recoverable must not run."""


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


def schedule_targets() -> list[Target]:
    """Scheduled-job state, at every vault root that has held it.

    Schedules resolve against the CURRENT vault root, so a vault that moved
    leaves its queue behind at the previous location still describing live work.
    Both are removed on any uninstall rather than with the projects tier: a
    schedule that outlives the install fires against files that are gone, and it
    is the one kind of leftover that acts on its own.
    """
    names = ("cron-config.json", "cron-config.json.lock")
    roots = {config.get_vault_root()}
    try:
        roots.add(Path(config._fallback_vault_root()).expanduser())
    except OSError:
        pass
    out: list[Target] = []
    for root in sorted(roots):
        for name in names:
            path = root / name
            if path.exists():
                out.append(Target(f"Schedules ({name})", path, "schedules",
                                  user_data=True))
    return out


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


def vault_state_targets() -> list[Target]:
    """App state Modulatio wrote INSIDE the vault, listed independently of the
    vault directory itself.

    The container and its contents are separate decisions. A vault folder the
    user made is never deleted, but the state written into it is still state,
    and a wipe that spares it leaves keys, grants, agents, schedules and memory
    behind purely because of where they sit. Listing them here makes removal
    depend on what a thing is rather than which folder it landed in.

    Bound to the app's own notion of a project — a vault child only counts if it
    carries the seed markers Modulatio creates — so an unrelated folder sharing
    the vault is never touched. Everything returned is user-data, so it rides in
    the backup before it goes.
    """
    from modulatio import vault

    out: list[Target] = []
    root = config.get_vault_root()
    # Secrets live in the settings home, which a settings wipe removes whole.
    # A vault-side file is the older location, left when a move could not
    # finish; it is still removed so no copy outlives the wipe.
    env = root / ".env"
    if env.exists():
        out.append(Target("Vault secrets (older location)", env, "projects",
                          optional=True, user_data=True))
    for code in vault.list_projects():
        out.append(Target(f"Project state ({code})", vault.project_dir(code),
                          "projects", optional=True, user_data=True))
    return out


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
    candidates.extend(schedule_targets())
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
        else:
            # The container is spared, its contents are not: removing the folder
            # would take work the user put there, but the state Modulatio wrote
            # into it is still state. Skipped when the vault IS ours, since the
            # directory above already covers everything inside it.
            candidates.extend(vault_state_targets())
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
    # The archive holds everything the wipe is about to destroy, secrets
    # included, so it is created owner-only FROM THE START rather than
    # written at whatever the ambient umask allows and narrowed afterwards --
    # a window where the bytes are readable is the same disclosure as leaving
    # them readable. Exclusive creation refuses an existing destination
    # instead of truncating it, and refusing to follow a link means the name
    # cannot be pointed at a file the archive would then overwrite.
    try:
        fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600)
    except OSError as exc:
        raise BackupVerificationError(
            f"backup destination could not be created safely: {dest} ({exc})"
        ) from exc
    with os.fdopen(fd, "wb") as raw:
        with tarfile.open(fileobj=raw, mode="w:gz") as tar:
            for t in user_targets:
                tar.add(t.path, arcname=t.path.name)
        raw.flush()
        os.fsync(raw.fileno())
    # Read the archive back before the caller deletes anything it names. The
    # removal that follows is irreversible and this file is its only safety net,
    # so an archive that wrote nothing has to stop the uninstall rather than let
    # it proceed against a net with no rope in it.
    with tarfile.open(dest, "r:gz") as tar:
        if not tar.getnames():
            raise BackupVerificationError(
                f"backup archive is empty, refusing to remove: {dest}")
    return dest


def prune_backups(directory: Path, prefix: str, keep: int) -> list[Path]:
    """Delete all but the ``keep`` newest ``prefix``-named archives in
    ``directory``, newest decided by filename so it does not depend on mtimes
    surviving a copy. Returns what was removed.

    A tool that cleans up should not itself accumulate: one archive per run with
    nothing pruning them grows without bound in the user's home.
    """
    try:
        archives = sorted(directory.glob(f"{prefix}*.tar.gz"), reverse=True)
    except OSError:
        return []
    removed: list[Path] = []
    for stale in archives[keep:]:
        try:
            stale.unlink()
            removed.append(stale)
        except OSError:
            continue
    return removed


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


def running_processes() -> list[tuple[int, str]]:
    """Every live process running a Modulatio entry point, as ``(pid, command)``.

    Found by inspecting the process table rather than by reading a pid file. The
    servers are launched detached, so they outlive whatever started them: there
    is no parent to walk down from and no pid file this module owns. A witness
    that reads one pid file reports silence for processes it was never able to
    see, which is worse than not looking — it says "nothing running" while a
    server holds its port and serves the install being removed.

    Excludes this process and its ancestors so an uninstall run from a Modulatio
    entry point never reports or stops itself.
    """
    import subprocess

    mine = {os.getpid(), os.getppid()}
    out: list[tuple[int, str]] = []
    try:
        listing = subprocess.run(
            ["ps", "-eo", "pid=,args="], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return out
    for line in listing.stdout.splitlines():
        head, _, command = line.strip().partition(" ")
        if not head.isdigit():
            continue
        pid = int(head)
        if pid in mine or "modulatio" not in command:
            continue
        # Match the EXECUTABLE, not the whole command line: a shell, an editor or
        # a grep whose arguments merely name a Modulatio path is somebody's work,
        # and stopping it would be worse than the leftover this is hunting. An
        # entry point is either the executable itself or the script an
        # interpreter was handed as its first argument.
        parts = command.split()
        if not parts:
            continue
        names = [parts[0].rsplit("/", 1)[-1]]
        if names[0].startswith("python") and len(parts) > 1:
            names.append(parts[1].rsplit("/", 1)[-1])
        if any(n.startswith("modulatio") for n in names):
            out.append((pid, command))
    return out


def stop_processes(timeout_s: float = 8.0) -> list[str]:
    """Stop every process :func:`running_processes` can see, and report each.

    Signals politely, waits, then forces what is still alive: a server left
    running keeps serving a deleted install from memory, and holds its port
    against the next one. A process that refuses both is named rather than
    passed over in silence.
    """
    import signal
    import time as _time

    results: list[str] = []
    targets = running_processes()
    for pid, command in targets:
        name = command.split()[-1] if command else str(pid)
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError as exc:
            results.append(f"could not signal pid {pid} ({name}): {exc}")
            continue
        deadline = _time.monotonic() + timeout_s
        while _time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except OSError:
                break
            _time.sleep(0.2)
        else:
            try:
                os.kill(pid, signal.SIGKILL)
                results.append(f"forced pid {pid} ({name})")
                continue
            except OSError as exc:
                results.append(f"pid {pid} ({name}) would not stop: {exc}")
                continue
        results.append(f"stopped pid {pid} ({name})")
    return results


def stop_daemon() -> str:
    """Stop the running daemon (kills the process + unlinks its pid file) and
    drop a stale pid file if one remains. Returns a short status string.

    Covers the CRON daemon only — it is the one process with a pid file this
    module owns. Everything else is found by inspection in
    :func:`stop_processes`, so a quiet answer here never means the machine is
    quiet.
    """
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


def _systemd_unit_roots() -> tuple[tuple[Path, str], ...]:
    """Where service units live, paired with the scope that owns them.

    Resolved through a function rather than inlined so a test can point it at a
    fixture tree: these are absolute host paths, and a developer machine running
    Modulatio as a service would otherwise change what every test sees.
    """
    return (
        (Path("/etc/systemd/system"), "system"),
        (Path("/run/systemd/system"), "system"),
        (Path.home() / ".config" / "systemd" / "user", "user"),
    )


@dataclass(frozen=True)
class ServiceUnit:
    """An installed service unit that launches Modulatio, and where it lives."""

    name: str    # unit filename, e.g. 'modulatio-api.service'
    path: str    # the unit file on disk
    scope: str   # 'system' (root-owned) | 'user'


def detect_service_units() -> list[ServiceUnit]:
    """Installed systemd units whose start command launches Modulatio.

    A unit outlives the files removed here: one carrying a restart directive
    respawns onto a deleted executable every few seconds, so an uninstall that
    ignores it leaves a service failing in a loop against an install that is
    supposed to be gone. Removing a system unit needs root, which a user-level
    uninstall does not have — the caller reports what it found instead of
    deleting the files out from under a service still trying to run them.

    Matched on the start command rather than the unit name, so a renamed unit is
    still found, and deliberately loose: over-reporting costs a message, while
    under-reporting leaves a service behind.
    """
    found: list[ServiceUnit] = []
    for root, scope in _systemd_unit_roots():
        try:
            units = sorted(root.glob("*.service"))
        except OSError:
            continue
        for unit in units:
            try:
                text = unit.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for line in text.splitlines():
                stripped = line.strip()
                if stripped.startswith("ExecStart") and "modulatio" in stripped:
                    found.append(ServiceUnit(unit.name, str(unit), scope))
                    break
    return found


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
