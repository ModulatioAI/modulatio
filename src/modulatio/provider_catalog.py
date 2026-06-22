# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Provider catalog — the data layer behind the Configuration tab.

A ``Provider`` describes a model source (OpenRouter, xAI, Anthropic, …) richly
enough that the configurator can do everything *for* the operator: it carries
the base_url, api_format, auth options, signup URL, and how to list models. The
operator's only manual input is the **key** — model id, base_url, and venv are
always picked or auto-filled, never typed.

A ``CatalogModel`` is one model discovered from a provider (live-fetched from
its ``/models`` endpoint or, for OAuth providers, a curated picklist), carrying
just what the picker needs: id, name, context, and whether it's **free**.

Picking a provider + model feeds the existing clean backend —
``model_presets.add_preset(base_url=…, api_format=…, auth_type=…, model=…)`` —
via :func:`preset_kwargs`. The catalog never reimplements preset storage.

This module is the OpenRouter slice; other providers (xAI, Anthropic, OpenAI,
NVIDIA, Ollama Cloud, the locals, and custom) register into ``PROVIDERS`` the
same way as we build them out.
"""
from __future__ import annotations

import json
import re
import urllib.request
from typing import Literal, Optional

from pydantic import BaseModel, Field


# ── types ───────────────────────────────────────────────────────────────────


class AuthOption(BaseModel):
    """One way to authenticate to a provider. A provider may offer several
    (e.g. OpenAI/Anthropic carry *both* their OAuth method and a plain key)."""

    auth_type: Literal[
        "api_key", "oauth_anthropic", "oauth_openai", "oauth_xai", "claude_cli", "none"
    ]
    label: str  # "API key", "Sign in with Claude (OAuth)", "No auth (local)"
    env_var: Optional[str] = None  # for api_key: the env var the key lands in
    oauth_hint: Optional[str] = None  # for oauth: e.g. "run `claude login`"
    beta: bool = False  # surfaced as "(beta)" in the picker


Modality = Literal["text", "image", "video", "audio", "embedding"]


def classify_modality(model_id: str) -> Modality:
    """Infer a model's modality from its id — for providers that mix modalities
    in one /models list (Google returns chat, image, video, tts, embedding all
    together). Role assignment filters to ``text``; the rest are catalogued in
    their own sections."""
    i = model_id.lower()
    if "tts" in i:
        return "audio"
    if "veo" in i:
        return "video"
    if "image" in i or "imagen" in i:
        return "image"
    if "embed" in i:
        return "embedding"
    return "text"


class ModelsSource(BaseModel):
    """How to discover a provider's models. A provider may have several — one
    per modality (xAI splits text / image / video into separate endpoints)."""

    kind: Literal["api", "picklist", "local_probe", "custom"]
    endpoint: Optional[str] = None  # api: path under base_url, e.g. "/models"
    auth_required: bool = False  # api: does *listing* need the key?
    modality: Modality = "text"  # what kind of models this source lists
    picklist_key: Optional[str] = None  # picklist: key into the seed picklist
    probe_url: Optional[str] = None  # local_probe: where to probe


class Provider(BaseModel):
    """A model source the configurator can fully drive."""

    id: str
    name: str
    base_url: str
    api_format: Literal["openai", "anthropic"]
    #: Request endpoint dialect carried onto presets: "chat" (default), "responses"
    #: (xAI multi-agent / o1), or "codex" (GPT-5.5 via the Codex subscription —
    #: the ChatGPT-backend Responses API with tool-calling).
    request_endpoint: Optional[str] = None
    auth_options: list[AuthOption]
    models_source: ModelsSource  # the primary (text) source
    extra_sources: list[ModelsSource] = Field(default_factory=list)  # image/video/…
    signup_url: Optional[str] = None  # "go here to get a key"
    free_detect: Literal["pricing_zero", "suffix_free", "all", "none"] = "none"
    # Truthful caveat shown on the free section — free is rate-limited, not
    # unlimited, and limits vary by model. Per-model limits aren't in any feed,
    # so this is a provider-level note (annotate specific models by hand if we
    # ever get the numbers).
    free_note: Optional[str] = None
    pinned_models: list[str] = Field(default_factory=list)  # always shown first
    # Some providers' /models list ids with a prefix the chat endpoint rejects
    # (Gemini returns "models/gemini-2.5-flash" but wants "gemini-2.5-flash").
    id_prefix_strip: Optional[str] = None
    # Providers that return mixed modalities in one list (Google) classify each
    # model's modality from its id instead of trusting the source's tag.
    infer_modality_by_id: bool = False
    venv_wrapper: Optional[str] = None
    notes: Optional[str] = None


class CatalogModel(BaseModel):
    """One model discovered from a provider — what the picker needs."""

    id: str
    name: str
    provider_id: str
    modality: Modality = "text"  # text chat/completion, or image/video/audio
    context_length: Optional[int] = None
    is_free: bool = False
    created: Optional[int] = None  # provider's unix ts, for recency sorting
    capability_tags: list[str] = Field(default_factory=list)


# ── provider registry (built out one provider at a time) ─────────────────────


OPENROUTER = Provider(
    id="openrouter",
    name="OpenRouter",
    base_url="https://openrouter.ai/api/v1",
    api_format="openai",
    auth_options=[
        AuthOption(
            auth_type="api_key", label="API key", env_var="OPENROUTER_API_KEY"
        )
    ],
    models_source=ModelsSource(kind="api", endpoint="/models", auth_required=False),
    signup_url="https://openrouter.ai/keys",
    free_detect="pricing_zero",
    free_note="Free models are rate-limited; some have daily request caps.",
    pinned_models=["openrouter/auto", "openrouter/free"],
    notes="One key, 340+ models across providers; ~25 free.",
)

OLLAMA_CLOUD = Provider(
    id="ollama_cloud",
    name="Ollama Cloud",
    base_url="https://ollama.com/v1",
    api_format="openai",
    auth_options=[
        AuthOption(auth_type="api_key", label="API key", env_var="OLLAMA_API_KEY")
    ],
    models_source=ModelsSource(kind="api", endpoint="/models", auth_required=False),
    signup_url="https://ollama.com/settings/keys",
    # Cloud's /models has no pricing; every Cloud model runs on the free tier
    # (rate-limited, not per-model priced), so all are free-to-use.
    free_detect="all",
    free_note=(
        "Free tier — session + weekly limits metered by GPU-time (varies by "
        "model size), reset on 5-hour and 7-day cycles."
    ),
    notes="Hosted Ollama models, OpenAI-compatible; free tier is rate-limited.",
)

XAI = Provider(
    id="xai",
    name="xAI",
    base_url="https://api.x.ai/v1",
    api_format="openai",
    auth_options=[
        AuthOption(auth_type="api_key", label="API key", env_var="XAI_API_KEY"),
        # Grok OAuth (SuperGrok / X Premium+) via the official Grok CLI's
        # ~/.grok/auth.json. Beta — pending live validation.
        AuthOption(
            auth_type="oauth_xai",
            label="Sign in with Grok (OAuth)",
            oauth_hint="install + log in with the Grok CLI (x.ai/cli)",
            beta=True,
        ),
    ],
    # xAI splits modalities across endpoints; listing any of them needs the key.
    models_source=ModelsSource(
        kind="api", endpoint="/language-models", auth_required=True, modality="text"
    ),
    extra_sources=[
        ModelsSource(
            kind="api", endpoint="/image-generation-models",
            auth_required=True, modality="image",
        ),
        ModelsSource(
            kind="api", endpoint="/video-generation-models",
            auth_required=True, modality="video",
        ),
    ],
    signup_url="https://console.x.ai",
    free_detect="none",  # no free tier
    notes=(
        "Grok text + image + video (grok-imagine). No free tier. Voice exists "
        "in product but isn't a listable API endpoint, so it's not catalogued."
    ),
)

ANTHROPIC = Provider(
    id="anthropic",
    name="Anthropic",
    base_url="https://api.anthropic.com",
    api_format="anthropic",
    # API key only. The subscription path is Clay (the CLAUDE_CLI provider,
    # `claude -p`) — straight OAuth against api.anthropic.com 401s a
    # subscription token, so it is NOT offered here.
    auth_options=[
        AuthOption(auth_type="api_key", label="API key", env_var="ANTHROPIC_API_KEY"),
    ],
    # API-key users hit /v1/models, but to keep the picker key-free we list from
    # the curated seed (small, hand-maintained — Anthropic's catalog is short).
    models_source=ModelsSource(
        kind="picklist", picklist_key="anthropic", modality="text"
    ),
    signup_url="https://console.anthropic.com/settings/keys",
    free_detect="none",  # no free tier
    notes="Claude models (text). OAuth or API key. No free tier.",
)

OPENAI = Provider(
    id="openai",
    name="OpenAI",
    base_url="https://api.openai.com/v1",
    api_format="openai",
    # API key only. The subscription path is the OPENAI_CODEX provider (the
    # ChatGPT/Codex backend) — straight OAuth against api.openai.com 401s a
    # subscription token, so it is NOT offered here.
    auth_options=[
        AuthOption(auth_type="api_key", label="API key", env_var="OPENAI_API_KEY"),
    ],
    # Key-free picker: list from the curated picklist seed (a maintained general
    # snapshot, refreshed as OpenAI ships).
    models_source=ModelsSource(
        kind="picklist", picklist_key="openai", modality="text"
    ),
    signup_url="https://platform.openai.com/api-keys",
    free_detect="none",  # no free tier
    notes="GPT models (text). ChatGPT/Codex OAuth or API key. No free tier.",
)

# Codex (GPT-5.5) reasoning-effort choices, highest→lowest. The ChatGPT backend
# defaults to very high effort, which causes heavy reasoning-token bloat (slow,
# over-long runs) — MEDIUM is the recommended default. Stored on a preset as
# ``default_params={"reasoning": {"effort": <choice>}}``; the runner falls back
# to MEDIUM when a preset carries no choice.
CODEX_REASONING_EFFORTS: tuple[str, ...] = ("xhigh", "high", "medium", "low")
CODEX_DEFAULT_EFFORT = "medium"


def codex_reasoning_params(effort: str) -> dict:
    """The ``default_params`` payload that pins a Codex preset's reasoning effort."""
    return {"reasoning": {"effort": effort}}


OPENAI_CODEX = Provider(
    id="openai_codex",
    name="OpenAI Codex (subscription)",
    # The ChatGPT/Codex backend (Responses API) — NOT the metered api.openai.com,
    # which has no subscription quota. ``request_endpoint="codex"`` routes presets
    # to the Codex Responses tool-loop with the chatgpt-account-id header.
    base_url="https://chatgpt.com/backend-api/codex",
    api_format="openai",
    request_endpoint="codex",
    # Subscription access is OAuth-only (sign in via `codex login`).
    auth_options=[
        AuthOption(
            auth_type="oauth_openai",
            label="Sign in with ChatGPT (OAuth)",
            oauth_hint="run `codex login`",
        ),
    ],
    models_source=ModelsSource(
        kind="picklist", picklist_key="openai_codex", modality="text"
    ),
    signup_url="https://chatgpt.com/codex",
    free_detect="none",
    notes="GPT-5.5 via the ChatGPT/Codex subscription (OAuth, ChatGPT backend). "
          "Separate from the metered OpenAI API.",
)

CLAUDE_CLI = Provider(
    id="claude_cli",
    name="Clay — Claude avatar (Claude Code subscription)",
    # Reached by spawning the official `claude -p` binary through the harness —
    # NOT the metered api.anthropic.com and NOT the OAuth token. Additive: the
    # `anthropic` API-key provider stays intact.
    base_url="claude-cli",
    api_format="anthropic",
    request_endpoint="claude_cli",
    auth_options=[
        AuthOption(
            auth_type="claude_cli",
            label="Claude Code subscription (run `claude` to sign in)",
            oauth_hint="install Claude Code, then run `claude`",
        ),
    ],
    models_source=ModelsSource(
        kind="picklist", picklist_key="claude_cli", modality="text"
    ),
    signup_url="https://claude.com/product/claude-code",
    free_detect="none",
    notes="Clay is a Claude model running through your Claude Code subscription "
          "(claude -p). Separate from the metered Anthropic API.",
)

OPENAI_CODEX = Provider(
    id="openai_codex",
    name="OpenAI Codex (subscription)",
    # The ChatGPT/Codex backend (Responses API) — NOT the metered api.openai.com,
    # which has no subscription quota. ``request_endpoint="codex"`` routes presets
    # to the Codex Responses tool-loop with the chatgpt-account-id header.
    base_url="https://chatgpt.com/backend-api/codex",
    api_format="openai",
    request_endpoint="codex",
    # Subscription access is OAuth-only (sign in via `codex login`).
    auth_options=[
        AuthOption(
            auth_type="oauth_openai",
            label="Sign in with ChatGPT (OAuth)",
            oauth_hint="run `codex login`",
        ),
    ],
    models_source=ModelsSource(
        kind="picklist", picklist_key="openai_codex", modality="text"
    ),
    signup_url="https://chatgpt.com/codex",
    free_detect="none",
    notes="GPT-5.5 via the ChatGPT/Codex subscription (OAuth, ChatGPT backend). "
          "Separate from the metered OpenAI API.",
)

CLAUDE_CLI = Provider(
    id="claude_cli",
    name="Clay — Claude avatar (Claude Code subscription)",
    # Reached by spawning the official `claude -p` binary through the harness —
    # NOT the metered api.anthropic.com and NOT the OAuth token. Additive: the
    # `anthropic` API-key provider stays intact.
    base_url="claude-cli",
    api_format="anthropic",
    request_endpoint="claude_cli",
    auth_options=[
        AuthOption(
            auth_type="claude_cli",
            label="Claude Code subscription (run `claude` to sign in)",
            oauth_hint="install Claude Code, then run `claude`",
        ),
    ],
    models_source=ModelsSource(
        kind="picklist", picklist_key="claude_cli", modality="text"
    ),
    signup_url="https://claude.com/product/claude-code",
    free_detect="none",
    notes="Clay is a Claude model running through your Claude Code subscription "
          "(claude -p). Separate from the metered Anthropic API.",
)

NVIDIA = Provider(
    id="nvidia",
    name="NVIDIA",
    base_url="https://integrate.api.nvidia.com/v1",
    api_format="openai",
    auth_options=[
        AuthOption(auth_type="api_key", label="API key", env_var="NVIDIA_API_KEY")
    ],
    # The API-catalog /models list is public; using a model needs the key.
    models_source=ModelsSource(
        kind="api", endpoint="/models", auth_required=False, modality="text"
    ),
    signup_url="https://build.nvidia.com",
    # NVIDIA's catalog tier is free across the board, but tightly throttled.
    free_detect="all",
    free_note="Free via the NVIDIA API catalog — rate-limited to 40 requests/min.",
    notes="NVIDIA-hosted open models (OpenAI-compatible). Free catalog tier, 40 RPM.",
)

GOOGLE = Provider(
    id="google",
    name="Google (Gemini)",
    # The OpenAI-compatible Gemini endpoint — Bearer auth, standard /models.
    base_url="https://generativelanguage.googleapis.com/v1beta/openai",
    api_format="openai",
    auth_options=[
        AuthOption(auth_type="api_key", label="API key", env_var="GEMINI_API_KEY")
    ],
    models_source=ModelsSource(
        kind="api", endpoint="/models", auth_required=True, modality="text"
    ),
    signup_url="https://aistudio.google.com/apikey",
    # Gemini's /models lists "models/gemini-…" but the chat endpoint wants the
    # bare id — strip the prefix so registered presets actually resolve.
    id_prefix_strip="models/",
    # One list mixes chat / image / video / tts / embedding — classify per id.
    infer_modality_by_id=True,
    # AI Studio has a real free tier, but quotas vary by model and some models
    # can require billing — the caveat carries the honesty the feed can't.
    free_detect="all",
    free_note=(
        "Free tier — daily request/token quotas that vary by model (flash most "
        "generous); some models may require billing."
    ),
    notes="Gemini via the OpenAI-compatible endpoint. Free AI Studio tier (quota-limited).",
)

_LOCAL_FREE_NOTE = (
    "Runs on your machine — no API cost or rate limit; bounded by your VRAM."
)


def _local(id_: str, name: str, port: int) -> Provider:
    base = f"http://localhost:{port}/v1"
    return Provider(
        id=id_,
        name=name,
        base_url=base,
        api_format="openai",
        auth_options=[AuthOption(auth_type="none", label="No auth (local)")],
        # Probe the running server for whatever the user has loaded/pulled.
        models_source=ModelsSource(
            kind="local_probe", probe_url=f"{base}/models", modality="text"
        ),
        free_detect="all",
        free_note=_LOCAL_FREE_NOTE,
        notes=f"Local {name} server (OpenAI-compatible). Lists loaded models.",
    )


OLLAMA_LOCAL = _local("ollama_local", "Ollama (local)", 11434)
LM_STUDIO = _local("lm_studio", "LM Studio (local)", 1234)
LLAMA_CPP = _local("llama_cpp", "llama.cpp (local)", 8080)

CUSTOM = Provider(
    id="custom",
    name="Custom / Other",
    base_url="",  # the operator supplies it
    api_format="openai",
    # Default to no-auth (many custom endpoints are local/keyless); the operator
    # can switch to a key and name its env var.
    auth_options=[
        AuthOption(auth_type="none", label="No auth"),
        AuthOption(auth_type="api_key", label="API key"),
    ],
    # No catalog to list — the operator types the model id directly.
    models_source=ModelsSource(kind="custom"),
    notes=(
        "Any OpenAI- or Anthropic-compatible endpoint — you provide the "
        "base_url, model id, and key."
    ),
)

PROVIDERS: dict[str, Provider] = {
    OPENROUTER.id: OPENROUTER,
    OLLAMA_CLOUD.id: OLLAMA_CLOUD,
    XAI.id: XAI,
    ANTHROPIC.id: ANTHROPIC,
    OPENAI.id: OPENAI,
    OPENAI_CODEX.id: OPENAI_CODEX,
    CLAUDE_CLI.id: CLAUDE_CLI,
    NVIDIA.id: NVIDIA,
    GOOGLE.id: GOOGLE,
    OLLAMA_LOCAL.id: OLLAMA_LOCAL,
    LM_STUDIO.id: LM_STUDIO,
    LLAMA_CPP.id: LLAMA_CPP,
    CUSTOM.id: CUSTOM,
}

# Flagship model-family stems that always surface in the curated default view,
# even when the long tail is hidden behind search. Bare stems (no vendor slug)
# so they match both "anthropic/claude-…" (OpenRouter) and "claude-…" /
# "deepseek-v4-pro" (Ollama Cloud) — i.e. cross-provider.
_FLAGSHIP_FAMILIES: tuple[str, ...] = (
    "claude",
    "gpt",
    "gemini",
    "grok",
    "deepseek",
    "qwen",
    "llama",
    "mistral",
    "ministral",
    "kimi",
    "moonshot",
    "nemotron",
    "gemma",
    "minimax",
    "glm",
    "gpt-oss",
)


def get_provider(provider_id: str) -> Optional[Provider]:
    return PROVIDERS.get(provider_id)


def list_providers() -> list[Provider]:
    return list(PROVIDERS.values())


def provider_name_for_base_url(base_url: "str | None") -> str:
    """Best-effort display name of the provider serving ``base_url`` — matched by
    host against the catalog, else ``local`` for loopback, else the bare host.
    Display-only (no network); used by the fallback picker's Provider column."""
    if not base_url:
        return "—"
    from urllib.parse import urlparse
    host = (urlparse(base_url).hostname or base_url).lower()
    if host in ("127.0.0.1", "localhost", "0.0.0.0", "::1"):
        return "local"
    for p in list_providers():
        ph = (urlparse(p.base_url).hostname or "").lower()
        if ph and ph == host:
            return p.name
    return host


# ── model discovery (pure parse + thin HTTP wrapper) ─────────────────────────


def _price_is_zero(value: object) -> bool:
    return str(value) in ("0", "0.0", "0.00")


# Pricing fields that, when present and non-zero, mean the model bills the
# operator even with zero prompt/completion rates (per-request, per-image, etc.).
# A missing/None field is treated as zero (most models omit the ones they don't
# charge for).
_EXTRA_PRICE_FIELDS = ("request", "image", "web_search", "internal_reasoning")


def _is_free(model: dict, free_detect: str) -> bool:
    if free_detect == "pricing_zero":
        p = model.get("pricing")
        # A provider feed can return a truthy non-dict pricing (e.g. the string
        # "free"); guard the type so one malformed entry doesn't abort the whole
        # catalog parse on a downstream .get().
        p = p if isinstance(p, dict) else {}
        if not (_price_is_zero(p.get("prompt")) and _price_is_zero(p.get("completion"))):
            return False
        # A zero token rate isn't truly free if another billed dimension is set.
        return all(
            p.get(f) is None or _price_is_zero(p.get(f)) for f in _EXTRA_PRICE_FIELDS
        )
    if free_detect == "suffix_free":
        return str(model.get("id", "")).endswith(":free")
    if free_detect == "all":
        return True
    return False


def _coerce_int(value: object) -> Optional[int]:
    """Best-effort int from a provider feed (handles numeric strings/floats);
    a non-numeric value (e.g. created='2024-01-01', context_length='128k')
    becomes None rather than aborting the whole catalog with a ValidationError."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def parse_models(
    provider: Provider, payload: object, *, modality: Modality = "text"
) -> list[CatalogModel]:
    """Turn a provider's ``/models`` JSON into CatalogModels (pure/testable).
    ``modality`` tags the batch — image/video endpoints pass their own."""
    if isinstance(payload, dict):
        raw = payload.get("data", payload.get("models", payload))
    else:
        raw = payload
    # A provider's /models can return an error envelope ({"error": {...}}) with
    # HTTP 200, a scalar, or a malformed feed. Only a list of dicts is parseable;
    # anything else degrades to an empty catalog rather than crashing.
    if not isinstance(raw, list):
        return []
    out: list[CatalogModel] = []
    strip = provider.id_prefix_strip
    for m in raw:
        if not isinstance(m, dict):
            continue
        mid = m.get("id")
        # A truthy non-string id (e.g. an int) would crash .startswith() with a
        # strip set, or fail CatalogModel(id=...) pydantic validation otherwise.
        # Skip it so one malformed entry doesn't abort the whole batch.
        if not isinstance(mid, str) or not mid:
            continue
        if strip and mid.startswith(strip):
            mid = mid[len(strip):]
        mod = classify_modality(mid) if provider.infer_modality_by_id else modality
        name = m.get("name")
        out.append(
            CatalogModel(
                id=mid,
                name=name if isinstance(name, str) and name else mid,
                provider_id=provider.id,
                modality=mod,
                context_length=_coerce_int(m.get("context_length")),
                is_free=_is_free(m, provider.free_detect),
                created=_coerce_int(m.get("created")),
            )
        )
    return out


def apply_pinned(
    provider: Provider, models: list[CatalogModel]
) -> list[CatalogModel]:
    """Ensure the provider's pinned ids exist + lead the list (e.g.
    ``openrouter/auto`` + ``openrouter/free``), synthesizing a stub if the feed
    didn't include one."""
    by_id = {m.id: m for m in models}
    pinned: list[CatalogModel] = []
    for pid in provider.pinned_models:
        pinned.append(
            by_id.get(pid)
            or CatalogModel(id=pid, name=pid, provider_id=provider.id)
        )
    rest = [m for m in models if m.id not in provider.pinned_models]
    return pinned + rest


def _http_get_json(url: str, headers: dict[str, str], timeout: float) -> object:
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return json.loads(resp.read().decode("utf-8"))


def _load_picklist(picklist_key: str) -> list[str]:
    """Read a curated model id list from the seed picklist (used by OAuth
    providers like Anthropic that can't be live-listed with an API key)."""
    from pathlib import Path

    path = Path(__file__).parent / "_seed_data" / "oauth_model_picklists.json"
    # A missing/corrupt seed file degrades to an empty picklist (the picker just
    # shows nothing for that provider) rather than crashing the whole fetch —
    # mirroring local_probe's graceful-empty pattern.
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    # re-sweep: a corrupt seed value that isn't a list (a str/dict for a provider
    # key) must degrade to empty — never `list("claude-opus-4-8")` → one model id
    # per character, nor a dict's keys. Keep only the well-formed str entries.
    v = data.get(picklist_key)
    return [s for s in v if isinstance(s, str)] if isinstance(v, list) else []


def _fetch_source(
    provider: Provider, src: ModelsSource, api_key: Optional[str], timeout: float
) -> list[CatalogModel]:
    if src.kind == "api":
        url = provider.base_url.rstrip("/") + (src.endpoint or "/models")
        headers: dict[str, str] = {}
        if src.auth_required and api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        payload = _http_get_json(url, headers, timeout)
        return parse_models(provider, payload, modality=src.modality)
    if src.kind == "picklist" and src.picklist_key:
        ids = _load_picklist(src.picklist_key)
        payload = {"data": [{"id": i} for i in ids]}
        return parse_models(provider, payload, modality=src.modality)
    if src.kind == "local_probe":
        url = src.probe_url or (provider.base_url.rstrip("/") + "/models")
        try:
            payload = _http_get_json(url, {}, min(timeout, 5.0))
        except Exception:
            return []  # local server not running / unreachable — empty, no error
        return parse_models(provider, payload, modality=src.modality)
    return []  # custom: no listing — the operator enters the model id


def auth_status(
    auth: AuthOption, *, extra_env: Optional[dict[str, str]] = None
) -> tuple[bool, str]:
    """Is this auth option ready, and if not, how to set it up?

    Ties the catalog's auth options to the engine's auth strategies — so the
    configurator can show "OAuth ready ✓" or the setup hint ("run `claude
    login`" / "set OPENROUTER_API_KEY"). Returns ``(available, hint)``; hint is
    empty when ready."""
    from modulatio import auth_strategies

    cfg = {"env_var": auth.env_var} if auth.env_var else None
    try:
        strat = auth_strategies.build_strategy(auth.auth_type, cfg)
    except Exception:
        return False, auth.oauth_hint or ""
    ok = strat.is_available(extra_env)
    return ok, ("" if ok else strat.fix_hint())


def fetch_models(
    provider: Provider, *, api_key: Optional[str] = None, timeout: float = 30.0
) -> list[CatalogModel]:
    """Fetch a provider's model list across all its sources — free-flagged,
    modality-tagged, pinned.

    Sources are ``api`` (live ``/models``, public or key-gated) or ``picklist``
    (a curated seed list, for OAuth providers that can't be live-listed).
    Listing needs the key only when an ``api`` source's ``auth_required`` is set
    (OpenRouter/Ollama are public; xAI needs the key; Anthropic uses a picklist)."""
    all_models: list[CatalogModel] = []
    for src in [provider.models_source, *provider.extra_sources]:
        all_models.extend(_fetch_source(provider, src, api_key, timeout))
    return apply_pinned(provider, all_models)


# ── picker helpers: free / curated-default / search ──────────────────────────


def of_modality(models: list[CatalogModel], modality: Modality) -> list[CatalogModel]:
    """Filter by modality — role assignment (leader/qc/producer) wants
    ``"text"``; the image/video/voice listings are browsed separately."""
    return [m for m in models if m.modality == modality]


def free_models(models: list[CatalogModel]) -> list[CatalogModel]:
    """Every free model — the picker's clearly-flagged free section shows them
    all (not capped)."""
    return [m for m in models if m.is_free]


def curated_default(
    provider: Provider, models: list[CatalogModel], limit: int = 30
) -> list[CatalogModel]:
    """The default (unsearched) view: pinned first, then flagship families, then
    the newest of the rest — capped at ``limit``. Search reaches everything
    else; this is a default, not a wall."""
    pins = provider.pinned_models
    by_id = {m.id: m for m in models}
    # Keep every pin (in declared order), synthesizing a stub for any the feed
    # dropped — consistent with apply_pinned, so a pin can't silently vanish.
    pinned = [
        by_id.get(pid) or CatalogModel(id=pid, name=pid, provider_id=provider.id)
        for pid in pins
    ]
    rest = [m for m in models if m.id not in pins]

    def is_flagship(m: CatalogModel) -> bool:
        return any(fam in m.id for fam in _FLAGSHIP_FAMILIES)

    flagships = sorted(
        (m for m in rest if is_flagship(m)),
        key=lambda m: m.created or 0,
        reverse=True,
    )
    others = sorted(
        (m for m in rest if not is_flagship(m)),
        key=lambda m: m.created or 0,
        reverse=True,
    )
    return (pinned + flagships + others)[:limit]


def search(models: list[CatalogModel], query: str) -> list[CatalogModel]:
    """Filter by id or name substring — the escape hatch to the full catalog."""
    q = query.strip().lower()
    if not q:
        return list(models)
    return [m for m in models if q in m.id.lower() or q in m.name.lower()]


# ── catalog → preset (feeds the existing backend) ────────────────────────────


def _slug(provider: Provider, model: CatalogModel) -> str:
    raw = f"{provider.id}_{model.id}"
    return re.sub(r"[^a-z0-9]+", "_", raw.lower()).strip("_")


def preset_kwargs(
    provider: Provider,
    model: CatalogModel,
    auth: AuthOption,
    *,
    key: Optional[str] = None,
    pool: bool = False,
    capability_tags: Optional[list[str]] = None,
    model_tier: Optional[str] = None,
    cost_class: Optional[str] = None,
) -> dict:
    """Build ``model_presets.add_preset(**kwargs)`` from a catalog pick —
    everything auto-filled from the provider; the operator supplied only the API
    key (stored separately by the key step).

    Capabilities are left UNSET unless explicitly passed: an empty preset lets
    ``model_capabilities`` infer caps from the model id at agent-creation (auto
    for known families, and they stay current as the family table grows). Pass
    ``capability_tags`` / ``model_tier`` / ``cost_class`` only to *override* the
    inference — the configurator's add-model step surfaces the inferred values
    as an editable default and writes them back here when the operator changes
    them."""
    auth_config: Optional[dict] = None
    if auth.auth_type == "api_key" and auth.env_var:
        auth_config = {"env_var": auth.env_var}
        if pool:  # rotate across the provider's numbered keys at call time
            auth_config["pool"] = True
    elif pool:
        # re-sweep: pooling derives numbered variants (FOO_API_KEY_2, …) from a
        # named base env var. Without one (e.g. CUSTOM's keyed AuthOption has
        # env_var=None, or a non-api_key auth) the request can't be honored —
        # fail loud rather than silently drop it and surprise the operator.
        raise ValueError(
            "pool=True requires an api_key AuthOption with a named env_var; "
            f"got auth_type={auth.auth_type!r} env_var={auth.env_var!r}"
        )
    kwargs = dict(
        key=key or _slug(provider, model),
        label=model.name,
        base_url=provider.base_url,
        api_format=provider.api_format,
        auth_type=auth.auth_type,
        auth_config=auth_config,
        model=model.id,
    )
    if capability_tags is not None:
        kwargs["capability_tags"] = capability_tags
    if model_tier is not None:
        kwargs["model_tier"] = model_tier
    if cost_class is not None:
        kwargs["cost_class"] = cost_class
    if provider.request_endpoint:
        kwargs["endpoint"] = provider.request_endpoint
    return kwargs


__all__ = [
    "AuthOption",
    "ModelsSource",
    "Provider",
    "CatalogModel",
    "PROVIDERS",
    "OPENROUTER",
    "OLLAMA_CLOUD",
    "XAI",
    "ANTHROPIC",
    "OPENAI",
    "OPENAI_CODEX",
    "CLAUDE_CLI",
    "NVIDIA",
    "GOOGLE",
    "OLLAMA_LOCAL",
    "LM_STUDIO",
    "LLAMA_CPP",
    "CUSTOM",
    "Modality",
    "classify_modality",
    "get_provider",
    "list_providers",
    "parse_models",
    "apply_pinned",
    "fetch_models",
    "auth_status",
    "of_modality",
    "free_models",
    "curated_default",
    "search",
    "preset_kwargs",
]
