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


def test_provider_models_fetch_failure_degrades_empty(client, monkeypatch):
    """A down endpoint / missing key degrades to [] (the form falls back to a
    typed model id) — never a 500."""
    from modulatio import provider_catalog as pc

    def _boom(provider, *, api_key=None, **kw):
        raise OSError("endpoint down")

    monkeypatch.setattr(pc, "fetch_models", _boom)
    r = client.get("/api/config/providers/openrouter/models")
    assert r.status_code == 200
    assert r.json()["models"] == []


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
    model/service env vars (WB-1)."""
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
