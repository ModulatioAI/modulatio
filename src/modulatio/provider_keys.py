# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Multiple API keys per provider — a shared floating pool, with optional pins.

A provider has a base env var (``GEMINI_API_KEY``). Additional keys live under
numbered variants (``GEMINI_API_KEY_2``, ``_3`, … — **no cap**). Each may carry
a short human label.

**The model, in one line:** a key is in the provider's shared floating pool
*unless* you pin it. By default every model on a provider draws from that pool
(``next_pool_env_var`` rotates across the keys — throughput + 429 failover).
That's the no-thought path: add keys, models use them.

**Pinning is the optional lever** (for metering/budgeting). Pin a key to one or
more specific model presets and it serves *only* those models — and it **leaves
the pool**, so no other model can spend it. Pin a key to your image model and
the vendor's meter on that key *is* your image-gen budget. Pins live in
``key_pins.json`` (``{env_var: [model_preset_key, …]}``); a key with a non-empty
pin list is excluded from ``pool_env_vars``.

The key *values* live in the vault ``.env`` (via ``config.set_env_secret``);
only the **labels** and **pins** are stored here (not secret).
"""
from __future__ import annotations

import json
import os
import threading
from typing import Optional, TypedDict

from modulatio import config

LABELS_FILE = config.CONFIG_DIR / "key_labels.json"  # {env_var: label}
PINS_FILE = config.CONFIG_DIR / "key_pins.json"       # {env_var: [model_key]}


class KeySlot(TypedDict):
    index: int
    env_var: str
    label: Optional[str]
    is_set: bool
    pinned_to: list[str]  # model preset keys; non-empty ⇒ out of the pool


def _load_labels() -> dict[str, str]:
    if LABELS_FILE.exists():
        try:
            data = json.loads(LABELS_FILE.read_text(encoding="utf-8", errors="replace"))
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}
    return {}


def _save_labels(labels: dict[str, str]) -> None:
    config.write_secret_file(LABELS_FILE, json.dumps(labels, indent=2))


def _load_pins() -> dict[str, list[str]]:
    if PINS_FILE.exists():
        try:
            data = json.loads(PINS_FILE.read_text(encoding="utf-8", errors="replace"))
            if isinstance(data, dict):
                # keep only well-formed {env_var: [str, …]} entries
                return {k: [m for m in v if isinstance(m, str)]
                        for k, v in data.items() if isinstance(v, list)}
        except (OSError, json.JSONDecodeError):
            return {}
    return {}


def _save_pins(pins: dict[str, list[str]]) -> None:
    # drop emptied entries so an unpinned key cleanly rejoins the pool
    pins = {k: v for k, v in pins.items() if v}
    config.write_secret_file(PINS_FILE, json.dumps(pins, indent=2))


def env_var_for(base_env_var: str, index: int) -> str:
    """The env var for key #index — #1 is the base var, #N is ``<base>_N``."""
    return base_env_var if index == 1 else f"{base_env_var}_{index}"


def list_keys(base_env_var: str) -> list[KeySlot]:
    """Every configured key for a provider (by its base env var), sorted by
    index. #1 is the base var; #2.. are ``<base>_2``, ``_3``, … — discovered
    from both the environment (set keys) and the label registry. Returns slots
    with index / env_var / label / is_set; never the value."""
    labels = _load_labels()
    pins = _load_pins()
    # Snapshot env names once: a concurrent config edit can mutate os.environ
    # mid-scan ("dict changed size during iteration").
    env_names = list(os.environ.keys())
    indices: set[int] = set()
    # #1 — the base var, if set or labelled
    if os.environ.get(base_env_var) or base_env_var in labels:
        indices.add(1)
    # #2.. — scan env + labels for "<base>_<digits>"
    prefix = base_env_var + "_"
    for source in (env_names, labels.keys()):
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
            pinned_to=list(pins.get(ev, [])),
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
_pool_cursor_lock = threading.Lock()  # guards the cursor read-modify-write


def pool_env_vars(base_env_var: str) -> list[str]:
    """The provider's shared floating pool — env vars of the *set, unpinned*
    keys (#1 first). A pooled model spreads requests across these, so e.g. six
    producers ride three keys instead of one rate-limited key. **Pinned keys are
    excluded**: once a key is pinned to a model it serves only that model and
    drops out of the pool, keeping its spend isolated for metering."""
    return [s["env_var"] for s in list_keys(base_env_var)
            if s["is_set"] and not s["pinned_to"]]


def next_pool_env_var(base_env_var: str) -> str:
    """Round-robin the next set key's env var (per-request load balancing).
    Falls back to the base var when the pool is empty. Advances a per-provider
    cursor each call."""
    pool = pool_env_vars(base_env_var)
    if not pool:
        return base_env_var
    # Concurrent wave workers call this; the cursor read-modify-write must be
    # atomic or two workers can read the same index (uneven balancing) and
    # corrupt the count.
    with _pool_cursor_lock:
        i = _pool_cursor.get(base_env_var, 0) % len(pool)
        _pool_cursor[base_env_var] = i + 1
    return pool[i]


def remove_key(env_var: str) -> bool:
    """Remove a key entirely — its value from the vault .env (+ os.environ), its
    label, and any pins. Returns True if anything was removed. Presets that
    referenced it will read as needs-setup until repointed."""
    removed = config.remove_env_secret(env_var)
    labels = _load_labels()
    if env_var in labels:
        del labels[env_var]
        _save_labels(labels)
        removed = True
    pins = _load_pins()
    if env_var in pins:
        del pins[env_var]
        _save_pins(pins)
        removed = True
    return removed


# ── pinning: the optional metering lever ────────────────────────────────────

def pin_key(env_var: str, model_key: str) -> None:
    """Pin a key to a model preset. The key now serves only its pinned model(s)
    and leaves the shared pool (so its spend stays isolated for metering)."""
    pins = _load_pins()
    bound = pins.setdefault(env_var, [])
    if model_key not in bound:
        bound.append(model_key)
    _save_pins(pins)


def unpin_key(env_var: str, model_key: Optional[str] = None) -> None:
    """Unpin a key. With ``model_key`` → release just that binding; without →
    release all of them. A key with no bindings left rejoins the shared pool."""
    pins = _load_pins()
    if env_var not in pins:
        return
    if model_key is None:
        del pins[env_var]
    else:
        pins[env_var] = [m for m in pins[env_var] if m != model_key]
    _save_pins(pins)


def unpin_model(model_key: str) -> None:
    """Release every key pinned to a model — call when the model is removed so
    its keys rejoin the pool instead of dangling on a deleted preset."""
    pins = _load_pins()
    changed = False
    for ev in list(pins):
        if model_key in pins[ev]:
            pins[ev] = [m for m in pins[ev] if m != model_key]
            changed = True
    if changed:
        _save_pins(pins)


def pinned_env_var_for(model_key: str, base_env_var: str) -> Optional[str]:
    """The (set) key pinned to this model on this provider, if any — the env var
    a pinned model resolves to. None when the model uses the shared pool."""
    for slot in list_keys(base_env_var):
        if slot["is_set"] and model_key in slot["pinned_to"]:
            return slot["env_var"]
    return None


def default_env_var(base_env_var: str) -> str:
    """The default key's env var — always #1 (the base var)."""
    return base_env_var


__all__ = [
    "KeySlot",
    "LABELS_FILE",
    "PINS_FILE",
    "env_var_for",
    "list_keys",
    "add_key",
    "remove_key",
    "default_env_var",
    "pool_env_vars",
    "next_pool_env_var",
    "pin_key",
    "unpin_key",
    "unpin_model",
    "pinned_env_var_for",
]
