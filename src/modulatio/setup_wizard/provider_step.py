# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Combined model-curation step — single-flow add of self-contained models.

(File still named provider_step.py for git-history continuity; the
wizard step it owns is now "Models" — labels reflect that.)

Each model entry is fully self-contained (label + base_url + api_format
+ auth + model id). Quick-add detection rows still appear when their
upstream credentials/services exist, but each quick-add prompts for the
model id inline so the entry is complete by the time it lands in
``model_presets.json``.

Per locked design (2026-04-26): no provider/model split. User UX call
("Feng Shui — minimalist, functionality, flow"). Two models from the
same vendor → two entries. Auth fields duplicated by design; value-level
dedup via shared env var or shared OAuth credential file.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from modulatio import model_presets, oauth_helpers, theme
from modulatio.setup_wizard import steps


_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_\-]{0,63}$")


# === Curated model lists for OAuth quick-add (slice #23) ===
#
# Anthropic and OpenAI don't reliably expose `/v1/models` to the OAuth
# token paths the wizard uses (Claude CLI / Codex CLI). Instead of
# free-text prompts that produce typo entries (anthropic_ql, etc.),
# the wizard offers a curated picker.
#
# Source of truth lives as data (not code) at
# ``src/modulatio/_seed_data/oauth_model_picklists.json`` so refreshing
# the curated list when a vendor ships a new model is a JSON edit, not
# a code change. Loaded once at import and exposed as the same module
# globals callers + tests expect.

def _load_oauth_picklists() -> dict[str, tuple[str, ...]]:
    """Read the curated picklists from the package's seed-data dir.

    Returns a dict keyed by vendor slug ('anthropic', 'openai') with
    tuple values (sorted, immutable). Hatchling's wheel target ships
    ``src/modulatio/`` whole, so the JSON travels with the package and
    needs no extra build config.
    """
    seed = Path(__file__).resolve().parent.parent / "_seed_data" / "oauth_model_picklists.json"
    # re-sweep (finding 1): a malformed/edited seed must not brick the whole
    # wizard at import. Degrade to empty picklists so the curated OAuth picker
    # falls back to manual entry instead of raising an opaque traceback.
    try:
        raw = json.loads(seed.read_text(encoding="utf-8", errors="replace"))
        if not isinstance(raw, dict):
            raise ValueError("oauth_model_picklists.json must be a JSON object")
    except (OSError, ValueError) as exc:  # ValueError covers json.JSONDecodeError
        theme.warn(
            f"curated OAuth model list unavailable ({exc}); "
            "use manual model entry."
        )
        return {}
    # re-sweep (finding 1, r4): the comprehension sits OUTSIDE the try above,
    # so a hand-edited seed whose list mixes uncomparable elements (e.g. a str
    # and a null → sorted() raises TypeError) would still brick the wizard at
    # import despite the stated "malformed seed never bricks" intent. Filter to
    # str elements before sorting so heterogeneous/None entries are dropped, not
    # fatal — the curated list only ever holds model-id strings anyway.
    return {
        vendor: tuple(sorted(m for m in models if isinstance(m, str)))
        for vendor, models in raw.items()
        if isinstance(models, list)
    }


_OAUTH_PICKLISTS = _load_oauth_picklists()
# re-sweep (finding 1): .get(vendor, ()) — a seed missing either key disables
# only that vendor's curated list, not the whole wizard.
ANTHROPIC_OAUTH_MODELS: tuple[str, ...] = _OAUTH_PICKLISTS.get("anthropic", ())
OPENAI_OAUTH_MODELS: tuple[str, ...] = _OAUTH_PICKLISTS.get("openai", ())


# === Env-var smart default (slice #23) ===
#
# Map common API hostnames → conventional env var name. Memoizes the
# user-facing convention for the well-known vendors and falls through
# to a hostname-derived default for everything else.

_HOSTNAME_TO_ENV_VAR: dict[str, str] = {
    "api.x.ai": "XAI_API_KEY",
    "api.openai.com": "OPENAI_API_KEY",
    "api.anthropic.com": "ANTHROPIC_API_KEY",
    "openrouter.ai": "OPENROUTER_API_KEY",
    "ollama.com": "OLLAMA_API_KEY",
    "api.groq.com": "GROQ_API_KEY",
    "api.deepseek.com": "DEEPSEEK_API_KEY",
    "api.together.xyz": "TOGETHER_API_KEY",
    "generativelanguage.googleapis.com": "GOOGLE_API_KEY",
}


def default_env_var_for(base_url: str) -> str:
    """Map a base_url hostname to the conventional env var name.

    Known vendors get their published convention from
    ``_HOSTNAME_TO_ENV_VAR``. Unknown hosts derive from the first
    hostname segment (api.deepseek.com → DEEPSEEK_API_KEY,
    groq.com → GROQ_API_KEY). Malformed URLs return a neutral
    placeholder ('API_KEY') so the wizard can still proceed.
    """
    try:
        parsed = urlparse(base_url)
        host = (parsed.hostname or "").lower()
    except Exception:
        host = ""
    if not host:
        return "API_KEY"
    if host in _HOSTNAME_TO_ENV_VAR:
        return _HOSTNAME_TO_ENV_VAR[host]
    # Derive from hostname: 'api.deepseek.com' → 'DEEPSEEK_API_KEY'.
    # Skip 'api'/'www' prefixes; pick the next segment.
    parts = [p for p in host.split(".") if p not in ("api", "www")]
    if not parts:
        return "API_KEY"
    vendor = parts[0]
    # Strip non-alphanum and uppercase.
    cleaned = re.sub(r"[^a-z0-9]", "", vendor).upper()
    if not cleaned:
        return "API_KEY"
    return f"{cleaned}_API_KEY"


def _validate_slug(slug: str) -> str | None:
    if not _SLUG_RE.match(slug):
        return (
            "Entry id must start with a lowercase letter/digit, "
            "contain only lowercase letters / digits / underscores / "
            "hyphens, and be 1–64 chars."
        )
    return None


# === Local model service detection (ports of common stacks) ===

LOCAL_OLLAMA_PROBE_URL = "http://127.0.0.1:11434/api/tags"
LOCAL_OLLAMA_BASE_URL = "http://127.0.0.1:11434/v1"
LM_STUDIO_PROBE_URL = "http://127.0.0.1:1234/v1/models"
LM_STUDIO_BASE_URL = "http://127.0.0.1:1234/v1"
_PROBE_TIMEOUT_SEC = 1.5


def _probe_local_service(url: str) -> bool:
    """Best-effort GET with short timeout. True iff a 2xx response came back."""
    try:
        import httpx
        r = httpx.get(url, timeout=_PROBE_TIMEOUT_SEC)
        return 200 <= r.status_code < 300
    except Exception:
        return False


def has_local_ollama() -> bool:
    return _probe_local_service(LOCAL_OLLAMA_PROBE_URL)


def has_lm_studio() -> bool:
    return _probe_local_service(LM_STUDIO_PROBE_URL)


def _list_local_ollama_models() -> list[str]:
    """Hit Ollama's /api/tags and return the list of loaded model ids.
    Empty list on any failure — caller falls back to free-text entry."""
    try:
        import httpx
        r = httpx.get(LOCAL_OLLAMA_PROBE_URL, timeout=_PROBE_TIMEOUT_SEC)
        if r.status_code != 200:
            return []
        data = r.json()
        out: list[str] = []
        for m in data.get("models", []):
            name = m.get("name") or m.get("model")
            if isinstance(name, str) and name:
                out.append(name)
        return sorted(out)
    except Exception:
        return []


def _list_lm_studio_models() -> list[str]:
    """Hit LM Studio's OpenAI-compat /v1/models endpoint."""
    try:
        import httpx
        r = httpx.get(LM_STUDIO_PROBE_URL, timeout=_PROBE_TIMEOUT_SEC)
        if r.status_code != 200:
            return []
        data = r.json()
        out: list[str] = []
        for m in data.get("data", []):
            mid = m.get("id")
            if isinstance(mid, str) and mid:
                out.append(mid)
        return sorted(out)
    except Exception:
        return []


# === OAuth attribution warning ===
#
# Pro/Max OAuth tokens were issued for the upstream vendor's CLI tools.
# Modulatio reuses them as bearer tokens without identifying as those CLIs.
# Other harnesses that reuse these tokens have similar attribution
# disclaimers in their docs. Vendors may treat third-party use as a
# TOS violation. See feedback_modulatio_oauth_attribution_gap.md.

_OAUTH_WARNING_LINES = (
    "  ⚠ This uses your Pro/Max OAuth token via the underlying API.",
    "    Vendors may treat third-party tool use of subscription credentials",
    "    as a TOS violation; account restrictions have happened to similar",
    "    patterns. For unattended / production workloads, prefer the api_key",
    "    path with a billed API key. See `modulatio doctor` for status.",
)


def _print_oauth_warning() -> None:
    print()
    for line in _OAUTH_WARNING_LINES:
        print(theme.color(line, "warning"))
    print()


# === Quick-add helpers — each creates ONE complete model entry ===

def _next_unique_key(prefix: str) -> str:
    """Return ``prefix`` if free, else ``prefix_2``, ``prefix_3``, etc."""
    presets = model_presets.load_presets()
    if prefix not in presets:
        return prefix
    i = 2
    while f"{prefix}_{i}" in presets:
        i += 1
    return f"{prefix}_{i}"


def _quick_add_anthropic_oauth() -> str | None:
    """One-click registration of an Anthropic-OAuth-backed model.
    Picker over the curated Anthropic model list, with manual-override
    escape for advanced users (model just released, not in catalog)."""
    _print_oauth_warning()
    print()
    print(theme.color("  Pick an Anthropic model:", "primary", bold=True))
    options: list[tuple[str, str]] = [(m, m) for m in ANTHROPIC_OAUTH_MODELS]
    options.append(
        (theme.color("+ Type a model id manually (advanced)", "highlight"), "_manual")
    )
    pick = steps.pick_option(
        "Anthropic OAuth model", options, default_index=0,
    )
    if pick in (steps.BACK, steps.QUIT):
        return None
    if pick == "_manual":
        model_raw = steps.prompt_nav(
            "Model id at api.anthropic.com", required=True,
        )
    else:
        model_raw = pick
    if model_raw in (steps.BACK, steps.QUIT) or not isinstance(model_raw, str):
        return None
    key = _next_unique_key(f"anthropic_{model_raw.replace('.', '_').replace('-', '_')}")
    model_presets.add_preset(
        key,
        label=f"{model_raw} (Anthropic OAuth)",
        base_url="https://api.anthropic.com",
        api_format="anthropic",
        auth_type="oauth_anthropic",
        auth_config={},
        model=model_raw,
    )
    theme.success(f"Registered '{key}' → anthropic/{model_raw} via Claude CLI OAuth.")
    return key


def _quick_add_openai_oauth() -> str | None:
    _print_oauth_warning()
    print()
    print(theme.color("  Pick an OpenAI model:", "primary", bold=True))
    options: list[tuple[str, str]] = [(m, m) for m in OPENAI_OAUTH_MODELS]
    options.append(
        (theme.color("+ Type a model id manually (advanced)", "highlight"), "_manual")
    )
    pick = steps.pick_option(
        "OpenAI Codex OAuth model", options, default_index=0,
    )
    if pick in (steps.BACK, steps.QUIT):
        return None
    if pick == "_manual":
        model_raw = steps.prompt_nav(
            "Model id at api.openai.com", required=True,
        )
    else:
        model_raw = pick
    if model_raw in (steps.BACK, steps.QUIT) or not isinstance(model_raw, str):
        return None
    key = _next_unique_key(f"openai_{model_raw.replace('.', '_').replace('-', '_')}")
    model_presets.add_preset(
        key,
        label=f"{model_raw} (OpenAI Codex OAuth)",
        base_url="https://api.openai.com/v1",
        api_format="openai",
        auth_type="oauth_openai",
        auth_config={},
        model=model_raw,
    )
    theme.success(f"Registered '{key}' → openai/{model_raw} via Codex CLI OAuth.")
    return key


def _quick_add_local_ollama() -> str | None:
    """Probe Ollama's /api/tags for actually-loaded models. If we find any,
    show as a picker. If the list is empty (Ollama running but no models
    pulled, or the probe raced) fall through to free-text entry so the
    user can still register what they expect to pull."""
    models = _list_local_ollama_models()
    if models:
        print()
        print(theme.color("  Pick from the models loaded in your local Ollama:", "primary", bold=True))
        options = [(m, m) for m in models]
        options.append((theme.color("+ Type a model id manually (advanced)", "highlight"), "_manual"))
        pick = steps.pick_option("Local Ollama model", options, default_index=0)
        if pick in (steps.BACK, steps.QUIT):
            return None
        if pick == "_manual":
            model_raw = steps.prompt_nav("Model id", required=True)
        else:
            model_raw = pick
    else:
        theme.warn("Ollama is running but reports no loaded models. Pull one (e.g. `ollama pull llama3.2`) or type the id you intend to use.")
        model_raw = steps.prompt_nav("Model id at the local Ollama", required=True)
    if model_raw in (steps.BACK, steps.QUIT) or not isinstance(model_raw, str):
        return None
    key = _next_unique_key(f"local_{model_raw.replace('.', '_').replace(':', '_').replace('-', '_')}")
    model_presets.add_preset(
        key,
        label=f"{model_raw} (Local Ollama)",
        base_url=LOCAL_OLLAMA_BASE_URL,
        api_format="openai",
        auth_type="none",
        auth_config={},
        model=model_raw,
    )
    theme.success(f"Registered '{key}' → openai/{model_raw} via {LOCAL_OLLAMA_BASE_URL}.")
    return key


def _quick_add_lm_studio() -> str | None:
    """Probe LM Studio's /v1/models for loaded models. Same pattern as Ollama."""
    models = _list_lm_studio_models()
    if models:
        print()
        print(theme.color("  Pick from the models loaded in LM Studio:", "primary", bold=True))
        options = [(m, m) for m in models]
        options.append((theme.color("+ Type a model id manually (advanced)", "highlight"), "_manual"))
        pick = steps.pick_option("LM Studio model", options, default_index=0)
        if pick in (steps.BACK, steps.QUIT):
            return None
        if pick == "_manual":
            model_raw = steps.prompt_nav("Model id", required=True)
        else:
            model_raw = pick
    else:
        theme.warn("LM Studio is running but reports no loaded models. Load one in LM Studio or type the id you intend to use.")
        model_raw = steps.prompt_nav("Model id at LM Studio", required=True)
    if model_raw in (steps.BACK, steps.QUIT) or not isinstance(model_raw, str):
        return None
    key = _next_unique_key(f"lmstudio_{model_raw.replace('.', '_').replace(':', '_').replace('-', '_')}")
    model_presets.add_preset(
        key,
        label=f"{model_raw} (LM Studio)",
        base_url=LM_STUDIO_BASE_URL,
        api_format="openai",
        auth_type="none",
        auth_config={},
        model=model_raw,
    )
    theme.success(f"Registered '{key}' → openai/{model_raw} via {LM_STUDIO_BASE_URL}.")
    return key


# === API-key entry helper ===

def _enter_api_key(env_var: str, staged_keys: dict[str, str]) -> bool:
    import os
    existing = staged_keys.get(env_var) or os.environ.get(env_var)
    if existing:
        masked = existing[:6] + "..." if len(existing) > 9 else "***"
        theme.muted(f"  {env_var} already set ({masked}). Press Enter to keep, or paste a new value.")
    else:
        theme.muted(f"  Paste your {env_var} value. Keys land in <vault>/.env at finalize (chmod 600).")
    try:
        raw = input(theme.prompt_color(f"  {env_var}: ", "highlight")).strip()
    except (EOFError, KeyboardInterrupt):
        return False
    if not raw:
        if existing:
            return True
        theme.warn("Empty input — no key staged.")
        return False
    clean_key, clean_val = steps.sanitize_env_pair(env_var, raw)
    if clean_key is None:
        theme.error("Rejected — invalid env var name or value (newline/null bytes not allowed).")
        return False
    staged_keys[clean_key] = clean_val
    theme.success(f"{env_var} staged.")
    return True


# === Custom add flow — one screen per entry ===

def _custom_add_flow(staged_keys: dict[str, str]) -> str | None:
    print()
    print(theme.color("  Add a model — endpoint, auth, model id. All in one go.", "primary", bold=True))
    print(theme.color("  Type 'b' at any prompt to cancel.", "muted"))
    print()

    def _ask(label: str, *, default: str = "", required: bool = True) -> str | None:
        while True:
            raw = steps.prompt_nav(label, default=default, required=required)
            if raw in (steps.BACK, steps.QUIT):
                return None
            return raw if isinstance(raw, str) else default

    # Entry id is auto-derived from the model id at the end of this flow —
    # no prompt for it. Internal-only identifier; user can rename later via
    # `modulatio models edit` if they care.

    base_url = _ask(
        "Base URL (e.g. https://api.x.ai/v1, https://ollama.com/v1, "
        "http://127.0.0.1:11434/v1)"
    )
    if base_url is None:
        return None

    print()
    print(theme.color("  API compatibility — which API shape does this endpoint speak?", "muted"))
    api_format = steps.pick_option(
        "API format",
        [
            ("openai (OpenAI Chat Completions — most providers)", "openai"),
            ("anthropic (Anthropic Messages API)", "anthropic"),
        ],
        default_index=0,
    )
    if api_format in (steps.BACK, steps.QUIT) or not isinstance(api_format, str):
        return None

    print()
    print(theme.color("  Authentication.", "muted"))
    auth_type = steps.pick_option(
        "Auth type",
        [
            ("none — local endpoint, no auth (Local Ollama, LM Studio, etc.)", "none"),
            ("api_key — paste an API key + env var name", "api_key"),
            ("oauth_anthropic — reuse Claude CLI OAuth (~/.claude/.credentials.json)", "oauth_anthropic"),
            ("oauth_openai — reuse Codex CLI OAuth (~/.codex/auth.json)", "oauth_openai"),
        ],
        default_index=1,
    )
    if auth_type in (steps.BACK, steps.QUIT) or not isinstance(auth_type, str):
        return None

    auth_config: dict[str, Any] = {}
    if auth_type == "api_key":
        # Smart default from base_url hostname so the user doesn't have
        # to remember the convention (slice #23). User can override.
        suggested_env_var = default_env_var_for(base_url)
        env_var = _ask(
            "Env var name for the API key",
            default=suggested_env_var,
        )
        if env_var is None:
            return None
        if not all(c.isalnum() or c == "_" for c in env_var):
            theme.error("Env var name must contain only letters, digits, and underscores.")
            return None
        auth_config = {"env_var": env_var.upper()}
        if not _enter_api_key(env_var.upper(), staged_keys):
            theme.muted("You can enter the key later via the menu.")
    elif auth_type == "oauth_anthropic":
        _print_oauth_warning()
        if not oauth_helpers.has_anthropic_credentials():
            theme.warn("~/.claude/.credentials.json not found. Run `claude login` before using.")
    elif auth_type == "oauth_openai":
        _print_oauth_warning()
        if not oauth_helpers.has_openai_credentials():
            theme.warn("~/.codex/auth.json not found. Run `codex login` before using.")

    model_raw = _ask(
        "Model id at the endpoint (the vendor's identifier — check the provider's docs)"
    )
    if model_raw is None:
        return None

    label = _ask(
        "Display label",
        default=f"{model_raw} ({api_format})",
    )
    if label is None:
        return None

    # Auto-derive the entry id from api_format + sluggified model id.
    # Append _2, _3, ... on collision. User never sees this prompt; they
    # can rename later via `modulatio models edit` if they care.
    base_id = f"{api_format}_{model_raw.replace('.', '_').replace(':', '_').replace('-', '_').replace('/', '_')}"
    eid = _next_unique_key(base_id)

    try:
        model_presets.add_preset(
            eid,
            label=label,
            base_url=base_url,
            api_format=str(api_format),
            auth_type=auth_type,
            auth_config=auth_config,
            model=model_raw,
        )
    except ValueError as e:
        theme.error(str(e))
        return None
    theme.success(f"Registered as '{eid}' → {api_format}/{model_raw}.")
    return eid


# === Status badge + remove flow ===

def _status_badge(key: str, staged_keys: dict[str, str] | None = None) -> str:
    if model_presets.is_available(key, staged_env=staged_keys):
        return theme.color("[ready]", "success")
    p = model_presets.get_preset(key) or {}
    auth_type = p.get("auth_type", "")
    if auth_type == "oauth_anthropic":
        return theme.color("[no Claude creds]", "warning")
    if auth_type == "oauth_openai":
        return theme.color("[no Codex creds]", "warning")
    if auth_type == "api_key":
        return theme.color("[missing key]", "warning")
    return theme.color("[?]", "muted")


def _drop_orphaned_staged_key(
    removed_key: str,
    removed_preset: dict[str, Any],
    surviving: dict[str, Any],
    staged_keys: dict[str, str],
) -> None:
    """re-sweep (F2): when an api_key model is removed, drop its staged env
    var from ``staged_keys`` UNLESS a surviving preset still references that
    same env var (shared key). Keeps finalize from writing an orphaned key to
    ``<vault>/.env`` and listing a phantom provider on the confirm screen."""
    if not staged_keys or not isinstance(removed_preset, dict):
        return
    if removed_preset.get("auth_type") != "api_key":
        return
    auth_config = removed_preset.get("auth_config")
    if not isinstance(auth_config, dict):
        return
    env_var = auth_config.get("env_var")
    if not env_var or env_var not in staged_keys:
        return
    for p in surviving.values():
        if not isinstance(p, dict):
            continue
        ac = p.get("auth_config")
        if isinstance(ac, dict) and ac.get("env_var") == env_var:
            return  # still referenced by a surviving preset — keep it
    staged_keys.pop(env_var, None)


def _remove_flow(staged_keys: dict[str, str] | None = None) -> None:
    presets = model_presets.load_presets()
    if not presets:
        theme.muted("No models to remove.")
        return
    options = [
        (f"{key:24s}  {str(p.get('label') or '')[:40]}", key)
        for key, p in sorted(presets.items())
        if isinstance(p, dict)
    ]
    pick = steps.pick_option("Pick a model to remove", options)
    if pick in (steps.BACK, steps.QUIT) or not isinstance(pick, str):
        return
    if steps.confirm_yn(
        f"Remove '{pick}'? Agents currently bound to it will break until reassigned.",
        default=False,
    ):
        # Capture the preset BEFORE removal so we can unstage an orphaned key.
        removed_preset = presets.get(pick) or {}
        model_presets.remove_preset(pick)
        if staged_keys is not None:
            surviving = {k: v for k, v in presets.items() if k != pick}
            _drop_orphaned_staged_key(pick, removed_preset, surviving, staged_keys)
        theme.success(f"Removed '{pick}'.")


def run(state: dict) -> Any:
    """Execute the combined models step. Mutates state with
    ``staged_api_keys`` (env_var → value) and ``configured_models``
    (sorted list of preset keys) used by step 4 (agents)."""
    staged_keys: dict[str, str] = state.setdefault("staged_api_keys", {})

    while True:
        theme.clear_screen()
        theme.step_header(3, 7, "Configure Models")
        print(theme.color("  Add the models your agents will use.", "muted"))
        print(theme.color("  Each entry holds endpoint + auth + model id — all self-contained.", "muted"))
        print()

        configured = model_presets.load_presets()
        if configured:
            for i, (key, p) in enumerate(sorted(configured.items()), 1):
                # re-sweep (F1): load_presets() returns raw on-disk JSON with
                # no per-preset shape check. Skip a non-dict preset and coerce
                # sliced fields to str so a corrupt/hand-edited file (non-dict
                # value, or model=null) can't crash the render.
                if not isinstance(p, dict):
                    continue
                badge = _status_badge(key, staged_keys)
                label = str(p.get("label") or "")[:30]
                api_format = str(p.get("api_format") or "?")
                model = str(p.get("model") or "?")[:24]
                line = (
                    f"  {theme.color(f'{i:>2}', 'highlight')}) "
                    # Pad the visible key to 24 cols BEFORE coloring so the
                    # column width isn't thrown off by invisible ANSI escapes.
                    f"{theme.color(f'{key:24s}', 'accent', bold=True)}  "
                    f"{label:30s}  "
                    # re-sweep (finding 2, r4): pad api_format to a fixed width
                    # so the model + trailing badge columns stay aligned across
                    # rows whose api_format differs in width ('openai' vs '?').
                    f"→ {api_format:>9s}/{model:24s}  "
                    f"{badge}"
                )
                print(line)
        else:
            print(theme.color("  (no models configured yet)", "muted"))
        print()

        # Quick-add rows — only when their credentials/services exist.
        quick_options: list[tuple[str, str, Any]] = []
        if oauth_helpers.has_anthropic_credentials():
            quick_options.append(("qa", "+ Quick-add Anthropic OAuth model (detected ~/.claude/.credentials.json)", _quick_add_anthropic_oauth))
        if oauth_helpers.has_openai_credentials():
            quick_options.append(("qo", "+ Quick-add OpenAI Codex OAuth model (detected ~/.codex/auth.json)", _quick_add_openai_oauth))
        if has_local_ollama():
            quick_options.append(("ql", "+ Quick-add Local Ollama model (detected at 127.0.0.1:11434)", _quick_add_local_ollama))
        if has_lm_studio():
            quick_options.append(("qs", "+ Quick-add LM Studio model (detected at 127.0.0.1:1234)", _quick_add_lm_studio))
        for code, label, _fn in quick_options:
            print(f"  {theme.color(code, 'highlight')}) {theme.color(label, 'highlight', bold=True)}")
        if quick_options:
            print()
        quick_dispatch = {code: fn for code, _label, fn in quick_options}

        print(f"  {theme.color('a', 'highlight')}) Add a custom model")
        if configured:
            print(f"  {theme.color('r', 'highlight')}) Remove a model")
        floor_met = bool(configured)
        done_label = (
            theme.color("c) Continue — at least one model configured", "success", bold=True)
            if floor_met else
            theme.color("c) Continue (BLOCKED — add at least one model)", "warning")
        )
        print(f"  {done_label}")
        print(steps.nav_hint())

        try:
            raw = input(theme.prompt_color("\n  Choice: ", "highlight")).strip().lower()
        except (EOFError, KeyboardInterrupt):
            return steps.QUIT

        if raw == "q":
            return steps.QUIT
        if raw == "b":
            return steps.BACK
        if raw == "c":
            if not floor_met:
                theme.error("At least one model must be configured.")
                continue
            state["configured_models"] = sorted(configured.keys())
            return "configured"
        if raw in quick_dispatch:
            quick_dispatch[raw]()
            try:
                input(theme.prompt_color("  Press Enter to continue...", "muted"))
            except (EOFError, KeyboardInterrupt):
                return steps.QUIT
            continue
        if raw == "a":
            _custom_add_flow(staged_keys)
            try:
                input(theme.prompt_color("  Press Enter to continue...", "muted"))
            except (EOFError, KeyboardInterrupt):
                return steps.QUIT
            continue
        if raw == "r" and configured:
            _remove_flow(staged_keys)
            try:
                input(theme.prompt_color("  Press Enter to continue...", "muted"))
            except (EOFError, KeyboardInterrupt):
                return steps.QUIT
            continue
        theme.error(f"Unknown choice: {raw}")


__all__ = ["run", "_validate_slug"]
