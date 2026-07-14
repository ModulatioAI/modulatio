# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Cowboy Claude and Clifton Knox.
"""WebOS CONFIG routes — the read/write configuration surface (Feature 2).

The browser mirror of the TUI's CONFIG tab: Settings, Folders, Projects,
Agents, Models, Services. Every handler is a thin call into the SAME engine
seam the matching TUI screen uses, and — the load-bearing rule — reproduces
that screen's GUARDS explicitly (shell/.env read-only, range checks, the triad
floor, project triple-guard, folder-root refusal). Secret VALUES are
write-only: they go IN and are never returned; a key view reports only whether
a slot is set.
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, field_validator

from modulatio.web.routes.console import valid_project

router = APIRouter(prefix="/api")


def _producer_id(name: str, existing: set) -> str:
    """The TUI's exact producer-id derivation: sanitize the name to
    ``[a-z0-9_]`` (so a junk/traversal name can't make a traversal id), then
    de-duplicate with a numeric suffix."""
    base = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "producer"
    agent_id, n = base, 2
    while agent_id in existing:
        agent_id = f"{base}_{n}"
        n += 1
    return agent_id


# ── SETTINGS (install-wide engine knobs) ──────────────────────────────


class KnobValue(BaseModel):
    value: str


@router.get("/settings")
def settings_list() -> dict:
    from modulatio import settings_knobs

    return {"knobs": [
        {
            "key": k.key, "label": k.label, "default": k.default, "hint": k.hint,
            "value": settings_knobs.knob_value(k.key),
            "source": settings_knobs.knob_source(k.key),
        }
        for k in settings_knobs.KNOBS
    ]}


@router.post("/settings/{key}")
def settings_set(key: str, body: KnobValue) -> dict:
    from modulatio import settings_knobs

    if key not in settings_knobs.BY_KEY:
        raise HTTPException(status_code=404, detail=f"unknown setting {key}")
    ok, reason = settings_knobs.set_knob(key, body.value)
    if not ok:
        # A shell/.env-owned knob is a conflict (it wins, read-only here); a
        # bad value is unprocessable.
        status = 409 if "read-only" in reason else 422
        raise HTTPException(status_code=status, detail=reason)
    return {"saved": True, "source": settings_knobs.knob_source(key)}


@router.delete("/settings/{key}")
def settings_clear(key: str) -> dict:
    from modulatio import settings_knobs

    if key not in settings_knobs.BY_KEY:
        raise HTTPException(status_code=404, detail=f"unknown setting {key}")
    settings_knobs.clear_knob(key)
    return {"cleared": True, "source": settings_knobs.knob_source(key)}


# ── FOLDERS (install-wide registry — the management view) ──────────────
#
# This is the operator's own folder registry (their own paths on the loopback
# box), so — unlike the export picker's /api/folders (names+modes only) — the
# management view shows the path you registered, exactly as the TUI does. The
# ADD guards are the TUI's, ported one-for-one so a hand-crafted request can't
# register what the terminal would refuse.

_FOLDER_MODES = ("ro", "output", "rw")


class FolderAdd(BaseModel):
    name: str
    path: str
    mode: str


@router.get("/config/folders")
def folders_list() -> dict:
    from modulatio import config

    return {"folders": [
        {"name": f["name"], "mode": f["mode"], "path": f["path"]}
        for f in config.list_folders()
    ]}


@router.post("/config/folders")
def folders_add(body: FolderAdd) -> dict:
    from modulatio import config

    if body.mode not in _FOLDER_MODES:
        raise HTTPException(status_code=422, detail=f"bad mode {body.mode!r}")
    p = Path(body.path)
    if not p.is_absolute():
        raise HTTPException(
            status_code=422,
            detail="path must be absolute (a mounted share is registered by "
                   "its mount point)")
    p = p.resolve()
    if not config.probe_folder(str(p)):
        raise HTTPException(
            status_code=422,
            detail=f"{p} is not a reachable directory — mount the share / "
                   "create the folder first")
    reason = config.folder_root_refusal(str(p))
    if reason is not None:
        raise HTTPException(status_code=422, detail=reason)
    name = body.name.strip() or p.name
    folders = config.list_folders()
    if any(r["name"].lower() == name.lower() for r in folders):
        raise HTTPException(status_code=422, detail=f"a folder named '{name}' exists")
    if any(Path(r["path"]) == p for r in folders):
        raise HTTPException(status_code=422, detail=f"{p} is already registered")
    folders.append({"name": name, "path": str(p), "mode": body.mode, "kind": "path"})
    config.save_folders(folders)
    return {"name": name, "mode": body.mode}


@router.delete("/config/folders/{name}")
def folders_remove(name: str) -> dict:
    from modulatio import config

    folders = config.list_folders()
    kept = [r for r in folders if r["name"].lower() != name.lower()]
    if len(kept) == len(folders):
        raise HTTPException(status_code=404, detail=f"unknown folder {name}")
    config.save_folders(kept)
    return {"deleted": True}


@router.post("/config/folders/{name}/output")
def folders_set_output(name: str) -> dict:
    """Pick a folder as the job-output destination — only an ``output``-mode
    folder can receive the finished product (the TUI's rule)."""
    from modulatio import config

    rec = next((r for r in config.list_folders() if r["name"] == name), None)
    if rec is None:
        raise HTTPException(status_code=404, detail=f"unknown folder {name}")
    if rec["mode"] != "output":
        raise HTTPException(
            status_code=409,
            detail=f"'{name}' is {rec['mode']} — only an output-mode folder "
                   "can receive the job's finished product")
    config.set_job_output_folder(name)
    return {"output": name}


# ── MODELS (read — the preset list; the add/key surface is its own slice) ─


@router.get("/config/models")
def models_list() -> dict:
    from modulatio import model_presets

    presets = model_presets.load_presets()
    return {"models": [
        {
            "key": key,
            "provider": rec.get("provider", ""),
            "model": rec.get("model", ""),
            "available": model_presets.is_available(key),
            # The key env-var NAME (not a secret) — the key manager targets it.
            "env_var": (rec.get("auth_config") or {}).get("env_var"),
        }
        for key, rec in presets.items()
    ]}


# ── AGENTS (project roster; leader/qc singletons; TUI guards ported) ──


class AgentAdd(BaseModel):
    tier: str
    name: str
    model: str


class AgentModel(BaseModel):
    model: str


class AgentFallbacks(BaseModel):
    fallbacks: list[str]


def _agent_json(a) -> dict:
    return {
        "id": a.id, "tier": a.tier, "name": a.name, "model": a.model,
        "skills": list(getattr(a, "skills", []) or []),
        "fallbacks": list(getattr(a, "fallback_models", []) or []),
    }


@router.get("/{project}/config/agents")
def agents_list(project: str) -> dict:
    from modulatio import roster

    code = valid_project(project)
    return {"agents": [_agent_json(a) for a in roster.list_agents(code)]}


@router.post("/{project}/config/reload")
def config_reload(project: str, request: Request) -> dict:
    """The TUI's /reload, on the web: apply config/roster changes to the live
    services without a server restart. The actor mirrors the TUI's guard —
    busy Leader or a running job → 409, reload refused."""
    from modulatio.web.actors import get_actor

    code = valid_project(project)
    ok, message = get_actor(code, stub=bool(request.app.state.stub)).reload_services()
    if not ok:
        raise HTTPException(status_code=409, detail=message)
    return {"message": message}


@router.post("/{project}/config/agents")
def agents_add(project: str, body: AgentAdd) -> dict:
    from modulatio import roster

    code = valid_project(project)
    if body.tier not in ("leader", "qc", "producer"):
        raise HTTPException(status_code=422, detail=f"bad tier {body.tier!r}")
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="name the agent first")
    existing = {a.id for a in roster.list_agents(code)}
    if body.tier in ("leader", "qc"):
        agent_id = body.tier  # singleton keyed by the role
        if agent_id in existing:
            raise HTTPException(
                status_code=409,
                detail=f"a {body.tier} already exists — remove it first, then re-add")
        identity = f"{name}, the {body.tier}."
    else:
        agent_id = _producer_id(name, existing)
        identity = f"{name}, a producer."
    roster.add_agent(
        project_code=code, agent_id=agent_id, name=name, identity=identity,
        skills=[], model=body.model, tier=body.tier)
    return {"agent_id": agent_id}


@router.put("/{project}/config/agents/{agent_id}/model")
def agents_set_model(project: str, agent_id: str, body: AgentModel) -> dict:
    from modulatio import roster

    code = valid_project(project)
    try:
        roster.add_model(project_code=code, agent_id=agent_id, model=body.model)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:  # invalid agent id
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"model": body.model}


@router.put("/{project}/config/agents/{agent_id}/fallbacks")
def agents_set_fallbacks(project: str, agent_id: str, body: AgentFallbacks) -> dict:
    from modulatio import roster

    code = valid_project(project)
    try:
        roster.set_fallbacks(
            project_code=code, agent_id=agent_id, fallback_keys=body.fallbacks)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"fallbacks": body.fallbacks}


@router.delete("/{project}/config/agents/{agent_id}")
def agents_remove(project: str, agent_id: str) -> dict:
    from modulatio import roster, vault

    code = valid_project(project)
    try:
        vault.validate_registry_name(agent_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"deleted": roster.remove_agent(project_code=code, agent_id=agent_id)}


# ── MODELS (add / remove) + the provider catalog ──────────────────────


class ModelAdd(BaseModel):
    provider_id: str
    model: str
    # The operator's chosen auth method (the TUI's AuthStep radio, mirrored).
    # Blank = the provider's api-key option, else its first — the pre-choice
    # behavior, kept for back-compat.
    auth_type: str = ""


@router.get("/config/providers")
def providers_catalog() -> dict:
    """The shipped, user-agnostic provider catalog the add-model form picks
    from — base_url / api_format / auth options auto-filled per provider; the
    operator supplies only the model id and (separately) the key."""
    from modulatio import provider_catalog as pc

    out = []
    for p in pc.list_providers():
        auth = []
        for a in p.auth_options:
            entry = {"auth_type": a.auth_type, "label": a.label,
                     "env_var": a.env_var}
            if a.auth_type.startswith("oauth"):
                # The TUI's AuthStep shows an OAuth option's live signed-in
                # status + setup hint — mirror it so the browser's method
                # picker can too (file checks only; no network).
                ready, hint = pc.auth_status(a)
                entry["ready"] = ready
                entry["hint"] = a.oauth_hint or hint
            auth.append(entry)
        out.append({
            "id": p.id, "name": p.name, "base_url": p.base_url,
            "api_format": p.api_format, "signup_url": p.signup_url,
            "auth": auth,
        })
    return {"providers": out}


class OauthLoginStart(BaseModel):
    auth_type: str


@router.post("/config/oauth-login")
def oauth_login_start(body: OauthLoginStart) -> dict:
    """Start Modulatio's OWN OAuth sign-in for a provider method — no
    separate tooling needed. Two shapes: ``redirect`` (a consent URL the
    browser opens; the callback lands on this machine's loopback, so the
    operator's browser must be local) and ``device`` (a verification page +
    short code — works from any browser). Poll the GET twin for the outcome;
    the minted tokens land in the vault-side stores, never in a response."""
    from modulatio import oauth_login
    try:
        if body.auth_type == "oauth_xai":
            return {"kind": "redirect", "url": oauth_login.begin_xai_login()}
        if body.auth_type == "oauth_openai":
            info = oauth_login.begin_openai_login()
            return {"kind": "device", "url": info["url"],
                    "user_code": info["user_code"]}
    except oauth_login.LoginError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    raise HTTPException(
        status_code=422, detail=f"no in-app sign-in for {body.auth_type!r}")


@router.get("/config/oauth-login")
def oauth_login_state() -> dict:
    from modulatio import oauth_login
    return oauth_login.login_status()


@router.get("/config/providers/{provider_id}/models")
def provider_models(provider_id: str, auth_type: str = Query("")) -> dict:
    """The provider's LIVE model list for the add-model picker — the same
    engine fetch the TUI picker runs (``fetch_models``), with the listing key
    resolved server-side from the CHOSEN auth option (the TUI passes its
    AuthStep selection to the picker the same way); blank = the provider's
    api-key option, else its first. The key itself never crosses the web
    boundary; an unreachable list degrades to ``[]`` (the form falls back to
    a typed model id)."""
    from modulatio import provider_catalog as pc

    provider = pc.get_provider(provider_id)
    if provider is None:
        raise HTTPException(status_code=422, detail=f"unknown provider {provider_id}")
    if auth_type:
        auth = next((a for a in provider.auth_options
                     if a.auth_type == auth_type), None)
        if auth is None:
            raise HTTPException(
                status_code=422,
                detail=f"provider {provider_id} has no auth {auth_type!r}")
    else:
        auth = next((a for a in provider.auth_options if a.auth_type == "api_key"),
                    provider.auth_options[0])
    key = pc.listing_key(env_var=auth.env_var, auth_type=auth.auth_type)
    try:
        models = pc.fetch_models(provider, api_key=key)
    except Exception:  # noqa: BLE001 — a down endpoint degrades to free-text entry
        models = []
    # Role-relevant text models, same as the TUI picker's default listing.
    return {"models": [
        {"id": m.id, "free": m.is_free}
        for m in pc.of_modality(models, "text")
    ]}


@router.post("/config/models/add")
def model_add(body: ModelAdd) -> dict:
    from modulatio import model_presets
    from modulatio import provider_catalog as pc

    provider = pc.get_provider(body.provider_id)
    if provider is None:
        raise HTTPException(status_code=422, detail=f"unknown provider {body.provider_id}")
    if not body.model.strip():
        raise HTTPException(status_code=422, detail="model id required")
    # The auth method: the operator's explicit choice when given (must be one
    # of the provider's real options — never a caller-invented shape), else the
    # provider's api-key option, else its first. The KEY itself is never
    # handled here — it lives in the write-only key pool under that env var.
    if body.auth_type:
        auth = next((a for a in provider.auth_options
                     if a.auth_type == body.auth_type), None)
        if auth is None:
            raise HTTPException(
                status_code=422,
                detail=f"provider {provider.id} has no auth {body.auth_type!r}")
    else:
        auth = next((a for a in provider.auth_options if a.auth_type == "api_key"),
                    provider.auth_options[0])
    model = pc.CatalogModel(id=body.model, name=body.model, provider_id=provider.id)
    kwargs = pc.preset_kwargs(provider, model, auth)
    key = kwargs.pop("key")
    try:
        model_presets.add_preset(key, **kwargs)
    except ValueError as exc:  # collision / invalid
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"key": key}


@router.delete("/config/models/{key}")
def model_remove(key: str) -> dict:
    from modulatio import model_presets

    return {"deleted": model_presets.remove_preset(key)}


# ── KEYS (write-only provider-key pool — the red-line) ────────────────
#
# Values go IN write-only (POST) and are NEVER returned; a slot view reports
# only whether it is_set. This is the whole security crux of Feature 2 — the
# key never leaves the vault, so it never crosses the web boundary out.


class KeyAdd(BaseModel):
    base: str
    value: str
    label: str | None = None

    @field_validator("value")
    @classmethod
    def _non_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("key value must be non-blank")
        return v


def _slot_json(s) -> dict:
    # Deliberately enumerate the safe fields — never `value`.
    return {
        "index": s["index"], "env_var": s["env_var"], "label": s.get("label"),
        "is_set": s["is_set"], "pinned_to": list(s.get("pinned_to", [])),
    }


def _allowed_key_bases() -> set:
    """The ONLY env vars the WebOS key manager may touch (WB-1): the key
    handles of CONFIGURED models + services. The route exposes the provider-key
    pool primitive, so without this a crafted request could write ANY env var
    into the vault .env — e.g. the MODULATIO_RUN_SHELL_UNSAFE sandbox switch.
    Keys are only ever set for a model/service that already exists, so the
    configured set is exactly the legitimate target set."""
    from modulatio import model_presets, services

    bases: set = set()
    for rec in model_presets.load_presets().values():
        ev = (rec.get("auth_config") or {}).get("env_var")
        if ev:
            bases.add(ev)
    for svc in services.load_services().values():
        if svc.env_var:
            bases.add(svc.env_var)
    return bases


def _require_key_base(base: str) -> str:
    if not base.isidentifier() or base not in _allowed_key_bases():
        raise HTTPException(
            status_code=404,
            detail="not a configured model/service key handle")
    return base


@router.get("/config/keys")
def keys_list(base: str = Query(...)) -> dict:
    from modulatio import provider_keys

    _require_key_base(base)
    return {"slots": [_slot_json(s) for s in provider_keys.list_keys(base)]}


@router.post("/config/keys")
def keys_add(body: KeyAdd) -> dict:
    from modulatio import provider_keys

    _require_key_base(body.base)
    slot = provider_keys.add_key(body.base, body.value, body.label)
    return _slot_json(slot)


@router.delete("/config/keys/{env_var}")
def keys_remove(env_var: str) -> dict:
    from modulatio import provider_keys

    # A slot's env var is its base (#1) or ``<base>_<digits>`` (#2..) — the
    # target must belong to an ALLOWED base, never an arbitrary name (WB-1).
    if not env_var.isidentifier():
        raise HTTPException(status_code=404, detail="invalid key handle")
    ok = any(env_var == b or re.fullmatch(rf"{re.escape(b)}_\d+", env_var)
             for b in _allowed_key_bases())
    if not ok:
        raise HTTPException(
            status_code=404, detail="not a configured model/service key slot")
    return {"deleted": provider_keys.remove_key(env_var)}


# ── SERVICES (outside APIs; catalog or custom; keys via the pool) ─────


class ServiceCatalogAdd(BaseModel):
    catalog_id: str


class ServiceCustomAdd(BaseModel):
    name: str
    base_url: str
    auth_shape: str = "bearer"
    capabilities: list[str]
    per_task_cap: int = 1


class CapabilityDefault(BaseModel):
    capability: str
    service_id: str


def _service_json(s) -> dict:
    return {
        "id": s.id, "name": s.name, "kind": s.kind,
        "capabilities": list(s.capabilities), "base_url": s.base_url,
        "env_var": s.env_var, "free_tier": s.free_tier,
        "per_task_cap": s.per_task_cap,
    }


@router.get("/config/service-catalog")
def service_catalog_read() -> dict:
    from modulatio import service_catalog as sc

    return {"catalog": [
        {
            "id": e.service.id, "name": e.service.name,
            "capabilities": list(e.service.capabilities),
            "base_url": e.service.base_url, "beta": e.beta, "notes": e.notes,
        }
        for e in sc.catalog()
    ]}


@router.get("/config/services")
def services_list() -> dict:
    from modulatio import services

    return {"services": [_service_json(s) for s in services.load_services().values()]}


@router.post("/config/services/add-catalog")
def service_add_catalog(body: ServiceCatalogAdd) -> dict:
    from modulatio import service_catalog as sc, services

    entry = sc.entry(body.catalog_id)
    if entry is None:
        raise HTTPException(status_code=422, detail=f"unknown service {body.catalog_id}")
    try:
        services.add_service(entry.service)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"id": entry.service.id}


@router.post("/config/services/add-custom")
def service_add_custom(body: ServiceCustomAdd) -> dict:
    from modulatio import services

    slug = re.sub(r"[^a-z0-9]+", "-", body.name.lower()).strip("-")
    if not slug:
        raise HTTPException(status_code=422, detail="name the service")
    svc = services.Service(
        id=slug, name=body.name.strip(), kind="custom",
        capabilities=tuple(body.capabilities),
        env_var=services.new_key_handle(),  # opaque SVCKEY_<hex> — no name leak
        base_url=body.base_url, auth_shape=body.auth_shape,
        per_task_cap=body.per_task_cap,
    )
    try:
        services.add_service(svc)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"id": slug, "env_var": svc.env_var}


@router.delete("/config/services/{service_id}")
def service_remove(service_id: str) -> dict:
    from modulatio import services

    return {"deleted": services.remove_service(service_id)}


@router.post("/config/services/default")
def service_set_default(body: CapabilityDefault) -> dict:
    from modulatio import services

    try:
        services.set_capability_default(body.capability, body.service_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"capability": body.capability, "default": body.service_id}
