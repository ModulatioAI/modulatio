# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""The service-API pool — outside-SaaS services and their keys.

Spec: docs/design/2026-07-05-service-api-pool.md. A *service* is an outside
application reachable over an API (image generation, video, speech, research,
anything within reason). The registry (``services.json`` in the config dir)
records which services the operator configured; the KEYS live in the existing
``provider_keys`` numbered-slot pool under each service's ``env_var`` (vault
``.env`` via ``config.set_env_secret`` — this module never stores a secret).

Resolution is the model-picker rhyme: the operator's per-capability default,
else the only service configured for that capability, else None (ambiguity
does not guess). The Leader is a superset consumer — same registry, no
Leader-specific path.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from typing import Optional

from modulatio import config, provider_keys

SERVICES_FILE = config.CONFIG_DIR / "services.json"

#: Capability classes seeded by the spec. The set is OPEN — a new class earns
#: a typed tool when a cataloged vendor demands one; until then the custom
#: lane (api_call + a Leader-authored skill) serves it.
KNOWN_CAPABILITIES = ("image", "video", "speech", "research")


@dataclass(frozen=True)
class Service:
    id: str
    name: str
    kind: str  # "catalog" | "custom"
    capabilities: tuple[str, ...]
    env_var: str
    base_url: str
    auth_shape: str  # "bearer" | "header:<Name>" | "query:<name>"
    free_tier: bool = False
    docs_url: str = ""
    per_task_cap: int = 1


def _validate(svc: Service) -> None:
    if not svc.id or not svc.id.replace("-", "").replace("_", "").isalnum():
        raise ValueError(f"service id {svc.id!r} must be a simple slug")
    if svc.kind not in ("catalog", "custom"):
        raise ValueError(f"service kind {svc.kind!r} must be catalog|custom")
    if not svc.base_url.startswith(("https://", "http://")):
        raise ValueError(
            f"service {svc.id!r}: base_url is required and must be an "
            "absolute http(s) URL — it is pinned at add time (the pin IS "
            "the authorization)"
        )
    if not (svc.auth_shape == "bearer"
            or (svc.auth_shape.startswith(("header:", "query:"))
                and svc.auth_shape.split(":", 1)[1])):
        raise ValueError(
            f"service {svc.id!r}: auth_shape {svc.auth_shape!r} must be "
            "bearer | header:<Name> | query:<name>"
        )
    if not svc.env_var.isidentifier():
        raise ValueError(f"service {svc.id!r}: env_var {svc.env_var!r} invalid")


def _load_raw() -> dict:
    if not SERVICES_FILE.exists():
        return {}
    try:
        data = json.loads(
            SERVICES_FILE.read_text(encoding="utf-8", errors="replace")
        )
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_raw(data: dict) -> None:
    config.write_secret_file(SERVICES_FILE, json.dumps(data, indent=2))


def load_services() -> dict[str, Service]:
    out: dict[str, Service] = {}
    for sid, entry in _load_raw().get("services", {}).items():
        if not isinstance(entry, dict):
            continue  # one corrupt entry must not hide the rest
        try:
            out[sid] = Service(
                id=sid,
                name=str(entry.get("name", sid)),
                kind=str(entry.get("kind", "custom")),
                capabilities=tuple(entry.get("capabilities", ())),
                env_var=str(entry.get("env_var", "")),
                base_url=str(entry.get("base_url", "")),
                auth_shape=str(entry.get("auth_shape", "bearer")),
                free_tier=bool(entry.get("free_tier", False)),
                docs_url=str(entry.get("docs_url", "")),
                per_task_cap=int(entry.get("per_task_cap", 1)),
            )
        except (TypeError, ValueError):
            continue  # one corrupt entry must not hide the rest
    return out


def get_service(service_id: str) -> Optional[Service]:
    return load_services().get(service_id)


def add_service(svc: Service) -> None:
    _validate(svc)
    data = _load_raw()
    entry = asdict(svc)
    entry.pop("id")
    entry["capabilities"] = list(svc.capabilities)
    data.setdefault("services", {})[svc.id] = entry
    _save_raw(data)


def remove_service(service_id: str) -> bool:
    data = _load_raw()
    if service_id not in data.get("services", {}):
        return False
    del data["services"][service_id]
    # Drop any capability default pointing at the removed service.
    defaults = data.get("capability_defaults", {})
    for cap in [c for c, s in defaults.items() if s == service_id]:
        del defaults[cap]
    _save_raw(data)
    return True


def set_capability_default(capability: str, service_id: str) -> None:
    data = _load_raw()
    if service_id not in data.get("services", {}):
        raise ValueError(f"no service {service_id!r} configured")
    data.setdefault("capability_defaults", {})[capability] = service_id
    _save_raw(data)


def resolve_for_capability(capability: str) -> Optional[Service]:
    """The model-picker rhyme: operator default → only-one-configured →
    None. Two candidates and no default is ambiguous — never guess with
    someone else's money."""
    all_svcs = load_services()
    default_id = _load_raw().get("capability_defaults", {}).get(capability)
    if default_id and default_id in all_svcs:
        if capability in all_svcs[default_id].capabilities:
            return all_svcs[default_id]
    backed = [s for s in all_svcs.values() if capability in s.capabilities]
    return backed[0] if len(backed) == 1 else None


def checkout_key(svc: Service) -> Optional[str]:
    """Check a key out of the service's slot pool: first set slot wins
    (v1 floor — on-error rotation is the adapter retry's job, deferred).
    Returns the key VALUE; callers inject it and never surface it."""
    for slot in provider_keys.list_keys(svc.env_var):
        if slot["is_set"]:
            value = os.environ.get(slot["env_var"], "").strip()
            if value:
                return value
    return None


def cost_class_for(svc: Service) -> Optional[str]:
    """Metered by default; ``free_tier`` opts out (spec Decision 3)."""
    return None if svc.free_tier else "paid-cloud"
