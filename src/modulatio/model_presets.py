# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Modulatio model registry — self-contained user-curated entries.

Each entry IS a complete model definition: label + endpoint + auth + the
bare model id. The runner reads the entry once at dispatch and assembles
the LiteLLM call (``api_format/model`` prefix, ``api_base``, ``api_key``).

Per locked design (2026-04-26 redesign): no provider/model split. The
prior two-step wizard (providers + models) collapsed into one flow per
user UX feedback ("Feng Shui — minimalist, functionality, flow"). Two
models from the same vendor = two entries; auth fields duplicated by
design. Value-level dedup happens via shared env vars or shared OAuth
credential files, not via shared config rows.

Schema per entry:

.. code-block:: python

    {
        "label":       "<human-readable label>",
        "base_url":    "<provider endpoint>",
        "api_format":  "openai" | "anthropic",
        "auth_type":   "none" | "api_key" | "oauth_anthropic" | "oauth_openai",
        "auth_config": {
            # api_key: {"env_var": "PROVIDER_API_KEY"}
            # oauth_*: {} — credential file path baked into oauth_helpers
            # none:    {}
        },
        "model":       "<bare model id at the endpoint>",
    }

Persistence: ``~/.config/modulatio/model_presets.json``. No built-ins —
Modulatio ships agnostic; users add what they want via setup wizard or
``modulatio models add``.
"""

from __future__ import annotations

import json
import os
from typing import Any

from modulatio import config

PRESETS_FILE = config.CONFIG_DIR / "model_presets.json"

VALID_API_FORMATS = ("openai", "anthropic")


def valid_auth_types() -> tuple[str, ...]:
    """Sorted tuple of currently-registered auth strategies. Derives
    from ``auth_strategies.registered_auth_types()`` so registering a
    new strategy automatically extends preset validation. Slice B-2.
    """
    from modulatio import auth_strategies
    return auth_strategies.registered_auth_types()


# Back-compat alias — module-level constant kept for callers that imported it
# directly. Derived from the registry at import so it reflects the built-in
# strategies (incl. oauth_xai); new code should still call ``valid_auth_types()``
# to also pick up strategies registered dynamically after import.
VALID_AUTH_TYPES = valid_auth_types()


# === Persistence ===

def load_presets() -> dict[str, dict[str, Any]]:
    """Return all user-defined model entries. Empty dict when no config exists."""
    if not PRESETS_FILE.exists():
        return {}
    try:
        data = json.loads(PRESETS_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def save_presets(presets: dict[str, dict[str, Any]]) -> None:
    """Atomic write of the registry. chmod 600 (auth_config may carry env
    var names users consider sensitive)."""
    config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = PRESETS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(presets, indent=2))
    os.replace(tmp, PRESETS_FILE)
    PRESETS_FILE.chmod(0o600)


def add_preset(
    key: str,
    *,
    label: str,
    base_url: str,
    api_format: str,
    auth_type: str,
    model: str,
    auth_config: dict[str, Any] | None = None,
    default_params: dict[str, Any] | None = None,
    model_tier: str | None = None,
    cost_class: str | None = None,
    capability_tags: list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Register a new model entry. Raises ValueError on collision or invalid input.

    ``default_params`` (optional): provider call-kwargs merged into every
    ``litellm.completion`` for this model — e.g.
    ``{"extra_body": {"reasoning": {"enabled": False}}}`` to force a
    reasoning-toggle model thinking-OFF (the producer-role requirement;
    OpenRouter honors this). Lets the preset express the FULL model contract
    — "the human picks any model, including its reasoning mode."

    ``model_tier`` / ``cost_class`` / ``capability_tags`` (optional, Brick 2 of
    the skill-library arc): the model's CAPABILITY surface — what a producer
    bound to this model can do. Stored only when provided; when absent,
    :func:`modulatio.roster._caps_from_model` infers a sensible default from
    the model id (the setup wizard surfaces this as a confirmable "quick tag").
    Capabilities live on the MODEL now, not on assigned skills.
    """
    if api_format not in VALID_API_FORMATS:
        raise ValueError(f"api_format must be one of {VALID_API_FORMATS}, got {api_format!r}")
    valid = valid_auth_types()
    if auth_type not in valid:
        raise ValueError(f"auth_type must be one of {valid}, got {auth_type!r}")
    if default_params is not None and not isinstance(default_params, dict):
        raise ValueError(
            f"default_params must be a dict of completion kwargs, got "
            f"{type(default_params).__name__}"
        )

    presets = load_presets()
    if key in presets:
        raise ValueError(
            f"Model entry '{key}' already exists. "
            f"Use a different key or remove the existing one first."
        )
    entry = {
        "label": label,
        "base_url": base_url,
        "api_format": api_format,
        "auth_type": auth_type,
        "auth_config": auth_config or {},
        "model": model,
    }
    if default_params:
        entry["default_params"] = default_params
    if model_tier:
        entry["model_tier"] = model_tier
    if cost_class:
        entry["cost_class"] = cost_class
    if capability_tags:
        entry["capability_tags"] = [t for t in capability_tags if t]
    presets[key] = entry
    save_presets(presets)
    return entry


def remove_preset(key: str) -> bool:
    """Remove an entry. Returns True if removed, False if absent."""
    presets = load_presets()
    if key not in presets:
        return False
    del presets[key]
    save_presets(presets)
    return True


def update_preset(key: str, **fields: Any) -> dict[str, Any]:
    """Update one or more fields. Re-validates the merged state."""
    presets = load_presets()
    if key not in presets:
        raise KeyError(f"Model entry '{key}' not found.")
    merged = {**presets[key], **fields}
    if merged.get("api_format") not in VALID_API_FORMATS:
        raise ValueError(f"api_format must be one of {VALID_API_FORMATS}")
    valid = valid_auth_types()
    if merged.get("auth_type") not in valid:
        raise ValueError(f"auth_type must be one of {valid}")
    presets[key] = merged
    save_presets(presets)
    return merged


def get_preset(key: str) -> dict[str, Any] | None:
    return load_presets().get(key)


def is_available(key: str, *, staged_env: dict[str, str] | None = None) -> bool:
    """True if the entry's auth is currently resolvable.

    Slice B-2: dispatches through the strategy registry rather than
    per-auth_type branches here. ``staged_env`` is forwarded as the
    strategy's ``extra_env`` (api-key strategy uses it for the
    wizard's mid-flight check; other strategies ignore it).

    Does NOT check token validity — that requires a network call.
    """
    from modulatio import auth_strategies

    p = get_preset(key)
    if not p:
        return False
    try:
        strategy = auth_strategies.build_strategy(
            p.get("auth_type", "none"),
            p.get("auth_config") or {},
        )
    except ValueError:
        return False
    return strategy.is_available(extra_env=staged_env)


__all__ = [
    "PRESETS_FILE",
    "VALID_API_FORMATS",
    "VALID_AUTH_TYPES",
    "load_presets",
    "save_presets",
    "add_preset",
    "remove_preset",
    "update_preset",
    "get_preset",
    "is_available",
]
