# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Modulatio configuration — paths, defaults, persistence.

All filesystem paths in Modulatio source MUST be resolved through this
module. Hardcoded paths violate the no-hardcoded-paths principle.

Storage:
    ~/.config/modulatio/defaults.json    — paths + default models
    ~/.config/modulatio/preferences.json — user preferences (see preferences.py)
    ~/.config/modulatio/model_presets.json — model presets

Defaults schema (filled in by the setup wizard from the user's
choices — Modulatio is model-agnostic, no role-to-model bindings
ship in source):

    {
        "vault_root": "<path>",
        "shared_resources_path": "<path>",
        "cache_root": "<path>",
        "default_models": {
            "leader": "<preset-key from model_presets.json>",
            "planner": "<preset-key>",
            "producer": "<preset-key>",
            "qc": "<preset-key>",
            "researcher": "<preset-key>"
        }
    }

The wizard writes this file on first install based on which models
the user registered + assigned to each role. Path fallbacks let the
engine boot without a wizard run; ``default_models`` has NO fallback —
unconfigured roles return ``None`` and the orchestrator surfaces
that explicitly rather than silently picking a model.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import Optional

logger = logging.getLogger("modulatio.config")

# === Locations ===

CONFIG_DIR = Path.home() / ".config" / "modulatio"
DEFAULTS_FILE = CONFIG_DIR / "defaults.json"
TEAM_TEMPLATE_FILE = CONFIG_DIR / "team_template.json"
AUTH_ALERTS_FILE = CONFIG_DIR / "auth_alerts.json"

# === Fallbacks (used when defaults.json is missing or incomplete) ===
#
# These are sensible boot defaults, NOT canonical truth. defaults.json is
# truth. The wizard overwrites these on first install. Tests should
# monkeypatch via save_defaults() + reload(), or patch module-level
# constants in consuming modules.
#
# Both vault and shared-resources fall back to neutral paths that don't
# assume Obsidian. The wizard (slice 3) detects ~/Obsidian/ presence and
# offers it as the suggested default for users who want Obsidian
# integration; otherwise these neutral paths are used.
#
# Pre-wizard, missing shared resources (skills/standards/research files)
# fall back to the in-code constants in orchestration.py per slice #6f-A
# — system stays functional even with an empty shared-resources directory.

def _xdg_data_home() -> Path:
    """Resolve XDG_DATA_HOME with the spec's default fallback.

    Per https://specifications.freedesktop.org/basedir-spec, falls back
    to ``~/.local/share`` when XDG_DATA_HOME is unset or empty.
    """
    raw = os.environ.get("XDG_DATA_HOME", "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".local" / "share"


def _xdg_cache_home() -> Path:
    raw = os.environ.get("XDG_CACHE_HOME", "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".cache"


# XDG-aware fallbacks: when the wizard hasn't run yet (or the user has
# explicitly chosen "use default fallback"), data lives under
# ``$XDG_DATA_HOME/modulatio/`` and cache under ``$XDG_CACHE_HOME/modulatio/``.
# That respects the XDG Base Directory Specification on Linux/macOS and
# keeps Modulatio out of ``~/modulatio/`` (a magic top-level dir name that
# reads as a hardcode).
#
# Late-bound: the helpers below are called on every accessor, so a test
# that scrubs ``XDG_*`` env vars (or a runtime that exports them after
# import) sees the right path immediately. Module-import-time constants
# would freeze the env at import and force test gymnastics.
#
# Wizard suggestions still propose known vault directories when detected; these
# paths only matter when the wizard hasn't run.
def _fallback_vault_root() -> str:
    return str(_xdg_data_home() / "modulatio" / "projects")


def _fallback_shared_resources() -> str:
    return str(_xdg_data_home() / "modulatio" / "shared")


def _fallback_cache_root() -> str:
    return str(_xdg_cache_home() / "modulatio")

# Default embedder for semantic routing + team-memory recall. Small,
# CPU-friendly, ~80MB cached weights. Wizard's embedded-LLM step can
# override via ``defaults.json["embedding_model"]``. Single source of
# truth — semantic_router and team_memory both read it from here.
_FALLBACK_EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"

# === Cache ===

_cached_defaults: Optional[dict] = None
_lock = threading.Lock()


def _load_defaults() -> dict:
    """Load defaults.json into the module cache. Empty dict if file missing
    or malformed — fallback paths apply per-accessor."""
    global _cached_defaults
    with _lock:
        if _cached_defaults is not None:
            return _cached_defaults
        if not DEFAULTS_FILE.exists():
            _cached_defaults = {}
            return _cached_defaults
        try:
            _cached_defaults = json.loads(DEFAULTS_FILE.read_text(encoding="utf-8", errors="replace"))
        except (json.JSONDecodeError, OSError):
            _cached_defaults = {}
        return _cached_defaults


def reload() -> None:
    """Force re-read of defaults.json — for tests and post-wizard refresh."""
    global _cached_defaults
    with _lock:
        _cached_defaults = None


def defaults_exist() -> bool:
    """First-install detection: True if user has run the wizard at least once."""
    return DEFAULTS_FILE.exists()


def _harden_secret_dir(directory: Path) -> None:
    """Make the engine's own settings directory owner-only, and refuse one
    owned by somebody else.

    A file's mode does not protect it from its parent: anyone who can write
    the DIRECTORY can rename a secret aside and put their own in its place,
    whatever the mode on the file says. Directories created under an ordinary
    group-writable umask therefore leave a 0600 secret replaceable.

    Applies to the engine's own settings home only. A vault a user pointed at
    their own folder is theirs, and its permissions are not the engine's to
    rewrite.
    """
    try:
        resolved = directory.resolve()
        if resolved != CONFIG_DIR.resolve() and CONFIG_DIR.resolve() not in resolved.parents:
            return
        st = resolved.stat()
    except OSError:
        return
    if st.st_uid != os.getuid():
        raise PermissionError(
            f"refusing to write a secret into {resolved}: it belongs to "
            f"another user (uid {st.st_uid})"
        )
    if st.st_mode & 0o077:
        os.chmod(resolved, 0o700)


def write_secret_file(path: Path, content: str) -> None:
    """Atomically write *content* to *path* with mode 0o600 throughout.

    The naive ``path.write_text(..., encoding="utf-8"); path.chmod(0o600)`` pattern leaves
    the file briefly world-readable between create-with-default-umask
    and the explicit chmod. On a multi-user host, that window is enough
    to leak credentials across six call sites doing exactly this for tokens, OAuth credentials,
    .env files, telegram config, and backups.

    This helper opens the temp file with mode 0o600 directly, writes to
    it, and atomically renames it into place. The file is never readable
    by other users at any point. The temp file lives in the target's
    parent directory so the rename is guaranteed atomic on POSIX.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _harden_secret_dir(path.parent)
    # Unique temp name (not a fixed ``<name>.tmp``): two concurrent writers for
    # the SAME secret path (an auth-alert write racing a key-pin write) would
    # otherwise share one temp file, clobber each other's bytes, and interleave
    # the replace/unlink — corrupting the secret. mkstemp creates the file with
    # mode 0o600, so there is never a world-readable window either.
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=path.name + ".", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp_name, str(path))
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def secrets_path() -> Path:
    """THE file secrets and API keys live in.

    One home, shared by every surface that sets or reads a key, and it is the
    settings directory rather than the vault. Keys kept beside a user's work
    inherit that folder's fate: a vault someone points at a notes directory
    cannot be deleted on a wipe, and anything colocated with it survives by
    accident of location. Settings are removable without touching work, so a
    wipe can take the secrets and leave the documents.
    """
    return CONFIG_DIR / ".env"


def migrate_vault_secrets() -> int:
    """Move secrets out of an older vault-side ``.env`` into the settings home.

    Returns the number of assignments moved. Values already present in the
    settings home win — a key set since the move is newer than one left behind.
    The source file is removed only once every assignment it held is readable
    from the new home, so an interrupted move loses nothing and simply repeats.
    """
    try:
        legacy = get_vault_root() / ".env"
        if not legacy.is_file():
            return 0
        current = _parse_env_assignments(secrets_path())
        legacy_pairs = _parse_env_assignments(legacy)
        moved = 0
        for name, value in legacy_pairs.items():
            if name in current:
                continue
            set_env_secret(name, value)
            moved += 1
        if all(k in _parse_env_assignments(secrets_path()) for k in legacy_pairs):
            legacy.unlink()
        return moved
    except OSError:
        # A move that cannot complete leaves the source in place; the loader
        # still reads it, so the keys keep working and the next start retries.
        logger.warning("vault secret migration incomplete", exc_info=True)
        return 0


def _parse_env_assignments(path: Path) -> "dict[str, str]":
    """``NAME=value`` pairs from an env file, ignoring comments and blanks.
    Missing or unreadable file reads as empty."""
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        out[key.strip()] = value
    return out


#: Serializes the read-modify-replace behind every secret edit. Two writers
#: that each read the file, change their own key, and write the whole thing
#: back will each write a copy that never saw the other's change, so the edit
#: that lands second silently drops the first. The lock spans the READ as well
#: as the write, because reading stale content is what makes the loss.
_SECRET_EDIT_LOCK = threading.RLock()


@contextlib.contextmanager
def _secret_edit_guard():
    """Hold the in-process lock and a file lock over the secret store, so
    concurrent editors in one process and in separate processes both
    serialize."""
    lock_path = Path(str(secrets_path()) + ".lock")
    with _SECRET_EDIT_LOCK:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)


def set_env_secret(name: str, value: str) -> Path:
    """Set/update a single secret (e.g. an API key) in the vault ``.env``,
    0600, and load it into ``os.environ`` so it's usable immediately.

    Used by the Configuration tab when the operator enters an API key — the
    key persists across sessions (loaded by ``load_modulatio_env``) and is live
    this session. The named key is updated in place when already present, or
    appended; ALL other lines — including comments and blanks — are preserved
    verbatim (mirroring ``remove_env_secret``'s preserve-and-rewrite). Returns
    the env file path.

    A newline (``\\n``/``\\r``) anywhere in *name* or *value* would let a
    crafted key value inject a second ``KEY=...`` assignment into the file, so
    such inputs are rejected fail-closed rather than written. An ``=`` is fine
    in the value (readers split on the first ``=``) but not in the key name.
    """
    if "\n" in name or "\r" in name or "\n" in value or "\r" in value:
        raise ValueError("env secret name/value must not contain newlines")
    if "=" in name or not name.strip():
        raise ValueError("env secret name must be non-empty and contain no '='")
    with _secret_edit_guard():
        return _set_env_secret_locked(name, value)


def _set_env_secret_locked(name: str, value: str) -> Path:
    env_path = secrets_path()
    out_lines: list[str] = []
    replaced = False
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
            stripped = line.strip()
            if (stripped and not stripped.startswith("#") and "=" in stripped
                    and stripped.split("=", 1)[0].strip() == name):
                # Update the existing assignment in place; preserve position.
                if not replaced:
                    out_lines.append(f"{name}={value}")
                    replaced = True
                # Drop any later duplicate assignments of the same key.
                continue
            out_lines.append(line)  # comment, blank, or unrelated kv — verbatim
    if not replaced:
        out_lines.append(f"{name}={value}")
    write_secret_file(env_path, "\n".join(out_lines) + "\n")
    os.environ[name] = value
    return env_path


def remove_env_secret(name: str) -> bool:
    """Remove a secret from the vault ``.env`` and ``os.environ``. Returns True
    if it was present. Backs the Configuration tab's "remove key"."""
    with _secret_edit_guard():
        return _remove_env_secret_locked(name)


def _remove_env_secret_locked(name: str) -> bool:
    removed = False
    env_path = secrets_path()
    if env_path.exists():
        kept: list[str] = []
        for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
            stripped = line.strip()
            if (stripped and not stripped.startswith("#") and "=" in stripped
                    and stripped.split("=", 1)[0].strip() == name):
                removed = True
                continue
            kept.append(line)
        if removed:
            write_secret_file(env_path, "\n".join(kept) + ("\n" if kept else ""))
    if name in os.environ:
        del os.environ[name]
        removed = True
    return removed


def save_defaults(defaults: dict) -> None:
    """Persist defaults to disk + invalidate cache."""
    global _cached_defaults
    write_secret_file(DEFAULTS_FILE, json.dumps(defaults, indent=2))
    with _lock:
        _cached_defaults = defaults


# === Path accessors ===

def _expand(p: Optional[str], fallback: str) -> Path:
    """Expand ~ and turn into an absolute Path. Falls back to provided default
    when value is missing or empty.

    A *relative* value is anchored to ``$HOME``, never the process cwd: the
    daemon (or a launch from a different directory) would otherwise resolve the
    vault to a different place after a reboot and the project's config would
    appear lost. ``.resolve()`` on a relative path uses cwd — so anchor first.
    """
    path = Path(os.path.expanduser(p or fallback))
    if not path.is_absolute():
        path = Path.home() / path
    return path.resolve()


def get_vault_root() -> Path:
    """Per-project vault folders live under here.

    Default fallback: ``$XDG_DATA_HOME/modulatio/projects/`` (typically
    ``~/.local/share/modulatio/projects/`` per XDG spec). Obsidian-agnostic.
    Wizard overrides via vault_path step.
    """
    return _expand(_load_defaults().get("vault_root"), _fallback_vault_root())


_DOTENV_LOADED: bool = False


def load_modulatio_env() -> None:
    """Load .env files into ``os.environ`` for any Modulatio entry point.

    Two layers, both ``override=False`` so shell-exported vars still win:

    1. ``<install-root>/.env`` — repo-local secrets staged at the project
       root (e.g. ``~/modulatio-v2/.env``). Useful for dev workflows.
    2. ``<vault-root>/.env`` — per-vault secrets the setup wizard stages
       at finalize. This is where production users keep API keys.

    Idempotent: subsequent calls are no-ops, so multiple entry points
    importing this won't load twice. Safe to call from cli.py module
    init AND from the TUI's ``run()`` (or any future API/daemon entry).
    """
    global _DOTENV_LOADED
    if _DOTENV_LOADED:
        return
    try:
        from dotenv import load_dotenv  # type: ignore[import-not-found]
    except ImportError:
        _DOTENV_LOADED = True  # don't retry — dotenv missing is fine
        return
    install_root = Path(__file__).resolve().parents[2]
    project_env = install_root / ".env"
    if project_env.exists():
        load_dotenv(project_env, override=False)
    try:
        migrate_vault_secrets()
    except Exception:
        # A failed move must not stop the keys from loading below.
        logger.warning("vault secret migration skipped", exc_info=True)
    try:
        secrets = secrets_path()
        if secrets.exists():
            load_dotenv(secrets, override=False)
        # An older vault-side file is still read, AFTER the settings home so
        # the newer value wins, for the case where the move could not finish.
        vault_env = get_vault_root() / ".env"
        if vault_env.exists():
            load_dotenv(vault_env, override=False)
    except Exception:
        # Never block startup on env-load failure — but leave a trace.
        logger.warning(".env load failed; continuing without it",
                       exc_info=True)
    _DOTENV_LOADED = True
    # SETTINGS-tab overrides layer AFTER the dotenv layers, same
    # shell-wins contract (see apply_env_overrides).
    try:
        apply_env_overrides()
    except Exception:
        # Never block startup on a malformed overrides block — but leave a trace.
        logger.warning("settings env-overrides apply failed; continuing",
                       exc_info=True)


#: The ONLY keys apply_env_overrides will inject — the same curated set the
#: SETTINGS tab exposes, enforced at the BACKEND so a hand-edited
#: defaults.json can't become a persistent environment injector: an
#: unrestricted block could otherwise set MODULATIO_RUN_SHELL_UNSAFE /
#: MODULATIO_SANDBOX_PROFILE / LD_PRELOAD forever. Widening this list is a
#: deliberate act.
ENV_OVERRIDE_ALLOWLIST: frozenset[str] = frozenset({
    "MODULATIO_TASK_MAX_RETRIES",
    "MODULATIO_GOAL_MAX_RETRIES",
    "MODULATIO_GOAL_REDO_ACTOR",
    "MODULATIO_TASK_CONTEXT_CAP_PCT",
    "MODULATIO_QC_FIXER",
    "MODULATIO_SKILL_CODIFICATION",
    "MODULATIO_JT_CODIFICATION",
    "MODULATIO_CONCURRENT_WAVES",
    "MODULATIO_WAVE_POOL_CEILING",
    "MODULATIO_SIZE_TOLERANCE",
    "MODULATIO_CTX_BUDGET_PRODUCER",
    "MODULATIO_CTX_BUDGET_QC",
    "MODULATIO_CTX_BUDGET_PLANNER",
    "MODULATIO_CTX_BUDGET_LEADER_DECOMPOSE",
    "MODULATIO_CTX_BUDGET_LEADER_ITERATE",
    "MODULATIO_CTX_BUDGET_LEADER_REFLECT",
    "MODULATIO_CTX_BUDGET_LEADER_CHAT",
    "MODULATIO_CTX_BUDGET_RESEARCH",
    "MODULATIO_WEB_PORT",
    "MODULATIO_REQUIRE_SANDBOX",
    "MODULATIO_SANDBOX_PROFILE",
    "MODULATIO_CALL_TIMEOUT",
    "MODULATIO_WAVE_GLOBAL_CAP",
    "MODULATIO_DISPATCH_BREAKER",
    "MODULATIO_LEADER_ITERATE",
    "MODULATIO_WAVE_REFLECT",
    "MODULATIO_INBOXES",
    "MODULATIO_WIN_CODIFY_FLOOR",
    "MODULATIO_CODIFICATION_TIMEOUT_S",
    "MODULATIO_MAX_ATTACHMENT_BYTES",
    "MODULATIO_CRASH_KEEP",
    "MODULATIO_LOW_CREDIBILITY_DOMAINS",
})

#: Values a stored override may never carry, per allowlisted key. The key is
#: settable; these particular values are not, because they turn confinement
#: off — which stays an explicit environment act rather than stored state.
#:
#: Each entry names the CANONICALIZER the consumer applies, so the refusal
#: compares the same spelling the runtime acts on. Comparing raw text here
#: would let ``"OFF"`` or ``" off "`` past a guard whose consumer lowercases
#: and strips — the guard has to speak the consumer's language.
def _canonical_sandbox_profile(value: object) -> str:
    from modulatio.sandbox import canonical_profile
    return canonical_profile(value)


_REFUSED_OVERRIDE_VALUES: "dict[str, tuple]" = {
    "MODULATIO_SANDBOX_PROFILE": (
        _canonical_sandbox_profile, frozenset({"off"})),
}

#: Keys apply_env_overrides has set — so a later save can update or unset
#: them live, while never touching a key the shell/.env owns.
_ENV_OVERRIDES_SET: set[str] = set()


def apply_env_overrides() -> None:
    """Push ``defaults.json["env_overrides"]`` into ``os.environ``.

    The SETTINGS tab's persistence mechanism: every per-call env knob
    becomes persistent through this one seam, with zero changes at the
    read sites. Precedence is honest — a key already present in the
    environment that we did NOT set (shell export, .env file) wins and
    is never overwritten. Re-callable for live apply: updates re-set,
    and keys removed from the block are unset (only ours). Absent or
    malformed block → no-op (byte-identical default behavior)."""
    overrides = _load_defaults().get("env_overrides")
    if not isinstance(overrides, dict):
        overrides = {}
    for key in list(_ENV_OVERRIDES_SET):
        if key not in overrides:
            os.environ.pop(key, None)
            _ENV_OVERRIDES_SET.discard(key)
    for key, value in overrides.items():
        key = str(key)
        if key not in ENV_OVERRIDE_ALLOWLIST:
            # Unknown keys are refused
            # loudly, not injected — never a persistent sandbox/loader hijack.
            logger.warning(
                "env_overrides: refusing non-allowlisted key %r", key)
            continue
        canonicalize, refused = _REFUSED_OVERRIDE_VALUES.get(
            key, (None, frozenset()))
        if canonicalize is not None and canonicalize(value) in refused:
            # A refused value also RELEASES any value this loop applied
            # earlier: the file no longer claims the old one, so keeping it
            # enforced would be ownership the stored state has dropped.
            if key in _ENV_OVERRIDES_SET:
                os.environ.pop(key, None)
                _ENV_OVERRIDES_SET.discard(key)
            # Some values of an otherwise settable key disable confinement
            # outright. Those stay a deliberate shell/env act — a stored file
            # must not be able to persist one.
            logger.warning(
                "env_overrides: refusing %r=%r — that value disables "
                "confinement and must be set in the environment, not stored",
                key, str(value))
            continue
        if key in os.environ and key not in _ENV_OVERRIDES_SET:
            continue  # shell/.env owns it — the tab renders it read-only
        os.environ[key] = str(value)
        _ENV_OVERRIDES_SET.add(key)


def get_shared_resources_path() -> Path:
    """Shared skills, standards, templates, research live under here.

    Default fallback is the neutral ``_fallback_shared_resources()``
    constant defined above (currently ``~/modulatio/shared``). Wizard
    overrides via the vault-path step (paired with ``vault_root``).
    """
    return _expand(_load_defaults().get("shared_resources_path"), _fallback_shared_resources())


def get_cache_root() -> Path:
    """Embedder caches, vector indexes, etc. XDG-compliant default."""
    return _expand(_load_defaults().get("cache_root"), _fallback_cache_root())


def get_embedding_model() -> str:
    """Embedder model identifier (HuggingFace / sentence-transformers
    name) for semantic routing + team-memory recall. Single source of
    truth — wizard override via ``defaults.json["embedding_model"]``;
    fallback is the package default.
    """
    raw = _load_defaults().get("embedding_model")
    return raw if isinstance(raw, str) and raw.strip() else _FALLBACK_EMBEDDING_MODEL


def get_data_file(name: str) -> Path:
    """Vault-root-relative data file path (e.g. heartbeat-queue.json)."""
    return get_vault_root() / name


# === Default model accessors (filled by setup wizard) ===

# Skills-first (#143): "planner" is the current task-planning role key;
# "coordinator" is kept for back-compat reads of pre-defaults.json.
# Role-language migration (v0.6.0): "producer" is the current producer-model
# key; "specialist" is kept for back-compat reads of pre-migration
# defaults.json. "researcher" likewise stays readable (research now routes by
# capability to a producer; the key is no longer written for new installs).
_VALID_ROLES = ("leader", "planner", "producer", "coordinator", "specialist", "qc", "researcher")


def get_default_model(role: str) -> Optional[str]:
    """Return the persisted default model for a role, if set.

    Roles: leader, planner, producer, qc (legacy "coordinator"/"specialist"/
    "researcher" still readable for pre-migration defaults.json).
    Returns None if no default has been set for that role — caller falls
    back to CLI flag or its own default.
    """
    if role not in _VALID_ROLES:
        return None
    return _load_defaults().get("default_models", {}).get(role)


def get_default_models() -> dict:
    """Return the full default-model dict (role -> model_id)."""
    return dict(_load_defaults().get("default_models", {}))


def get_default_project_code() -> Optional[str]:
    """Return the project code captured by the wizard's first_project_step
    (or set later via ``modulatio`` defaults), or ``None`` if no default
    has been recorded. Bare ``modulatio`` (no subcommand) reads this to
    know which project to launch the TUI on."""
    code = _load_defaults().get("default_project_code")
    return code if isinstance(code, str) and code else None


def set_default_project_code(code: str) -> None:
    """Update defaults.json with a new default project code. Used by the
    TUI / CLI when the user explicitly switches default project, and by
    the wizard's finalize when first_project_step captured one."""
    defaults = dict(_load_defaults())
    defaults["default_project_code"] = code
    save_defaults(defaults)


# === Default budget caps (per-plan inheritance) ===
#
# Project-level defaults that new plans inherit at ``plans.persist`` time.
# Hand-edited in ``defaults.json`` under ``budget_caps`` (no wizard prompt
# yet — caps are an advanced bounded-mode feature; first-run users skip).
# Each axis is independently None (unbounded) or set. Plans can override
# per-plan by editing the persisted plan's frontmatter before approval.

_BUDGET_CAP_KEYS = ("max_wall_clock_min", "max_tokens", "max_cost_usd")


def get_default_budget_caps() -> dict:
    """Return the project-level default budget caps as a dict with three
    keys: ``max_wall_clock_min`` (float | None), ``max_tokens`` (int |
    None), ``max_cost_usd`` (float | None). Missing or malformed entries
    surface as None — callers (typically ``plans.persist``) only inherit
    fields that are explicitly set.
    """
    raw = _load_defaults().get("budget_caps") or {}
    out: dict = {k: None for k in _BUDGET_CAP_KEYS}
    if not isinstance(raw, dict):
        return out
    if isinstance(raw.get("max_wall_clock_min"), (int, float)) and not isinstance(raw.get("max_wall_clock_min"), bool):
        out["max_wall_clock_min"] = float(raw["max_wall_clock_min"])
    if isinstance(raw.get("max_tokens"), int) and not isinstance(raw.get("max_tokens"), bool):
        out["max_tokens"] = int(raw["max_tokens"])
    if isinstance(raw.get("max_cost_usd"), (int, float)) and not isinstance(raw.get("max_cost_usd"), bool):
        out["max_cost_usd"] = float(raw["max_cost_usd"])
    return out


def set_default_budget_caps(
    *,
    max_wall_clock_min: float | None = None,
    max_tokens: int | None = None,
    max_cost_usd: float | None = None,
) -> None:
    """Persist project-level default budget caps. ``None`` for any axis
    clears that axis (it falls back to unbounded). Pass values to set;
    omit keyword args to leave existing values untouched is NOT what
    this helper does — callers that want partial updates should read,
    merge, then pass the merged dict.
    """
    defaults = dict(_load_defaults())
    caps: dict = {}
    if max_wall_clock_min is not None:
        caps["max_wall_clock_min"] = float(max_wall_clock_min)
    if max_tokens is not None:
        caps["max_tokens"] = int(max_tokens)
    if max_cost_usd is not None:
        caps["max_cost_usd"] = float(max_cost_usd)
    if caps:
        defaults["budget_caps"] = caps
    else:
        defaults.pop("budget_caps", None)
    save_defaults(defaults)


# === Team template (wizard-defined agent roster) ===
#
# The setup wizard provisions a team (mandatory triad + 1-7 workers) and
# persists it here as a list of agent dicts. ``roster.seed_default_roster``
# reads this file when seeding a new project so wizard picks become real
# agents in every project. When the file is absent (no wizard run), the
# hardcoded fallback in ``roster._DEFAULT_ROSTER_TEMPLATE`` is used so v2
# still boots out-of-the-box.

def load_team_template() -> Optional[list[dict]]:
    """Return the wizard-defined team template (list of agent dicts), or
    ``None`` if the file is absent or malformed. Each agent dict carries
    ``id``, ``name``, ``identity``, ``skills``, ``model``, ``model_tier``,
    ``cost_class``, ``capability_tags``, ``tier``, ``template_origin``."""
    if not TEAM_TEMPLATE_FILE.exists():
        return None
    try:
        data = json.loads(TEAM_TEMPLATE_FILE.read_text(encoding="utf-8", errors="replace"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, list):
        return None
    return data


def save_team_template(agents: list[dict]) -> None:
    """Persist the wizard's agent picks. Overwrites any existing template.

    Uses ``write_secret_file`` for a 0o600-throughout write — the naive
    ``write_text`` + ``chmod`` pattern this helper exists to replace would
    leave the roster briefly world-readable on a multi-user host."""
    write_secret_file(TEAM_TEMPLATE_FILE, json.dumps(agents, indent=2))


# ── The FOLDERS registry — named operator folders for job runs ─────────────
#
# Install-wide, in defaults.json. A record is {"name", "path", "mode",
# "kind"}: mode "ro" (seats read) | "output" (seats read; the ENGINE may
# deliver the finished product there) | "rw" (seats read/write live mid-run).
# kind is "path" today (an already-mounted location — local dir, mapped
# drive, or a mounted smb/cifs/nfs share); unknown kinds are skipped so a
# future in-app-mount kind can land without breaking older installs.

_FOLDER_MODES = ("ro", "output", "rw")


def list_folders(on_corrupt=None) -> list[dict]:
    """The registered folders, shape-checked. Malformed entries (bad mode,
    relative path, missing/empty fields, unknown kind) are dropped AND
    reported — per record via ``on_corrupt`` when given, else the module
    logger — so a hand-edited defaults.json can't inject a bad record
    downstream, and a dropped folder never reads as merely unregistered.
    The remaining well-formed records still load (warn-and-skip, never
    warn-and-abort)."""

    def _report(reason: str) -> None:
        if on_corrupt is not None:
            on_corrupt(reason)
        else:
            logger.warning("folder registry: %s", reason)

    raw = _load_defaults().get("folders")
    if raw is None:
        return []
    if not isinstance(raw, list):
        _report("folders is not a list — all records dropped")
        return []
    folders: list[dict] = []
    for rec in raw:
        if not isinstance(rec, dict):
            _report(f"non-record entry dropped: {rec!r:.80}")
            continue
        name = rec.get("name")
        path = rec.get("path")
        if not (isinstance(name, str) and name.strip()):
            _report(f"record with missing/empty name dropped: {rec!r:.80}")
            continue
        if not (isinstance(path, str) and Path(path).is_absolute()):
            _report(f"record {name!r} dropped: path must be absolute")
            continue
        if rec.get("mode") not in _FOLDER_MODES or rec.get("kind") != "path":
            _report(f"record {name!r} dropped: unknown mode/kind")
            continue
        folders.append(rec)
    return folders


def save_folders(folders: list[dict]) -> None:
    """Persist the folder registry (overwrites)."""
    defaults = dict(_load_defaults())
    defaults["folders"] = folders
    save_defaults(defaults)


def get_job_output_folder() -> Optional[str]:
    """The name of the picked job-output folder — only if it names a
    registered ``output``-mode folder (the accessor is the floor: a stale
    pick after a delete/mode-change reads as no pick)."""
    name = _load_defaults().get("job_output_folder")
    if not (isinstance(name, str) and name):
        return None
    for rec in list_folders():
        if rec["name"] == name and rec["mode"] == "output":
            return name
    return None


def set_job_output_folder(name: "str | None") -> None:
    """Record (or clear, with None) the picked job-output folder."""
    defaults = dict(_load_defaults())
    if name:
        defaults["job_output_folder"] = name
    else:
        defaults.pop("job_output_folder", None)
    save_defaults(defaults)


def probe_folder(path: str, timeout_s: float = 2.0) -> bool:
    """True if ``path`` is a reachable directory. The stat runs in a daemon
    thread with a join timeout so a dead network mount (a hung NFS/CIFS
    stat can block indefinitely) never wedges the TUI or the engine."""
    import threading

    box: list[bool] = []

    def _check() -> None:
        try:
            box.append(Path(path).is_dir())
        except OSError:
            box.append(False)

    t = threading.Thread(target=_check, daemon=True)
    t.start()
    t.join(timeout_s)
    return bool(box and box[0])


def folder_root_refusal(path: str) -> "str | None":
    """The single safety floor for a registered-folder root — shared by the
    FOLDERS tab (ADD time), the grant classifier (USE time), and the
    output-pick resolver, so the three sites can't drift. Returns a reason
    string if ``path`` is unsafe to use as a folder root (a dotfile path
    component / secrets dir, a broad system dir, $HOME itself, or an overlap
    with the vault or delivery trees), else ``None``. Reachability is checked
    separately (``probe_folder``) — it's handled differently per site (the tab
    errors, grants skip, the pick falls back)."""
    from modulatio import delivery, vault  # lazy — vault imports config
    from modulatio.leader_gate import dangerous_widen_root

    blocked = [str(vault.VAULT_ROOT), str(delivery.delivery_root())]
    return dangerous_widen_root(path, blocked_subtrees=blocked)


def folder_grant_roots() -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The registered folders as seat-grant roots: ``(rw_roots, read_roots)``
    — rw-mode folders in the first tuple, ro/output in the second.

    USE-time re-validation (defense in depth with the tab's ADD-time check):
    every root must be reachable (``probe_folder``) and pass
    ``folder_root_refusal`` — so a hand-edited defaults.json can't grant the
    team /etc, a secrets dot-dir, or the swarm's own work tree."""
    rw: list[str] = []
    read: list[str] = []
    for rec in list_folders():
        path = rec["path"]
        if not probe_folder(path):
            continue
        if folder_root_refusal(path) is not None:
            continue
        (rw if rec["mode"] == "rw" else read).append(path)
    return tuple(rw), tuple(read)
