# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Modulatio configuration — paths, defaults, persistence.

All filesystem paths in Modulatio source MUST be resolved through this
module. Hardcoded paths violate the no-hardcoded-paths principle (see
`feedback_modulatio_no_hardcoded_paths.md`).

Storage:
    ~/.config/modulatio/defaults.json    — paths + default models
    ~/.config/modulatio/preferences.json — user preferences (see preferences.py)
    ~/.config/modulatio/model_presets.json — model presets (slice 2)

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
# reads as a hardcode per ``feedback_modulatio_no_hardcoded_paths.md``).
#
# Late-bound: the helpers below are called on every accessor, so a test
# that scrubs ``XDG_*`` env vars (or a runtime that exports them after
# import) sees the right path immediately. Module-import-time constants
# would freeze the env at import and force test gymnastics.
#
# Wizard suggestions still propose Obsidian vaults when detected; these
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


def write_secret_file(path: Path, content: str) -> None:
    """Atomically write *content* to *path* with mode 0o600 throughout.

    The naive ``path.write_text(..., encoding="utf-8"); path.chmod(0o600)`` pattern leaves
    the file briefly world-readable between create-with-default-umask
    and the explicit chmod. On a multi-user host, that window is enough
    to leak credentials; the pre-V2 security audit (2026-05-02) flagged
    six call sites doing exactly this for tokens, OAuth credentials,
    .env files, telegram config, and backups.

    This helper opens the temp file with mode 0o600 directly, writes to
    it, and atomically renames it into place. The file is never readable
    by other users at any point. The temp file lives in the target's
    parent directory so the rename is guaranteed atomic on POSIX.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
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
    env_path = get_vault_root() / ".env"
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
    removed = False
    env_path = get_vault_root() / ".env"
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
        vault_env = get_vault_root() / ".env"
        if vault_env.exists():
            load_dotenv(vault_env, override=False)
    except Exception:
        pass  # never block startup on env-load failure
    _DOTENV_LOADED = True
    # SETTINGS-tab overrides layer AFTER the dotenv layers, same
    # shell-wins contract (see apply_env_overrides).
    try:
        apply_env_overrides()
    except Exception:
        pass  # never block startup on a malformed overrides block


#: The ONLY keys apply_env_overrides will inject — the same curated set the
#: SETTINGS tab exposes, enforced at the BACKEND so a hand-edited
#: defaults.json can't become a persistent environment injector (Wild Bill
#: BLOCK, arc cadre 2026-07-04: an unrestricted block could set
#: MODULATIO_RUN_SHELL_UNSAFE / MODULATIO_SANDBOX_PROFILE / LD_PRELOAD
#: forever). Widening this list is a deliberate, reviewed act.
ENV_OVERRIDE_ALLOWLIST: frozenset[str] = frozenset({
    "MODULATIO_TASK_MAX_RETRIES",
    "MODULATIO_GOAL_MAX_RETRIES",
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
})

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
            # Backend allowlist (Wild Bill BLOCK): unknown keys are refused
            # loudly, not injected — never a persistent sandbox/loader hijack.
            logger.warning(
                "env_overrides: refusing non-allowlisted key %r", key)
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
