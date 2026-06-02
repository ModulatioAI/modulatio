# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Multiple API keys per provider — number them, label them, pick one.

A provider has a base env var (``GEMINI_API_KEY``). Additional keys live under
numbered variants (``GEMINI_API_KEY_2``, ``_3`, … — **no cap**). Each may carry
a short human label so the operator can pick "Key #2 · images" without ever
seeing the value. This is how you run different keys for text / images / web
search and let the vendor meter each separately.

The key *values* live in the vault ``.env`` (via ``config.set_env_secret``);
only the **labels** are stored here (``key_labels.json`` — not secret). The
configurator's key selector and the agent builder both read ``list_keys`` and
register a preset against the chosen slot's ``env_var``. Key #1 is the default.
"""
from __future__ import annotations

import json
import os
from typing import Optional, TypedDict

from modulatio import config

LABELS_FILE = config.CONFIG_DIR / "key_labels.json"  # {env_var: label}


class KeySlot(TypedDict):
    index: int
    env_var: str
    label: Optional[str]
    is_set: bool


def _load_labels() -> dict[str, str]:
    if LABELS_FILE.exists():
        try:
            data = json.loads(LABELS_FILE.read_text())
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}
    return {}


def _save_labels(labels: dict[str, str]) -> None:
    config.write_secret_file(LABELS_FILE, json.dumps(labels, indent=2))


def env_var_for(base_env_var: str, index: int) -> str:
    """The env var for key #index — #1 is the base var, #N is ``<base>_N``."""
    return base_env_var if index == 1 else f"{base_env_var}_{index}"


def list_keys(base_env_var: str) -> list[KeySlot]:
    """Every configured key for a provider (by its base env var), sorted by
    index. #1 is the base var; #2.. are ``<base>_2``, ``_3``, … — discovered
    from both the environment (set keys) and the label registry. Returns slots
    with index / env_var / label / is_set; never the value."""
    labels = _load_labels()
    indices: set[int] = set()
    # #1 — the base var, if set or labelled
    if os.environ.get(base_env_var) or base_env_var in labels:
        indices.add(1)
    # #2.. — scan env + labels for "<base>_<digits>"
    prefix = base_env_var + "_"
    for source in (os.environ.keys(), labels.keys()):
        for name in source:
            if name.startswith(prefix):
                suffix = name[len(prefix):]
                if suffix.isdigit():
                    indices.add(int(suffix))
    slots: list[KeySlot] = []
    for i in sorted(indices):
        ev = env_var_for(base_env_var, i)
        slots.append(KeySlot(
            index=i, env_var=ev, label=labels.get(ev),
            is_set=bool(os.environ.get(ev)),
        ))
    return slots


def add_key(
    base_env_var: str, value: str, label: Optional[str] = None
) -> KeySlot:
    """Store a key in the next free slot (#1 if the base var is empty, else the
    smallest unused #N — no cap). Persists the value to the vault .env and the
    label here. Returns the new slot."""
    used = {s["index"] for s in list_keys(base_env_var)}
    index = 1
    while index in used:
        index += 1
    ev = env_var_for(base_env_var, index)
    config.set_env_secret(ev, value)
    if label:
        labels = _load_labels()
        labels[ev] = label
        _save_labels(labels)
    return KeySlot(index=index, env_var=ev, label=label, is_set=True)


_pool_cursor: dict[str, int] = {}


def pool_env_vars(base_env_var: str) -> list[str]:
    """The env vars of the *set* keys for this provider — the rotation pool
    (#1 first). A model preset flagged ``pool`` spreads requests across these,
    so e.g. six producers ride three keys instead of one rate-limited key."""
    return [s["env_var"] for s in list_keys(base_env_var) if s["is_set"]]


def next_pool_env_var(base_env_var: str) -> str:
    """Round-robin the next set key's env var (per-request load balancing).
    Falls back to the base var when the pool is empty. Advances a per-provider
    cursor each call."""
    pool = pool_env_vars(base_env_var)
    if not pool:
        return base_env_var
    i = _pool_cursor.get(base_env_var, 0) % len(pool)
    _pool_cursor[base_env_var] = i + 1
    return pool[i]


def remove_key(env_var: str) -> bool:
    """Remove a key entirely — its value from the vault .env (+ os.environ) and
    its label from the registry. Returns True if anything was removed. Presets
    that referenced it will read as needs-setup until repointed."""
    removed = config.remove_env_secret(env_var)
    labels = _load_labels()
    if env_var in labels:
        del labels[env_var]
        _save_labels(labels)
        removed = True
    return removed


def default_env_var(base_env_var: str) -> str:
    """The default key's env var — always #1 (the base var)."""
    return base_env_var


__all__ = [
    "KeySlot",
    "LABELS_FILE",
    "env_var_for",
    "list_keys",
    "add_key",
    "remove_key",
    "default_env_var",
    "pool_env_vars",
    "next_pool_env_var",
]
