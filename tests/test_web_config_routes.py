# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Cowboy Claude and Clifton Knox.
"""WebOS CONFIG routes — the read/write configuration surface (Feature 2).
Each write binds the SAME engine seam the TUI Config screens use; secret
values are write-only and never cross the boundary out.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from modulatio import config, vault  # noqa: E402 — after the extra guard

pytestmark = pytest.mark.usefixtures("fresh_web_registries")


@pytest.fixture(autouse=True)
def _isolate_knob_env(monkeypatch):
    """The SETTINGS knob writes mutate PROCESS-GLOBAL os.environ +
    config._ENV_OVERRIDES_SET (a knob applies to the next call in-process).
    Reset both per test, and clear the knobs these tests touch, so a knob left
    set by another file's test can't make ours read it as shell/.env-owned
    (the full-suite-only failure the isolated run hid — engram 2274 class)."""
    monkeypatch.setattr(config, "_ENV_OVERRIDES_SET", set())
    for k in ("MODULATIO_TASK_MAX_RETRIES", "MODULATIO_QC_FIXER"):
        monkeypatch.delenv(k, raising=False)
    yield


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    from modulatio.web.app import create_app

    vault.init_project("web", "Web", "o")
    return TestClient(create_app(stub=True), base_url="http://localhost",
                      headers={"X-Modulatio-WebOS": "1"})


# ── SETTINGS ──────────────────────────────────────────────────────────


def test_settings_list_knobs_with_value_and_source(client):
    resp = client.get("/api/settings")
    assert resp.status_code == 200
    knobs = resp.json()["knobs"]
    by_key = {k["key"]: k for k in knobs}
    assert "MODULATIO_TASK_MAX_RETRIES" in by_key
    row = by_key["MODULATIO_TASK_MAX_RETRIES"]
    assert row["label"] and row["default"] == "3" and row["hint"]
    assert row["source"] in ("default", "settings", "shell/.env")


def test_settings_set_and_clear(client):
    r = client.post("/api/settings/MODULATIO_TASK_MAX_RETRIES", json={"value": "2"})
    assert r.status_code == 200
    assert config._load_defaults()["env_overrides"]["MODULATIO_TASK_MAX_RETRIES"] == "2"

    d = client.delete("/api/settings/MODULATIO_TASK_MAX_RETRIES")
    assert d.status_code == 200
    assert "MODULATIO_TASK_MAX_RETRIES" not in (
        config._load_defaults().get("env_overrides") or {})


def test_settings_set_out_of_range_422(client):
    r = client.post("/api/settings/MODULATIO_TASK_MAX_RETRIES", json={"value": "99"})
    assert r.status_code == 422


def test_settings_set_unknown_knob_404(client):
    r = client.post("/api/settings/MODULATIO_NOPE", json={"value": "1"})
    assert r.status_code == 404


def test_settings_shell_owned_knob_refused(client, monkeypatch):
    monkeypatch.setenv("MODULATIO_QC_FIXER", "0")
    r = client.post("/api/settings/MODULATIO_QC_FIXER", json={"value": "1"})
    assert r.status_code == 409  # read-only, owned outside the app


# ── FOLDERS (management view — the operator's own registry) ────────────


def test_folders_add_list_and_remove(client, tmp_path):
    d = tmp_path / "contracts"
    d.mkdir()
    add = client.post("/api/config/folders",
                      json={"name": "contracts", "path": str(d), "mode": "rw"})
    assert add.status_code == 200
    rows = client.get("/api/config/folders").json()["folders"]
    row = next(r for r in rows if r["name"] == "contracts")
    assert row["mode"] == "rw" and row["path"] == str(d)

    rm = client.delete("/api/config/folders/contracts")
    assert rm.status_code == 200
    assert not any(r["name"] == "contracts"
                   for r in client.get("/api/config/folders").json()["folders"])


def test_folders_add_refuses_bad_path(client, tmp_path):
    # non-absolute
    assert client.post("/api/config/folders",
                       json={"name": "x", "path": "relative/dir", "mode": "rw"}
                       ).status_code == 422
    # unreachable
    assert client.post("/api/config/folders",
                       json={"name": "y", "path": str(tmp_path / "nope"), "mode": "rw"}
                       ).status_code == 422
    # a system root
    assert client.post("/api/config/folders",
                       json={"name": "z", "path": "/etc", "mode": "rw"}
                       ).status_code == 422


def test_folders_add_refuses_duplicate_name(client, tmp_path):
    a = tmp_path / "a"
    a.mkdir()
    b = tmp_path / "b"
    b.mkdir()
    client.post("/api/config/folders",
                json={"name": "dup", "path": str(a), "mode": "rw"})
    r = client.post("/api/config/folders",
                    json={"name": "dup", "path": str(b), "mode": "rw"})
    assert r.status_code == 422


def test_folders_set_output_only_output_mode(client, tmp_path):
    from modulatio import config

    out = tmp_path / "deliver"
    out.mkdir()
    client.post("/api/config/folders",
                json={"name": "deliver", "path": str(out), "mode": "output"})
    r = client.post("/api/config/folders/deliver/output")
    assert r.status_code == 200
    assert config.get_job_output_folder() == "deliver"


# ── PROJECTS (create / switch / delete with guards) ───────────────────


def test_project_create_switch_delete(client):
    from modulatio import config, vault

    assert client.post("/api/projects",
                       json={"code": "beta", "objective": "test"}).status_code == 200
    assert "beta" in vault.list_projects()
    assert client.post("/api/projects/beta/switch").status_code == 200
    assert config.get_default_project_code() == "beta"
    # switch away so beta isn't active, then delete
    client.post("/api/projects/web/switch")
    assert client.delete("/api/projects/beta").status_code == 200
    assert "beta" not in vault.list_projects()


def test_project_delete_refuses_active(client):
    from modulatio import config, vault

    vault.init_project("gamma", "G", "o")
    config.set_default_project_code("gamma")
    r = client.delete("/api/projects/gamma")
    assert r.status_code == 409  # active — switch away first
    assert "gamma" in vault.list_projects()


def test_project_delete_refuses_while_in_flight(client, monkeypatch):
    from modulatio import config, vault
    from modulatio.web.actors import get_actor

    vault.init_project("delta", "D", "o")
    config.set_default_project_code("web")  # delta not active
    monkeypatch.setattr(get_actor("delta", stub=True), "kickoff_active", lambda: True)
    r = client.delete("/api/projects/delta")
    assert r.status_code == 409
    assert "delta" in vault.list_projects()


# ── RELOAD (the TUI's /reload, mirrored: guard + drop + toast) ─────────


def test_config_reload_drops_cached_orchestrator(client):
    """Reload refreshes the config cache and drops the actor's cached converse
    orchestrator — the TUI's exact semantics — so the next message rebuilds
    from disk."""
    from modulatio.web.actors import get_actor

    actor = get_actor("web", stub=True)
    actor._converse_orch = object()          # a "live" cached orchestrator
    r = client.post("/api/web/config/reload")
    assert r.status_code == 200
    assert "reloaded" in r.json()["message"].lower()
    assert actor._converse_orch is None      # dropped — next use rebuilds


def test_config_reload_refuses_while_busy(client, monkeypatch):
    """The TUI's guard, ported: a running job (or busy Leader) refuses the
    reload with the same message — invalidating mid-turn would race it."""
    from modulatio.web.actors import get_actor

    actor = get_actor("web", stub=True)
    monkeypatch.setattr(actor, "kickoff_active", lambda: True)
    r = client.post("/api/web/config/reload")
    assert r.status_code == 409
    assert "busy" in r.json()["detail"]

    monkeypatch.setattr(actor, "kickoff_active", lambda: False)
    monkeypatch.setattr(actor, "_converse_busy", True)
    r = client.post("/api/web/config/reload")
    assert r.status_code == 409


# ── MODELS (read — the preset list agents pick from) ──────────────────


def test_models_list(client):
    from modulatio import model_presets

    model_presets.add_preset(
        "gpt-x", label="GPT-X", base_url="https://api.openai.com/v1",
        api_format="openai", auth_type="api_key", model="gpt-x")
    resp = client.get("/api/config/models")
    assert resp.status_code == 200
    keys = [m["key"] for m in resp.json()["models"]]
    assert "gpt-x" in keys


# ── AGENTS (roster writes; leader/qc singletons; guards ported) ───────


def test_agent_add_producer_list_and_remove(client):
    from modulatio import roster

    add = client.post("/api/web/config/agents",
                      json={"tier": "producer", "name": "Scout", "model": "gpt-x"})
    assert add.status_code == 200
    agent_id = add.json()["agent_id"]
    assert any(a.id == agent_id for a in roster.list_agents("web"))

    rm = client.delete(f"/api/web/config/agents/{agent_id}")
    assert rm.status_code == 200
    assert not any(a.id == agent_id for a in roster.list_agents("web"))


def test_agent_leader_is_a_singleton(client):
    first = client.post("/api/web/config/agents",
                        json={"tier": "leader", "name": "Boss", "model": "gpt-x"})
    assert first.status_code == 200
    again = client.post("/api/web/config/agents",
                        json={"tier": "leader", "name": "Boss2", "model": "gpt-x"})
    assert again.status_code == 409


def test_agent_change_model_and_fallbacks(client):
    from modulatio import roster

    aid = client.post("/api/web/config/agents",
                      json={"tier": "producer", "name": "Ace", "model": "gpt-x"}
                      ).json()["agent_id"]
    m = client.put(f"/api/web/config/agents/{aid}/model", json={"model": "claude-y"})
    assert m.status_code == 200
    assert next(a for a in roster.list_agents("web") if a.id == aid).model == "claude-y"

    fb = client.put(f"/api/web/config/agents/{aid}/fallbacks",
                    json={"fallbacks": ["gpt-x"]})
    assert fb.status_code == 200


def test_agent_add_blank_name_refused(client):
    r = client.post("/api/web/config/agents",
                    json={"tier": "producer", "name": "  ", "model": "gpt-x"})
    assert r.status_code == 422


def test_agent_add_bad_tier_refused(client):
    r = client.post("/api/web/config/agents",
                    json={"tier": "overlord", "name": "X", "model": "gpt-x"})
    assert r.status_code == 422


def test_agent_name_is_sanitized_to_a_safe_id(client):
    """A junk/traversal name can't produce a traversal id — the derivation
    strips non-alphanumerics (the TUI's exact rule), so the id is always safe."""
    r = client.post("/api/web/config/agents",
                    json={"tier": "producer", "name": "../evil", "model": "gpt-x"})
    assert r.status_code == 200
    assert r.json()["agent_id"] == "evil"


def test_agent_change_model_unknown_agent_404(client):
    r = client.put("/api/web/config/agents/ghost/model", json={"model": "gpt-x"})
    assert r.status_code == 404


# ── MODELS add/remove + provider catalog ──────────────────────────────


def test_providers_catalog(client):
    resp = client.get("/api/config/providers")
    assert resp.status_code == 200
    ps = resp.json()["providers"]
    assert ps and all("id" in p and "base_url" in p and "api_format" in p for p in ps)


def test_providers_catalog_carries_models_source_kind(client):
    """Every provider entry names its models_source kind — the form gates the
    typed-model-id path to kind == "custom" (everything listable is
    picker-only), so the kind must cross the boundary."""
    ps = client.get("/api/config/providers").json()["providers"]
    assert all(p.get("kind") in {"api", "picklist", "local_probe", "custom"}
               for p in ps)
    assert any(p["kind"] == "custom" for p in ps)   # the typed-id path exists


def test_provider_models_live_list(client, monkeypatch):
    """The add-model picker gets the provider's LIVE list — the same engine
    fetch the TUI runs — filtered to text models, free-flagged."""
    from modulatio import provider_catalog as pc

    fake = [
        pc.CatalogModel(id="m-free", name="MF", provider_id="openrouter", is_free=True),
        pc.CatalogModel(id="m-paid", name="MP", provider_id="openrouter"),
        pc.CatalogModel(id="m-img", name="MI", provider_id="openrouter",
                        modality="image"),
    ]
    monkeypatch.setattr(pc, "fetch_models", lambda p, *, api_key=None, **kw: fake)
    r = client.get("/api/config/providers/openrouter/models")
    assert r.status_code == 200
    models = r.json()["models"]
    assert [m["id"] for m in models] == ["m-free", "m-paid"]   # image filtered out
    assert models[0]["free"] is True and models[1]["free"] is False


def test_provider_models_key_used_but_never_returned(client, monkeypatch):
    """The listing key resolves server-side (the vault env var) and is passed
    to the fetch — but the response body must never carry it."""
    from modulatio import provider_catalog as pc

    monkeypatch.setenv("XAI_API_KEY", "sk-LIST-NEVER-LEAK")
    seen = {}

    def _spy(provider, *, api_key=None, **kw):
        seen["key"] = api_key
        return [pc.CatalogModel(id="m", name="m", provider_id="xai")]

    monkeypatch.setattr(pc, "fetch_models", _spy)
    r = client.get("/api/config/providers/xai/models")
    assert r.status_code == 200
    assert seen["key"] == "sk-LIST-NEVER-LEAK"      # the fetch was key-authed
    assert "sk-LIST-NEVER-LEAK" not in r.text        # ...and the key stayed in


def test_provider_models_fetch_failure_is_502_not_empty_200(client, monkeypatch):
    """A down endpoint / bad credential is a FAILURE the form can show — a 502
    with a plain detail. It must never come back as an empty 200: that shape
    is indistinguishable from "provider has no models" and is what used to
    silently degrade the add-model picker to free-text entry."""
    from modulatio import provider_catalog as pc

    def _boom(provider, *, api_key=None, **kw):
        raise OSError("endpoint down")

    monkeypatch.setattr(pc, "fetch_models", _boom)
    r = client.get("/api/config/providers/openrouter/models")
    assert r.status_code == 502
    assert "model listing failed" in r.json()["detail"]
    assert "endpoint down" not in r.text   # raw exception text stays server-side


def test_provider_models_unknown_provider_422(client):
    r = client.get("/api/config/providers/nope/models")
    assert r.status_code == 422


def test_model_add_and_remove(client):
    from modulatio import model_presets

    add = client.post("/api/config/models/add",
                      json={"provider_id": "openrouter", "model": "meta/llama-3"})
    assert add.status_code == 200
    key = add.json()["key"]
    assert key in model_presets.load_presets()

    rm = client.delete(f"/api/config/models/{key}")
    assert rm.status_code == 200
    assert key not in model_presets.load_presets()


def test_model_add_unknown_provider_422(client):
    r = client.post("/api/config/models/add",
                    json={"provider_id": "nope", "model": "x"})
    assert r.status_code == 422


# ── KEYS (write-only — the security crux) ─────────────────────────────

_BASE = "OPENROUTER_API_KEY"
_SECRET = "sk-DO-NOT-LEAK-abc123"


def _allow_base(client):
    """Register an openrouter model so OPENROUTER_API_KEY becomes a legitimate
    (allowlisted) key handle — the key routes only target configured
    model/service env vars."""
    client.post("/api/config/models/add",
                json={"provider_id": "openrouter", "model": "meta/llama-3"})


def test_key_add_is_set_and_value_never_echoed(client, monkeypatch):
    monkeypatch.delenv(_BASE, raising=False)
    _allow_base(client)
    add = client.post("/api/config/keys",
                      json={"base": _BASE, "value": _SECRET, "label": "primary"})
    assert add.status_code == 200
    slot = add.json()
    assert slot["is_set"] is True
    # THE red-line: the value never crosses the boundary back out.
    assert _SECRET not in add.text
    assert "value" not in slot


def test_key_list_reports_is_set_never_the_value(client, monkeypatch):
    monkeypatch.delenv(_BASE, raising=False)
    _allow_base(client)
    client.post("/api/config/keys",
                json={"base": _BASE, "value": _SECRET, "label": "primary"})
    resp = client.get("/api/config/keys", params={"base": _BASE})
    assert resp.status_code == 200
    assert _SECRET not in resp.text
    slots = resp.json()["slots"]
    assert any(s["is_set"] for s in slots)
    assert all("value" not in s for s in slots)


def test_key_remove(client, monkeypatch):
    monkeypatch.delenv(_BASE, raising=False)
    _allow_base(client)
    ev = client.post("/api/config/keys",
                     json={"base": _BASE, "value": _SECRET}).json()["env_var"]
    rm = client.delete(f"/api/config/keys/{ev}")
    assert rm.status_code == 200
    slots = client.get("/api/config/keys", params={"base": _BASE}).json()["slots"]
    assert not any(s["env_var"] == ev and s["is_set"] for s in slots)


def test_key_add_blank_refused(client):
    _allow_base(client)
    r = client.post("/api/config/keys", json={"base": _BASE, "value": "  "})
    assert r.status_code == 422


# ── WB-1 remediation: keys are scoped to configured model/service handles ─


def test_key_add_refuses_arbitrary_env_var(client, monkeypatch):
    """WB-1 HIGH: the key route must not write ANY env var into the vault —
    only configured model/service key handles. A sandbox-bypass switch like
    MODULATIO_RUN_SHELL_UNSAFE is not a key handle and must be refused."""
    monkeypatch.delenv("MODULATIO_RUN_SHELL_UNSAFE", raising=False)
    r = client.post("/api/config/keys",
                    json={"base": "MODULATIO_RUN_SHELL_UNSAFE", "value": "1"})
    assert r.status_code == 404
    import os
    assert "MODULATIO_RUN_SHELL_UNSAFE" not in os.environ


def test_key_add_allows_catalog_provider_slot_before_first_model(client):
    """The credential-first add flow sets a provider's key BEFORE its first
    model exists. A catalog provider's api-key slot is a known, bounded handle,
    so it's allowed — this is the exact case the gif hit (adding the first xAI
    model with an API key 404'd on 'not a configured key handle')."""
    from modulatio import provider_catalog as pc

    xai_slot = next(a.env_var for a in pc.XAI.auth_options if a.auth_type == "api_key")
    assert client.get("/api/config/keys", params={"base": xai_slot}).status_code == 200
    r = client.post("/api/config/keys", json={"base": xai_slot, "value": "sk-first"})
    assert r.status_code == 200


def test_key_list_refuses_arbitrary_env_var(client):
    r = client.get("/api/config/keys", params={"base": "HOME"})
    assert r.status_code == 404


def test_key_delete_refuses_arbitrary_env_var(client):
    r = client.delete("/api/config/keys/HOME")
    assert r.status_code == 404


# ── SERVICES (catalog + custom; keys via the write-only pool) ─────────


def test_service_catalog_read(client):
    resp = client.get("/api/config/service-catalog")
    assert resp.status_code == 200
    ids = [e["id"] for e in resp.json()["catalog"]]
    assert "tavily" in ids


def test_service_add_from_catalog_and_remove(client):
    from modulatio import services

    add = client.post("/api/config/services/add-catalog", json={"catalog_id": "tavily"})
    assert add.status_code == 200
    assert "tavily" in services.load_services()
    assert client.delete("/api/config/services/tavily").status_code == 200
    assert "tavily" not in services.load_services()


def test_service_add_catalog_unknown_422(client):
    r = client.post("/api/config/services/add-catalog", json={"catalog_id": "nope"})
    assert r.status_code == 422


def test_service_add_custom_gets_opaque_key_handle(client):
    from modulatio import services

    add = client.post("/api/config/services/add-custom", json={
        "name": "My OCR", "base_url": "https://ocr.example.com",
        "auth_shape": "bearer", "capabilities": ["research"]})
    assert add.status_code == 200
    sid = add.json()["id"]
    svc = services.load_services()[sid]
    assert svc.kind == "custom"
    assert svc.env_var.startswith("SVCKEY_")  # opaque — no service name in the handle


def test_service_add_custom_bad_base_url_422(client):
    r = client.post("/api/config/services/add-custom", json={
        "name": "Bad", "base_url": "ftp://nope", "auth_shape": "bearer",
        "capabilities": ["research"]})
    assert r.status_code == 422


def test_service_set_capability_default(client):
    from modulatio import services

    client.post("/api/config/services/add-catalog", json={"catalog_id": "tavily"})
    r = client.post("/api/config/services/default",
                    json={"capability": "research", "service_id": "tavily"})
    assert r.status_code == 200
    assert services.capability_default("research") == "tavily"


# ── RELOAD? no — the ADD-MODEL auth-method mirror (the TUI's AuthStep) ─


def test_providers_catalog_carries_oauth_readiness(client, monkeypatch):
    """An OAuth auth option reports its live signed-in status + hint, so the
    browser's method picker can show what the TUI's AuthStep shows."""
    from modulatio import provider_catalog as pc

    monkeypatch.setattr(pc, "auth_status", lambda a, **k: (False, "sign in first"))
    r = client.get("/api/config/providers")
    xai = next(p for p in r.json()["providers"] if p["id"] == "xai")
    oauth = next(a for a in xai["auth"] if a["auth_type"] == "oauth_xai")
    assert oauth["ready"] is False
    assert "login-xai" in oauth["hint"]        # the catalog hint names the fix
    api = next(a for a in xai["auth"] if a["auth_type"] == "api_key")
    assert "ready" not in api                  # non-oauth options stay plain


def test_model_add_honors_chosen_oauth_auth(client):
    """Adding an xAI model with auth_type=oauth_xai creates an OAuth preset —
    the browser can finally reach the sign-in path the TUI offers."""
    from modulatio import model_presets

    r = client.post("/api/config/models/add", json={
        "provider_id": "xai", "model": "grok-4.5", "auth_type": "oauth_xai"})
    assert r.status_code == 200
    rec = model_presets.load_presets()[r.json()["key"]]
    assert rec["auth_type"] == "oauth_xai"


def test_model_add_rejects_invented_auth(client):
    r = client.post("/api/config/models/add", json={
        "provider_id": "xai", "model": "grok-4.5", "auth_type": "made-up"})
    assert r.status_code == 422


def test_provider_models_lists_with_the_chosen_auth(client, monkeypatch):
    """The live-list fetch resolves its credential from the CHOSEN method —
    an OAuth pick must not silently fall back to the api-key env var."""
    from modulatio import provider_catalog as pc

    seen = {}

    def _spy_listing_key(*, env_var=None, auth_type=None):
        seen["env_var"], seen["auth_type"] = env_var, auth_type
        return "tok"

    monkeypatch.setattr(pc, "listing_key", _spy_listing_key)
    monkeypatch.setattr(pc, "fetch_models", lambda p, *, api_key=None, **kw: [])
    r = client.get("/api/config/providers/xai/models?auth_type=oauth_xai")
    assert r.status_code == 200
    assert seen["auth_type"] == "oauth_xai" and seen["env_var"] is None


def test_oauth_login_routes_start_and_status(client, monkeypatch):
    """The in-app sign-in, from the browser: xai = redirect kind, openai =
    device kind (+ user code); unknown types 422; concurrent 409; the GET
    twin surfaces the flow state. No token ever rides a response."""
    from modulatio import oauth_login

    monkeypatch.setattr(
        oauth_login, "begin_xai_login", lambda: "https://consent.example/x")
    r = client.post("/api/config/oauth-login", json={"auth_type": "oauth_xai"})
    assert r.json() == {"kind": "redirect", "url": "https://consent.example/x"}

    monkeypatch.setattr(
        oauth_login, "begin_openai_login",
        lambda: {"url": "https://verify.example/d", "user_code": "AB-12"})
    r = client.post("/api/config/oauth-login", json={"auth_type": "oauth_openai"})
    assert r.json() == {"kind": "device", "url": "https://verify.example/d",
                        "user_code": "AB-12"}

    assert client.post("/api/config/oauth-login",
                       json={"auth_type": "made-up"}).status_code == 422

    def _busy():
        raise oauth_login.LoginError("a sign-in is already in progress")
    monkeypatch.setattr(oauth_login, "begin_xai_login", _busy)
    assert client.post("/api/config/oauth-login",
                       json={"auth_type": "oauth_xai"}).status_code == 409

    monkeypatch.setattr(
        oauth_login, "login_status", lambda: {"state": "done", "error": ""})
    assert client.get("/api/config/oauth-login").json() == {
        "state": "done", "error": ""}

def test_oauth_login_cancel_route(client, monkeypatch):
    """DELETE releases an abandoned sign-in: pending →
    cancelled True; nothing in flight → False. Never a token on the wire."""
    from modulatio import oauth_login

    monkeypatch.setattr(oauth_login, "cancel_login", lambda: True)
    r = client.delete("/api/config/oauth-login")
    assert r.status_code == 200 and r.json() == {"cancelled": True}

    monkeypatch.setattr(oauth_login, "cancel_login", lambda: False)
    assert client.delete("/api/config/oauth-login").json() == {"cancelled": False}


# ── custom-provider listing + registration overrides ─────────────────────────


def test_get_listing_takes_no_caller_endpoint_or_keyname(client, monkeypatch):
    """The GET listing route lists a provider against its OWN catalog
    definition only. It accepts no base_url/env_var — so it can neither be
    re-pointed at a caller endpoint nor coerced into resolving a caller-named
    process-env secret (the old exfil primitive). Stray query params are inert.
    """
    from modulatio import provider_catalog as pc

    seen = {}

    def _spy(provider, *, env_var=None, auth_type=None, base_url=None, **kw):
        seen.update(env_var=env_var, base_url=base_url)
        return []

    monkeypatch.setattr(pc, "fetch_models_authed", _spy)
    client.get("/api/config/providers/openrouter/models"
               "?base_url=https://evil/v1&env_var=SOME_SECRET")
    assert seen["base_url"] is None                       # never a caller endpoint
    assert seen["env_var"] == pc.get_provider("openrouter").auth_options[0].env_var


def test_custom_probe_uses_key_value_never_process_env(client, monkeypatch):
    """SECURITY (Wild Bill HIGH): the custom probe is a CSRF-protected POST
    whose bearer is the supplied key VALUE — it must never resolve a
    caller-named process-env secret. A secret in os.environ cannot leak: there
    is no env-var name input, and the outbound header carries only the value."""
    from modulatio import provider_catalog as pc

    monkeypatch.setenv("UNRELATED_PROCESS_SECRET", "must-never-leave-this-box")
    captured = {}

    def _capture(url, headers, timeout):
        captured.update(url=url, headers=headers)
        return {"data": [{"id": "probed-model"}]}

    monkeypatch.setattr(pc, "_http_get_json", _capture)
    r = client.post("/api/config/providers/custom/probe",
                    json={"base_url": "https://host/v1", "key": "sk-supplied"})
    assert r.status_code == 200
    assert [m["id"] for m in r.json()["models"]] == ["probed-model"]
    assert captured["url"] == "https://host/v1/models"
    assert captured["headers"] == {"Authorization": "Bearer sk-supplied"}
    assert "must-never-leave-this-box" not in str(captured)   # env secret stayed home


def test_custom_probe_is_post_only_csrf_protected():
    """The probe is a state-changing POST, so the CSRF header is mandatory
    (a cross-origin page can't set it) and a GET to the path is not routed —
    no safe-method probe of a caller-chosen URL. Uses a header-LESS client so
    the 403 boundary is actually exercised (not the fixture's always-on header).
    """
    from fastapi.testclient import TestClient

    from modulatio import vault
    from modulatio.web.app import create_app

    vault.init_project("web", "Web", "o")
    bare = TestClient(create_app(stub=True), base_url="http://localhost")  # no header
    r = bare.post("/api/config/providers/custom/probe",
                  json={"base_url": "https://host/v1", "key": "x"})
    assert r.status_code == 403                          # CSRF header required
    assert bare.get("/api/config/providers/custom/probe").status_code in (404, 405)


def test_custom_probe_rejects_nonhttp_base_url(client):
    for bad in ("file:///etc/passwd", "ftp://host/v1", "https://host/v1?x=/models", ""):
        r = client.post("/api/config/providers/custom/probe", json={"base_url": bad})
        assert r.status_code == 422, bad


def test_custom_probe_keyless_and_failure_is_empty(client, monkeypatch):
    """Keyless probe sends no Authorization; an unreachable endpoint lists empty
    (the typed id is custom's sanctioned path), never an error."""
    from modulatio import provider_catalog as pc

    seen = {}

    def _keyless(url, headers, timeout):
        seen.update(headers=headers)
        raise OSError("unreachable")

    monkeypatch.setattr(pc, "_http_get_json", _keyless)
    r = client.post("/api/config/providers/custom/probe",
                    json={"base_url": "https://host/v1"})
    assert r.status_code == 200 and r.json()["models"] == []
    assert seen["headers"] == {}                              # no bearer when keyless


def test_model_add_custom_stores_key_under_opaque_handle(client):
    """A browser-added custom model persists its endpoint and stores the key
    under an OPAQUE engine-minted handle (never a caller-named env var), with
    the value written AFTER registration and never echoed back."""
    from modulatio import model_presets, provider_keys

    r = client.post("/api/config/models/add", json={
        "provider_id": "custom", "model": "my-33b", "auth_type": "api_key",
        "base_url": "https://host/v1", "key": "sk-custom-secret"})
    assert r.status_code == 200
    key = r.json()["key"]
    try:
        preset = model_presets.load_presets()[key]
        assert preset["base_url"] == "https://host/v1"
        handle = preset["auth_config"]["env_var"]
        assert handle.startswith("CUSTOMKEY_")          # opaque, not caller-named
        assert any(s["is_set"] for s in provider_keys.list_keys(handle))
        assert "sk-custom-secret" not in r.text
    finally:
        model_presets.remove_preset(key)


def test_custom_add_cannot_self_authorize_process_control_env(client, monkeypatch):
    """SECURITY (Wild Bill HIGH): a custom add must not write a caller-named
    process-control env var. Even if the body names BASH_ENV as the slot, the
    key is stored under an opaque handle — BASH_ENV is never set in the live
    process (it would be executed by future non-interactive bash)."""
    import os

    monkeypatch.delenv("BASH_ENV", raising=False)
    r = client.post("/api/config/models/add", json={
        "provider_id": "custom", "model": "m", "auth_type": "api_key",
        "base_url": "https://operator.invalid/v1",
        "env_var": "BASH_ENV",                            # ignored — no caller slot names
        "key": "/tmp/attacker-controlled-shell-init"})
    assert r.status_code == 200
    assert "BASH_ENV" not in os.environ
    from modulatio import model_presets
    model_presets.remove_preset(r.json()["key"])


def test_model_add_custom_requires_valid_endpoint(client):
    """No endpoint (or a non-http one) is refused — no dead preset via a
    handcrafted request."""
    base = {"provider_id": "custom", "model": "m", "auth_type": "api_key"}
    assert client.post("/api/config/models/add", json=base).status_code == 422
    assert client.post("/api/config/models/add",
                       json={**base, "base_url": "ftp://h/v1"}).status_code == 422


def test_model_add_catalog_provider_ignores_endpoint_override(client):
    """A catalog provider's endpoint is a catalog fact — the body's base_url
    must not re-point it."""
    from modulatio import model_presets
    from modulatio import provider_catalog as pc

    r = client.post("/api/config/models/add", json={
        "provider_id": "openrouter", "model": "x/y",
        "base_url": "https://evil/v1"})
    assert r.status_code == 200
    key = r.json()["key"]
    try:
        preset = model_presets.load_presets()[key]
        assert preset["base_url"] == pc.get_provider("openrouter").base_url
    finally:
        model_presets.remove_preset(key)
