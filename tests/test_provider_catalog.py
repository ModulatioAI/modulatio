"""Tests for the provider catalog — the Configuration tab's data layer.

The catalog describes a provider richly enough that the configurator fills in
model id, base_url, api_format, and auth for the operator — who only ever
supplies the key. These cover OpenRouter (the first provider built out): free
detection, pinned models (auto + free), the curated-default view, search, and
the catalog→preset wiring onto the existing model_presets backend.
"""
from __future__ import annotations

from modulatio import model_presets, provider_catalog as pc
from pathlib import Path
from modulatio.provider_catalog import get_provider, parse_models
import pytest

# A trimmed payload mirroring OpenRouter's /models shape.
PAYLOAD = {
    "data": [
        {"id": "openrouter/auto", "name": "Auto Router", "context_length": 200000,
         "pricing": {"prompt": "0.000001", "completion": "0.000002"},
         "created": 1700000000},
        {"id": "openrouter/free", "name": "Free Router", "context_length": 100000,
         "pricing": {"prompt": "0", "completion": "0"}, "created": 1769000000},
        {"id": "anthropic/claude-sonnet-4.5", "name": "Claude Sonnet 4.5",
         "context_length": 200000,
         "pricing": {"prompt": "0.000003", "completion": "0.000015"},
         "created": 1769900000},
        {"id": "google/gemma-4-31b-it:free", "name": "Gemma 4 31B",
         "context_length": 8192, "pricing": {"prompt": "0", "completion": "0"},
         "created": 1769800000},
        {"id": "some/obscure-model", "name": "Obscure", "context_length": 4096,
         "pricing": {"prompt": "0.0001", "completion": "0.0002"},
         "created": 1600000000},
    ]
}


def test_openrouter_is_registered_with_one_key_auth():
    p = pc.get_provider("openrouter")
    assert p is not None
    assert p.base_url == "https://openrouter.ai/api/v1"
    assert p.api_format == "openai"
    # one API-key auth option; the user supplies only this key
    assert [a.auth_type for a in p.auth_options] == ["api_key"]
    assert p.auth_options[0].env_var == "OPENROUTER_API_KEY"
    assert p.signup_url  # "go here to get a key"
    assert p.pinned_models == ["openrouter/auto", "openrouter/free"]


def test_parse_flags_free_by_zero_pricing():
    models = pc.parse_models(pc.OPENROUTER, PAYLOAD)
    by_id = {m.id: m for m in models}
    assert by_id["openrouter/free"].is_free
    assert by_id["google/gemma-4-31b-it:free"].is_free
    assert not by_id["openrouter/auto"].is_free
    assert not by_id["anthropic/claude-sonnet-4.5"].is_free


def test_free_models_returns_all_free_uncapped():
    models = pc.parse_models(pc.OPENROUTER, PAYLOAD)
    free = pc.free_models(models)
    assert {m.id for m in free} == {"openrouter/free", "google/gemma-4-31b-it:free"}


def test_pinned_synthesized_when_absent_from_feed():
    # a feed missing openrouter/auto still surfaces it (pinned, synthesized)
    payload = {"data": [m for m in PAYLOAD["data"] if m["id"] != "openrouter/auto"]}
    models = pc.apply_pinned(pc.OPENROUTER, pc.parse_models(pc.OPENROUTER, payload))
    assert models[0].id == "openrouter/auto"  # synthesized + leads
    assert models[1].id == "openrouter/free"


def test_curated_default_leads_with_pinned_then_flagships():
    models = pc.apply_pinned(pc.OPENROUTER, pc.parse_models(pc.OPENROUTER, PAYLOAD))
    curated = pc.curated_default(pc.OPENROUTER, models, limit=30)
    assert [m.id for m in curated[:2]] == ["openrouter/auto", "openrouter/free"]
    # the flagship (anthropic/) surfaces ahead of the obscure tail
    ids = [m.id for m in curated]
    assert ids.index("anthropic/claude-sonnet-4.5") < ids.index("some/obscure-model")


def test_curated_default_respects_the_cap():
    models = pc.parse_models(pc.OPENROUTER, PAYLOAD)
    assert len(pc.curated_default(pc.OPENROUTER, models, limit=3)) == 3


def test_search_reaches_the_full_catalog():
    models = pc.parse_models(pc.OPENROUTER, PAYLOAD)
    assert [m.id for m in pc.search(models, "claude")] == ["anthropic/claude-sonnet-4.5"]
    assert pc.search(models, "") == list(models)  # empty query → everything


def test_fetch_models_pins_auto_and_free(monkeypatch):
    monkeypatch.setattr(pc, "_http_get_json", lambda url, headers, timeout: PAYLOAD)
    models = pc.fetch_models(pc.OPENROUTER)
    assert [m.id for m in models[:2]] == ["openrouter/auto", "openrouter/free"]
    assert len(models) == 5


def test_preset_kwargs_autofills_everything_but_the_key():
    p = pc.OPENROUTER
    model = pc.CatalogModel(id="openrouter/free", name="Free Router", provider_id="openrouter")
    kwargs = pc.preset_kwargs(p, model, p.auth_options[0])
    # the operator typed nothing here — all derived from provider + pick
    assert kwargs["base_url"] == "https://openrouter.ai/api/v1"
    assert kwargs["api_format"] == "openai"
    assert kwargs["auth_type"] == "api_key"
    assert kwargs["auth_config"] == {"env_var": "OPENROUTER_API_KEY"}
    assert kwargs["model"] == "openrouter/free"
    assert kwargs["label"] == "Free Router"
    assert kwargs["key"]  # auto-slugged, not typed


# ── Ollama Cloud ────────────────────────────────────────────────────────────

OLLAMA_PAYLOAD = {
    "object": "list",
    "data": [
        {"id": "deepseek-v4-pro", "object": "model", "created": 1776988800,
         "owned_by": "ollama"},
        {"id": "gemma4:31b", "object": "model", "created": 1775149200,
         "owned_by": "ollama"},
        {"id": "qwen3-next:80b", "object": "model", "created": 1757462400,
         "owned_by": "ollama"},
    ],
}


def test_ollama_cloud_registered_with_public_listing():
    p = pc.get_provider("ollama_cloud")
    assert p is not None
    assert p.base_url == "https://ollama.com/v1"
    assert p.api_format == "openai"
    assert p.auth_options[0].env_var == "OLLAMA_API_KEY"
    assert p.models_source.auth_required is False  # /models is public
    assert p.signup_url


def test_free_sections_carry_a_truthful_limit_caveat():
    # free is never presented as unlimited — OpenRouter's free section carries a caveat
    assert "limit" in pc.OPENROUTER.free_note.lower()


def test_ollama_cloud_models_not_blanket_free():
    models = pc.parse_models(pc.OLLAMA_CLOUD, OLLAMA_PAYLOAD)
    assert {m.id for m in models} == {"deepseek-v4-pro", "gemma4:31b", "qwen3-next:80b"}
    # No per-model price signal in the feed + not every Cloud model is free, so
    # none are blanket-tagged free (avoids over-claiming a paid model as free).
    assert not any(m.is_free for m in models)
    # Ollama feeds bare ids with no friendly name → fall back to the id
    assert next(m for m in models if m.id == "gemma4:31b").name == "gemma4:31b"


def test_curated_default_matches_flagships_on_bare_ollama_ids():
    # bare ids (no vendor/ prefix) still match flagship stems, newest first
    models = pc.parse_models(pc.OLLAMA_CLOUD, OLLAMA_PAYLOAD)
    curated = pc.curated_default(pc.OLLAMA_CLOUD, models, limit=10)
    assert curated[0].id == "deepseek-v4-pro"  # newest flagship leads


def test_ollama_preset_kwargs_carry_the_ollama_key():
    p = pc.OLLAMA_CLOUD
    model = pc.CatalogModel(id="deepseek-v4-pro", name="deepseek-v4-pro",
                            provider_id="ollama_cloud")
    kwargs = pc.preset_kwargs(p, model, p.auth_options[0])
    assert kwargs["base_url"] == "https://ollama.com/v1"
    assert kwargs["auth_config"] == {"env_var": "OLLAMA_API_KEY"}
    assert kwargs["model"] == "deepseek-v4-pro"


# ── xAI (multi-modality: text + image + video) ──────────────────────────────

XAI_TEXT = {"data": [
    {"id": "grok-4.3", "created": 1773014400, "object": "model"},
    {"id": "grok-build-0.1", "created": 1773100000, "object": "model"},
]}
XAI_IMAGE = {"data": [
    {"id": "grok-imagine-image", "created": 1773000000, "object": "model"},
]}
XAI_VIDEO = {"data": [
    {"id": "grok-imagine-video", "created": 1773000000, "object": "model"},
]}


def _fake_xai_http(url, headers, timeout):
    if "language-models" in url:
        return XAI_TEXT
    if "image-generation-models" in url:
        return XAI_IMAGE
    if "video-generation-models" in url:
        return XAI_VIDEO
    return {"data": []}


def test_xai_registered_no_free_key_required_to_list():
    p = pc.get_provider("xai")
    assert p is not None
    assert p.base_url == "https://api.x.ai/v1"
    assert p.auth_options[0].env_var == "XAI_API_KEY"
    assert p.free_detect == "none"  # no free tier
    assert p.models_source.auth_required is True  # listing needs the key
    # text primary + image/video extras
    assert p.models_source.modality == "text"
    assert {s.modality for s in p.extra_sources} == {"image", "video"}


def test_xai_fetch_tags_models_by_modality(monkeypatch):
    monkeypatch.setattr(pc, "_http_get_json", _fake_xai_http)
    models = pc.fetch_models(pc.XAI, api_key="x")
    assert len(pc.of_modality(models, "text")) == 2
    assert [m.id for m in pc.of_modality(models, "image")] == ["grok-imagine-image"]
    assert [m.id for m in pc.of_modality(models, "video")] == ["grok-imagine-video"]
    # none of these are free
    assert not any(m.is_free for m in models)


def test_role_assignment_filters_to_text_models(monkeypatch):
    """A leader/qc/producer pick wants text models only — image/video are
    listed but never offered for a chat role."""
    monkeypatch.setattr(pc, "_http_get_json", _fake_xai_http)
    models = pc.fetch_models(pc.XAI, api_key="x")
    text = pc.of_modality(models, "text")
    assert all(m.modality == "text" for m in text)
    assert {m.id for m in text} == {"grok-4.3", "grok-build-0.1"}


# ── xAI Grok OAuth (Modulatio's own sign-in — `modulatio auth login-xai`) ────


def test_xai_offers_both_api_key_and_oauth():
    p = pc.get_provider("xai")
    assert [a.auth_type for a in p.auth_options] == ["api_key", "oauth_xai"]
    oauth = p.auth_options[1]
    # The OAuth path is FUNCTIONAL (Modulatio's own PKCE sign-in whose token
    # carries API access) — the old borrowed-CLI-token placeholder era, when
    # this option was beta-flagged non-functional, is over.
    assert oauth.beta is False


def test_xai_oauth_option_names_the_sign_in_command():
    """The option's hint tells the operator HOW to sign in (the login
    command) and never claims the path is unsupported."""
    p = pc.get_provider("xai")
    oauth = next(a for a in p.auth_options if a.auth_type == "oauth_xai")
    blurb = f"{oauth.label} {oauth.oauth_hint or ''}".lower()
    assert "login-xai" in blurb
    assert "not supported" not in blurb and "not functional" not in blurb


def test_oauth_xai_strategy_is_registered():
    from modulatio import auth_strategies
    assert "oauth_xai" in auth_strategies.registered_auth_types()


def test_xai_oauth_ignores_foreign_credential_files(tmp_path, monkeypatch):
    """Token isolation: another tool's credential file is never a Modulatio
    credential — only Modulatio's own store (minted by `modulatio auth
    login-xai`) counts."""
    from modulatio import oauth_helpers
    monkeypatch.setattr(
        oauth_helpers, "MODULATIO_XAI_OAUTH_FILE", tmp_path / "own.json")
    assert not oauth_helpers.has_xai_credentials()
    assert oauth_helpers.read_xai_token() is None
    oauth_helpers.write_xai_credentials(
        {"access_token": "own-tok", "refresh_token": "own-ref"})
    assert oauth_helpers.has_xai_credentials()
    assert oauth_helpers.read_xai_token() == "own-tok"
    assert oauth_helpers.read_xai_refresh_token() == "own-ref"


def test_xai_oauth_auth_status_reflects_own_login(tmp_path, monkeypatch):
    from modulatio import oauth_helpers
    oauth = pc.get_provider("xai").auth_options[1]
    monkeypatch.setattr(
        oauth_helpers, "MODULATIO_XAI_OAUTH_FILE", tmp_path / "own.json")
    # not signed in → not ready, hint names Modulatio's OWN sign-in command
    ok, hint = pc.auth_status(oauth)
    assert not ok and "login-xai" in hint
    # signed in → ready
    oauth_helpers.write_xai_credentials({"access_token": "t"})
    ok, hint = pc.auth_status(oauth)
    assert ok and hint == ""


def test_existing_providers_default_to_text_modality():
    models = pc.parse_models(pc.OPENROUTER, PAYLOAD)
    assert all(m.modality == "text" for m in models)


# ── Anthropic (dual auth, picklist source, no free tier) ────────────────────


def test_anthropic_dual_auth_anthropic_format_no_free():
    p = pc.get_provider("anthropic")
    assert p is not None
    assert p.api_format == "anthropic"
    assert p.free_detect == "none"
    # API key only — the subscription path is Clay (CLAUDE_CLI), not straight
    # OAuth against api.anthropic.com (which 401s a subscription token).
    assert [a.auth_type for a in p.auth_options] == ["api_key"]
    assert p.models_source.kind == "picklist"
    assert p.models_source.picklist_key == "anthropic"


def test_anthropic_lists_claude_models_from_picklist():
    # no network, no key — reads the curated seed
    models = pc.fetch_models(pc.ANTHROPIC)
    ids = {m.id for m in models}
    assert ids and all(i.startswith("claude-") for i in ids)
    assert all(not m.is_free for m in models)  # no free tier
    assert all(m.modality == "text" for m in models)


def test_openai_dual_auth_picklist_no_free():
    p = pc.get_provider("openai")
    assert p is not None
    assert p.api_format == "openai"
    # API key only — the subscription path is OPENAI_CODEX, not straight OAuth
    # against api.openai.com (which 401s a subscription token).
    assert [a.auth_type for a in p.auth_options] == ["api_key"]
    assert p.models_source.kind == "picklist"
    assert p.models_source.picklist_key == "openai"
    assert p.free_detect == "none"


def test_openai_lists_gpt_models_from_picklist():
    models = pc.fetch_models(pc.OPENAI)
    ids = {m.id for m in models}
    assert ids and all(i.startswith("gpt-") for i in ids)
    assert all(not m.is_free and m.modality == "text" for m in models)


# ── NVIDIA (public listing, free catalog tier @ 40 RPM) ─────────────────────

NVIDIA_PAYLOAD = {"data": [
    {"id": "meta/llama-3.1-70b-instruct", "object": "model",
     "created": 1700000000, "owned_by": "meta"},
    {"id": "nvidia/nemotron-4-340b-instruct", "object": "model",
     "created": 1700000001, "owned_by": "nvidia"},
    {"id": "mistralai/mixtral-8x22b-instruct", "object": "model",
     "created": 1700000002, "owned_by": "mistralai"},
]}


def test_nvidia_public_listing_not_blanket_free():
    p = pc.get_provider("nvidia")
    assert p is not None
    assert p.base_url == "https://integrate.api.nvidia.com/v1"
    assert p.auth_options[0].env_var == "NVIDIA_API_KEY"
    assert p.models_source.auth_required is False  # listing is public
    # Remote billable API, no per-model free/paid signal → not blanket-tagged free.
    assert p.free_detect == "none"


def test_nvidia_models_not_blanket_free(monkeypatch):
    monkeypatch.setattr(pc, "_http_get_json", lambda url, h, t: NVIDIA_PAYLOAD)
    models = pc.fetch_models(pc.NVIDIA)
    assert len(models) == 3
    assert not any(m.is_free for m in models)
    assert all(m.modality == "text" for m in models)


def test_google_gemini_openai_compat_key_gated_free_tier():
    p = pc.get_provider("google")
    assert p is not None
    assert p.base_url == "https://generativelanguage.googleapis.com/v1beta/openai"
    assert p.api_format == "openai"  # Gemini's OpenAI-compat endpoint
    assert p.auth_options[0].env_var == "GEMINI_API_KEY"
    assert p.models_source.auth_required is True  # key needed to list
    assert p.free_detect == "none"  # no per-model free signal → not blanket-tagged free
    assert p.signup_url


def test_google_models_fetched_strips_models_prefix(monkeypatch):
    # Gemini lists "models/gemini-…"; the bare id is what the chat endpoint wants
    payload = {"data": [
        {"id": "models/gemini-3-flash", "object": "model"},
        {"id": "models/gemini-3-pro", "object": "model"},
    ]}
    monkeypatch.setattr(pc, "_http_get_json", lambda u, h, t: payload)
    models = pc.fetch_models(pc.GOOGLE, api_key="x")
    assert {m.id for m in models} == {"gemini-3-flash", "gemini-3-pro"}
    assert not any(m.is_free for m in models)
    # gemini auto-infers caps from the family table
    from modulatio import model_capabilities as mc
    assert mc.infer("gemini-3-pro")[2]


def test_google_classifies_mixed_modalities_from_one_list(monkeypatch):
    payload = {"data": [
        {"id": "models/gemini-3-pro", "object": "model"},
        {"id": "models/gemini-2.5-flash-preview-tts", "object": "model"},
        {"id": "models/gemini-3-pro-image", "object": "model"},
        {"id": "models/veo-3", "object": "model"},
        {"id": "models/text-embedding-004", "object": "model"},
    ]}
    monkeypatch.setattr(pc, "_http_get_json", lambda u, h, t: payload)
    models = pc.fetch_models(pc.GOOGLE, api_key="x")
    mod = {m.id: m.modality for m in models}
    assert mod["gemini-3-pro"] == "text"
    assert mod["gemini-2.5-flash-preview-tts"] == "audio"
    assert mod["gemini-3-pro-image"] == "image"
    assert mod["veo-3"] == "video"
    assert mod["text-embedding-004"] == "embedding"
    # role assignment (text) excludes the non-chat ones
    assert [m.id for m in pc.of_modality(models, "text")] == ["gemini-3-pro"]


def test_extended_family_table_infers_new_families():
    from modulatio import model_capabilities as mc
    # families we just catalogued now infer non-empty caps (were neutral before)
    assert mc.infer("gpt-oss:120b")[2]
    assert mc.infer("minimax-m3")[2]
    assert mc.infer("ministral-3:8b")[2]
    assert mc.infer("nemotron-3-super")[2]
    # ministral stays distinct from mistral (different tier)
    assert mc.infer("ministral-3:8b")[0] == "budget"


def test_preset_kwargs_pool_flag():
    p = pc.OPENROUTER
    model = pc.CatalogModel(id="x/y", name="Y", provider_id="openrouter")
    base = pc.preset_kwargs(p, model, p.auth_options[0])
    assert "pool" not in base["auth_config"]
    pooled = pc.preset_kwargs(p, model, p.auth_options[0], pool=True)
    assert pooled["auth_config"] == {"env_var": "OPENROUTER_API_KEY", "pool": True}


def test_preset_kwargs_can_override_inferred_caps():
    p = pc.OPENROUTER
    model = pc.CatalogModel(id="x/y", name="Y", provider_id="openrouter")
    base = pc.preset_kwargs(p, model, p.auth_options[0])
    assert "capability_tags" not in base  # unset → inference handles it
    over = pc.preset_kwargs(
        p, model, p.auth_options[0],
        capability_tags=["vision"], model_tier="strategic", cost_class="premium-cloud",
    )
    assert over["capability_tags"] == ["vision"]
    assert over["model_tier"] == "strategic"


# ── Locals (Ollama-local / LM Studio / llama.cpp) + custom ──────────────────


def test_locals_are_localhost_no_auth_probe_sources():
    for pid, port in [("ollama_local", 11434), ("lm_studio", 1234),
                      ("llama_cpp", 8080)]:
        p = pc.get_provider(pid)
        assert p is not None
        assert p.base_url == f"http://localhost:{port}/v1"
        assert p.auth_options[0].auth_type == "none"
        assert p.models_source.kind == "local_probe"
        assert p.free_detect == "all"


def test_local_probe_empty_when_server_down(monkeypatch):
    # unreachable local server → empty list, never an error
    def boom(url, headers, timeout):
        raise OSError("connection refused")
    monkeypatch.setattr(pc, "_http_get_json", boom)
    assert pc.fetch_models(pc.OLLAMA_LOCAL) == []


def test_local_probe_lists_loaded_models(monkeypatch):
    payload = {"data": [{"id": "llama3.3:70b", "object": "model"},
                        {"id": "qwen3:32b", "object": "model"}]}
    monkeypatch.setattr(pc, "_http_get_json", lambda u, h, t: payload)
    models = pc.fetch_models(pc.LM_STUDIO)
    assert {m.id for m in models} == {"llama3.3:70b", "qwen3:32b"}
    assert all(m.is_free for m in models)


def test_custom_provider_lists_nothing_user_fills_it():
    p = pc.get_provider("custom")
    assert p is not None
    assert p.base_url == ""  # operator supplies it
    assert p.models_source.kind == "custom"
    assert pc.fetch_models(p) == []  # no listing — model id is typed in


# ── OAuth / key setup status (ties to the engine's auth strategies) ──────────


def test_auth_status_api_key_ready_when_env_set(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    ok, hint = pc.auth_status(pc.OPENROUTER.auth_options[0])
    assert ok and hint == ""


def test_auth_status_api_key_gives_setup_hint_when_missing(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    ok, hint = pc.auth_status(pc.OPENROUTER.auth_options[0])
    assert not ok and hint  # tells the operator how to set it up


def test_auth_status_oauth_reports_login_hint(monkeypatch):
    # Codex OAuth option → status reflects whether `codex login` is done
    oauth = pc.OPENAI_CODEX.auth_options[0]
    assert oauth.auth_type == "oauth_openai"
    ok, hint = pc.auth_status(oauth)
    assert isinstance(ok, bool)
    if not ok:
        assert hint  # a real setup hint, e.g. "run `claude login`"


def test_catalog_pick_registers_a_real_preset(tmp_path, monkeypatch):
    """The end-to-end wiring: a catalog pick becomes a model_presets entry via
    the existing add_preset backend — no reimplementation."""
    monkeypatch.setattr(model_presets, "PRESETS_FILE", tmp_path / "model_presets.json")
    p = pc.OPENROUTER
    model = pc.CatalogModel(id="openrouter/free", name="Free Router", provider_id="openrouter")
    kwargs = pc.preset_kwargs(p, model, p.auth_options[0])
    key = kwargs.pop("key")
    entry = model_presets.add_preset(key, **kwargs)
    assert entry["base_url"] == "https://openrouter.ai/api/v1"
    assert entry["model"] == "openrouter/free"
    assert model_presets.get_preset(key) is not None


def test_capability_flags_maps_litellm_signals(monkeypatch):
    """Compact picker letters come straight from litellm's free supports_* probes:
    reasoning→r, vision→v, tools(function-calling)→t."""
    import litellm
    pc._CAP_CACHE.clear()
    monkeypatch.setattr(litellm, "supports_reasoning", lambda m: True)
    monkeypatch.setattr(litellm, "supports_vision", lambda m: False)
    monkeypatch.setattr(litellm, "supports_function_calling", lambda m: True)
    assert pc.capability_flags("any/model") == "rt"


def test_capability_flags_swallows_probe_errors(monkeypatch):
    """An unknown id (probe raises) must not crash the picker — that letter is
    simply dropped, the rest still contribute."""
    import litellm
    pc._CAP_CACHE.clear()

    def boom(_m):
        raise RuntimeError("unknown model id")

    monkeypatch.setattr(litellm, "supports_reasoning", boom)
    monkeypatch.setattr(litellm, "supports_vision", lambda m: True)
    monkeypatch.setattr(litellm, "supports_function_calling", boom)
    assert pc.capability_flags("weird/id") == "v"


# ── capability tags discovered inline in the /models feed (picker letters) ────


def test_parse_models_extracts_capability_tags_from_openrouter_shape():
    payload = {"data": [
        {"id": "x/vision-tools", "name": "VT",
         "architecture": {"input_modalities": ["text", "image"]},
         "supported_parameters": ["tools", "reasoning", "temperature"]},
        {"id": "x/plain", "name": "P"},  # no capability fields → no tags
    ]}
    out = pc.parse_models(pc.get_provider("openrouter"), payload)
    by_id = {m.id: m for m in out}
    # canonical r/v/t order, only the caps the feed actually carried
    assert by_id["x/vision-tools"].capability_tags == ["reasoning", "vision", "tools"]
    assert by_id["x/plain"].capability_tags == []


def test_parse_models_extracts_inline_capabilities_list():
    payload = {"data": [
        {"id": "ollama/llava", "name": "Llava", "capabilities": ["vision", "tools"]},
    ]}
    out = pc.parse_models(pc.get_provider("ollama_cloud"), payload)
    assert out[0].capability_tags == ["vision", "tools"]


def test_capability_flags_for_prefers_feed_tags_over_litellm(monkeypatch):
    # When the feed carries caps, litellm is NOT consulted (would be "rt" here).
    monkeypatch.setattr(pc, "capability_flags", lambda mid: "rt")
    m = pc.CatalogModel(id="x/y", name="Y", provider_id="openrouter",
                        capability_tags=["vision", "tools"])
    assert pc.capability_flags_for(m) == "vt"


def test_capability_flags_for_falls_back_to_litellm_when_no_feed_tags(monkeypatch):
    monkeypatch.setattr(pc, "capability_flags", lambda mid: "r")
    m = pc.CatalogModel(id="x/unknown", name="U", provider_id="openrouter")
    assert pc.capability_flags_for(m) == "r"


# ═══ fold: test_provider_catalog_low_audit.py ═══
# LOW-severity audit regressions for provider_catalog.
#
# #75 — OpenRouter free-detection must respect non-token pricing dimensions
#       (per-request / per-image), not just prompt+completion.
# #76 — curated_default must keep pinned ids even when the feed dropped them,
#       consistent with apply_pinned (no silent pin loss).


# ── #75: free-detection respects request / image pricing ─────────────────────


def test_zero_token_rate_but_billed_per_request_is_not_free():
    # prompt+completion are 0 but the model bills per request → NOT free.
    model = {
        "id": "vendor/charges-per-request",
        "pricing": {"prompt": "0", "completion": "0", "request": "0.001"},
    }
    assert not pc._is_free(model, "pricing_zero")


def test_zero_token_rate_but_billed_per_image_is_not_free():
    model = {
        "id": "vendor/charges-per-image",
        "pricing": {"prompt": "0", "completion": "0", "image": "0.002"},
    }
    assert not pc._is_free(model, "pricing_zero")


def test_zero_everywhere_stays_free():
    # explicit zeros across all dimensions → free
    model = {
        "id": "vendor/truly-free",
        "pricing": {
            "prompt": "0", "completion": "0", "request": "0", "image": "0",
        },
    }
    assert pc._is_free(model, "pricing_zero")


def test_missing_extra_fields_still_free():
    # most free models omit request/image entirely — must still read as free
    model = {"id": "vendor/free", "pricing": {"prompt": "0", "completion": "0"}}
    assert pc._is_free(model, "pricing_zero")


# ── #76: curated_default keeps pins the feed dropped ─────────────────────────


def test_curated_default_keeps_pin_missing_from_feed():
    # a feed that dropped a pinned id, fed straight to curated_default
    # (not via apply_pinned) must still surface every pin, leading the list.
    payload = {
        "data": [
            {"id": "google/gemma-4-31b-it:free",
             "pricing": {"prompt": "0", "completion": "0"}, "created": 1769800000},
        ]
    }
    models = pc.parse_models(pc.OPENROUTER, payload)  # no apply_pinned
    curated = pc.curated_default(pc.OPENROUTER, models, limit=30)
    ids = [m.id for m in curated]
    assert ids[:2] == ["openrouter/auto", "openrouter/free"]  # both pins survive
    assert "google/gemma-4-31b-it:free" in ids


# ═══ fold: test_provider_catalog_preship.py ═══
# 0.9.0 pre-ship regressions for provider_catalog error-path hardening.
#
# Three malformed-feed cases that previously aborted the entire catalog parse
# (or the whole fetch) instead of degrading per-entry:
#   1. _is_free with a truthy non-dict `pricing` (e.g. "free") -> AttributeError
#   2. parse_models with a truthy non-string `id` (e.g. int) -> AttributeError /
#      pydantic ValidationError
#   3. _load_picklist when the seed file is missing/corrupt -> JSON/OSError


# ── 1. _is_free: non-dict pricing must not crash, just be treated as unknown ──


def test_is_free_non_dict_pricing_does_not_crash():
    # A feed that returns pricing as a string ("free") used to raise
    # AttributeError out of parse_models, aborting the whole catalog.
    model = {"id": "vendor/model", "pricing": "free"}
    # Should not raise; a non-dict pricing can't prove zero rates -> not free.
    assert pc._is_free(model, "pricing_zero") is False


def test_is_free_list_pricing_does_not_crash():
    model = {"id": "vendor/model", "pricing": ["0", "0"]}
    assert pc._is_free(model, "pricing_zero") is False


def test_is_free_zero_dict_pricing_still_free():
    # Regression guard: the real zero-priced path is unchanged.
    model = {"id": "vendor/model:free", "pricing": {"prompt": "0", "completion": "0"}}
    assert pc._is_free(model, "pricing_zero") is True


def test_parse_models_survives_non_dict_pricing_entry():
    provider = pc.OPENROUTER  # free_detect="pricing_zero"
    payload = {
        "data": [
            {"id": "good/model", "pricing": {"prompt": "0", "completion": "0"}},
            {"id": "bad/model", "pricing": "free"},  # the poison entry
        ]
    }
    models = pc.parse_models(provider, payload)
    ids = {m.id for m in models}
    # Both entries parse; neither aborts the batch.
    assert "good/model" in ids
    assert "bad/model" in ids


# ── 2. parse_models: non-string truthy id must skip, not abort ───────────────


def test_parse_models_skips_non_string_id_with_strip():
    # Google has id_prefix_strip="models/"; an int id used to hit
    # int.startswith -> AttributeError, killing the whole feed.
    provider = pc.GOOGLE
    payload = {
        "data": [
            {"id": 12345},  # poison: truthy non-string
            {"id": "models/gemini-2.5-flash"},
        ]
    }
    models = pc.parse_models(provider, payload)
    ids = {m.id for m in models}
    assert "gemini-2.5-flash" in ids  # strip still applied to the good one
    assert 12345 not in ids
    assert all(isinstance(m.id, str) for m in models)


def test_parse_models_skips_non_string_id_no_strip():
    # OpenRouter has no strip; an int id used to reach CatalogModel(id=int)
    # and raise pydantic ValidationError.
    provider = pc.OPENROUTER
    payload = {"data": [{"id": 999}, {"id": "ok/model"}]}
    models = pc.parse_models(provider, payload)
    assert {m.id for m in models} == {"ok/model"}


def test_parse_models_coerces_non_string_name():
    provider = pc.OPENROUTER
    payload = {"data": [{"id": "x/y", "name": 7}]}
    models = pc.parse_models(provider, payload)
    assert len(models) == 1
    assert models[0].name == "x/y"  # falls back to id when name isn't a usable str


# ── 3. _load_picklist: missing/corrupt seed degrades to [] ───────────────────


def test_load_picklist_missing_file_returns_empty(monkeypatch):
    # _load_picklist builds Path(__file__).parent / ...; simulate the seed file
    # being absent by raising FileNotFoundError from read_text for that name.
    orig_read_text = Path.read_text

    def _boom(self, *a, **k):
        if self.name == "oauth_model_picklists.json":
            raise FileNotFoundError("seed gone")
        return orig_read_text(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", _boom)
    assert pc._load_picklist("anthropic") == []


def test_load_picklist_corrupt_json_returns_empty(monkeypatch):
    orig_read_text = Path.read_text

    def _garbage(self, *a, **k):
        if self.name == "oauth_model_picklists.json":
            return "{ this is not json ]"
        return orig_read_text(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", _garbage)
    assert pc._load_picklist("anthropic") == []


def test_load_picklist_non_dict_json_returns_empty(monkeypatch):
    orig_read_text = Path.read_text

    def _list_json(self, *a, **k):
        if self.name == "oauth_model_picklists.json":
            return "[1, 2, 3]"
        return orig_read_text(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", _list_json)
    assert pc._load_picklist("anthropic") == []


def test_load_picklist_happy_path_still_works():
    # The real seed file is present and parses for a real OAuth provider.
    out = pc._load_picklist("anthropic")
    assert isinstance(out, list)


# ═══ fold: test_provider_catalog_r2_audit.py ═══
# Regression tests for r2 audit findings in provider_catalog.parse_models.
#
# Covers:
#   - parse_models must not crash on non-list-of-dict /models payloads
#     (error envelopes, scalars, malformed feed rows).
#   - non-integer created/context_length must not abort the whole catalog.


def _provider():
    p = get_provider("openrouter")
    assert p is not None
    return p


def test_error_envelope_dict_does_not_crash():
    # An error body returned with HTTP 200 has no data/models key -> raw becomes
    # the dict itself; iterating its string keys must not raise AttributeError.
    payload = {"error": {"message": "invalid key", "code": 401}}
    assert parse_models(_provider(), payload) == []


def test_scalar_payload_does_not_crash():
    assert parse_models(_provider(), 42) == []
    assert parse_models(_provider(), "boom") == []
    assert parse_models(_provider(), None) == []


def test_malformed_non_dict_rows_are_skipped():
    payload = {"data": ["just-a-string", 7, None, {"id": "good-model"}]}
    out = parse_models(_provider(), payload)
    assert [m.id for m in out] == ["good-model"]


def test_non_integer_fields_do_not_abort_catalog():
    payload = {
        "data": [
            {"id": "m1", "created": "2024-01-01", "context_length": "128k"},
            {"id": "m2", "created": 1700000000, "context_length": 8192},
        ]
    }
    out = parse_models(_provider(), payload)
    assert [m.id for m in out] == ["m1", "m2"]
    # bad row coerces to None instead of dropping the model or raising.
    assert out[0].created is None
    assert out[0].context_length is None
    assert out[1].created == 1700000000
    assert out[1].context_length == 8192


def test_numeric_string_fields_still_coerce():
    payload = {"data": [{"id": "m", "created": "1700000000", "context_length": "8192"}]}
    out = parse_models(_provider(), payload)
    assert out[0].created == 1700000000
    assert out[0].context_length == 8192


def test_bool_field_not_treated_as_int():
    payload = {"data": [{"id": "m", "context_length": True}]}
    out = parse_models(_provider(), payload)
    assert out[0].context_length is None


# ═══ fold: test_provider_catalog_resweep_r3.py ═══
# 0.9.0 pre-ship re-sweep (round 3) regressions for provider_catalog.
#
# Two LOW findings, additive to the existing provider_catalog test modules:
#
#   1. _load_picklist must degrade a NON-LIST seed value (str/dict for a provider
#      key) to [] — never `list("claude-opus-4-8")` (one model id per char) nor a
#      dict's keys.
#   2. preset_kwargs(..., pool=True) must NOT silently drop pooling when the auth
#      option has no resolvable env_var (e.g. CUSTOM's keyed AuthOption); it raises
#      a clear ValueError instead.


# ── 1. _load_picklist: a non-list seed value degrades to [] ──────────────────


def _patch_seed(monkeypatch, raw: str) -> None:
    orig_read_text = Path.read_text

    def _fake(self, *a, **k):
        if self.name == "oauth_model_picklists.json":
            return raw
        return orig_read_text(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", _fake)


def test_load_picklist_string_value_degrades_to_empty(monkeypatch):
    # Before the fix, list("claude-opus-4-8") yielded one id per character.
    _patch_seed(monkeypatch, '{"anthropic": "claude-opus-4-8"}')
    assert pc._load_picklist("anthropic") == []


def test_load_picklist_dict_value_degrades_to_empty(monkeypatch):
    # A dict value would otherwise iterate to its KEYS as model ids.
    _patch_seed(monkeypatch, '{"anthropic": {"claude-opus-4-8": true}}')
    assert pc._load_picklist("anthropic") == []


def test_load_picklist_list_keeps_only_str_entries(monkeypatch):
    # A well-formed list still passes through; non-str junk is dropped.
    _patch_seed(monkeypatch, '{"anthropic": ["claude-opus-4-8", 7, null, "x"]}')
    assert pc._load_picklist("anthropic") == ["claude-opus-4-8", "x"]


# ── 2. preset_kwargs pool=True with no env_var raises, doesn't drop ──────────


def test_preset_kwargs_pool_without_env_var_raises():
    model = pc.CatalogModel(id="m/x", name="X", provider_id="custom")
    # CUSTOM's api_key AuthOption has env_var=None.
    keyed = next(a for a in pc.CUSTOM.auth_options if a.auth_type == "api_key")
    assert keyed.env_var is None
    with pytest.raises(ValueError, match="pool=True requires"):
        pc.preset_kwargs(pc.CUSTOM, model, keyed, pool=True)


def test_preset_kwargs_no_pool_custom_key_is_fine():
    # Without pool, a keyless-env custom api_key option still builds cleanly.
    model = pc.CatalogModel(id="m/x", name="X", provider_id="custom")
    keyed = next(a for a in pc.CUSTOM.auth_options if a.auth_type == "api_key")
    kwargs = pc.preset_kwargs(pc.CUSTOM, model, keyed)
    assert kwargs["auth_type"] == "api_key"
    assert kwargs["auth_config"] is None


def test_preset_kwargs_pool_with_env_var_still_pools():
    # The happy path (named env var) is unchanged: pool lands in auth_config.
    p = pc.get_provider("openrouter")
    model = pc.CatalogModel(id="x/y", name="Y", provider_id="openrouter")
    keyed = next(a for a in p.auth_options if a.auth_type == "api_key")
    kwargs = pc.preset_kwargs(p, model, keyed, pool=True)
    assert kwargs["auth_config"]["pool"] is True
    assert kwargs["auth_config"]["env_var"] == keyed.env_var


# ── fetch_models_authed: the self-healing listing entry point ─────────────────


def _http_401(url="https://api.x.ai/v1/models", code=401):
    import io
    import urllib.error
    return urllib.error.HTTPError(url, code, "denied", hdrs=None, fp=io.BytesIO(b""))


def test_fetch_models_authed_heals_expired_oauth_token(monkeypatch):
    """An aged OAuth access token 401/403s the listing; the entry point must
    refresh ONCE (the model-call retry contract) and re-fetch — a signed-in
    operator never sees an empty picker over token expiry."""
    from modulatio import oauth_refresh

    calls = []

    def _fetch(provider, *, api_key=None, **kw):
        calls.append(api_key)
        if api_key != "fresh-tok":
            raise _http_401(code=403)
        return [pc.CatalogModel(id="grok-4", name="grok-4", provider_id="xai")]

    monkeypatch.setattr(pc, "fetch_models", _fetch)
    monkeypatch.setattr(pc, "listing_key",
                        lambda *, env_var=None, auth_type=None: "stale-tok")
    monkeypatch.setattr(oauth_refresh, "try_refresh", lambda auth_type, failed=None: "fresh-tok")
    models = pc.fetch_models_authed(pc.XAI, auth_type="oauth_xai")
    assert [m.id for m in models] == ["grok-4"]
    assert calls == ["stale-tok", "fresh-tok"]   # exactly one retry


def test_fetch_models_authed_failed_refresh_reraises(monkeypatch):
    """When the refresh can't recover (revoked grant), the original HTTP error
    surfaces — the route turns it into a loud 502, never an empty list."""
    import urllib.error

    from modulatio import oauth_refresh

    def _fetch(provider, *, api_key=None, **kw):
        raise _http_401()

    monkeypatch.setattr(pc, "fetch_models", _fetch)
    monkeypatch.setattr(pc, "listing_key",
                        lambda *, env_var=None, auth_type=None: "stale-tok")
    monkeypatch.setattr(oauth_refresh, "try_refresh", lambda auth_type, failed=None: None)
    with pytest.raises(urllib.error.HTTPError):
        pc.fetch_models_authed(pc.XAI, auth_type="oauth_xai")


def test_fetch_models_authed_api_key_auth_never_refreshes(monkeypatch):
    """A rejected API key is not refreshable — no OAuth refresh attempt, the
    error propagates untouched."""
    import urllib.error

    from modulatio import oauth_refresh

    def _fetch(provider, *, api_key=None, **kw):
        raise _http_401()

    def _no(auth_type, failed=None):
        raise AssertionError("refresh must not run for api_key auth")

    monkeypatch.setattr(pc, "fetch_models", _fetch)
    monkeypatch.setattr(pc, "listing_key",
                        lambda *, env_var=None, auth_type=None: "sk-bad")
    monkeypatch.setattr(oauth_refresh, "try_refresh", _no)
    with pytest.raises(urllib.error.HTTPError):
        pc.fetch_models_authed(pc.XAI, env_var="XAI_API_KEY", auth_type="api_key")


# ── custom-provider endpoint probe ────────────────────────────────────────────


def test_custom_provider_probes_operator_base_url(monkeypatch):
    """Once the operator supplies a base_url, the custom provider is probed
    OpenAI-style ({base}/models, Bearer when a key is present) so the picker
    fills live instead of demanding a typed id."""
    seen = {}

    def _fake_get(url, headers, timeout):
        seen["url"], seen["headers"] = url, headers
        return {"data": [{"id": "my-local-33b"}]}

    monkeypatch.setattr(pc, "_http_get_json", _fake_get)
    models = pc.fetch_models(pc.CUSTOM, api_key="sk-c", base_url="https://host/v1/")
    assert [m.id for m in models] == ["my-local-33b"]
    assert seen["url"] == "https://host/v1/models"
    assert seen["headers"] == {"Authorization": "Bearer sk-c"}


def test_custom_probe_failure_is_silent_empty(monkeypatch):
    """An unreachable/incompatible custom endpoint lists EMPTY, never raises —
    the typed model id is custom's sanctioned path, so a failed probe must not
    block the flow (unlike catalog providers, where failure is loud)."""
    def _boom(url, headers, timeout):
        raise OSError("unreachable")

    monkeypatch.setattr(pc, "_http_get_json", _boom)
    assert pc.fetch_models(pc.CUSTOM, base_url="https://host/v1") == []


def test_custom_without_base_url_lists_empty(monkeypatch):
    """No endpoint yet → nothing to probe, no network attempt."""
    def _no(url, headers, timeout):
        raise AssertionError("must not attempt a fetch without a base_url")

    monkeypatch.setattr(pc, "_http_get_json", _no)
    assert pc.fetch_models(pc.CUSTOM) == []


# ── central custom-only base_url gate + concurrent-heal single-flight ─────────


def test_fetch_models_ignores_base_url_for_catalog_providers(monkeypatch):
    """Defense-in-depth: base_url is a custom-provider affordance. fetch_models
    itself drops it for any other kind, so a catalog provider can never be
    re-pointed at another endpoint by ANY caller path (not just the web route)."""
    seen = {}

    def _spy(url, headers, timeout):
        seen["url"] = url
        return {"data": []}

    monkeypatch.setattr(pc, "_http_get_json", _spy)
    pc.fetch_models(pc.OPENROUTER, base_url="https://evil/v1")
    assert seen["url"].startswith(pc.OPENROUTER.base_url)   # its OWN endpoint, not evil


def test_concurrent_stale_listings_heal_exactly_once(monkeypatch):
    """Two listings racing on the same stale OAuth token must rotate the grant
    ONCE — a per-caller refresh would burn xAI's rotating grant. The listing
    single-flight collapses the burst; only one try_refresh runs."""
    import threading

    from modulatio import oauth_refresh

    monkeypatch.setattr(pc, "_listing_heal", {})   # clean cache for the test
    monkeypatch.setattr(pc, "listing_key",
                        lambda **kw: "stale-access")
    at_barrier = threading.Barrier(2)

    def _fetch(provider, *, api_key=None, **kw):
        if api_key == "stale-access":
            at_barrier.wait(timeout=5)             # both arrive stale together
            raise pc.urllib.error.HTTPError(
                "https://api.x.ai/v1/models", 403, "expired", None, None)
        return [pc.CatalogModel(id="grok", name="grok", provider_id="xai")]

    refreshes: list[str] = []
    lock = threading.Lock()

    def _rotate(auth_type, failed=None):
        with lock:
            tok = f"fresh-{len(refreshes) + 1}"
            refreshes.append(tok)
            return tok

    monkeypatch.setattr(pc, "fetch_models", _fetch)
    monkeypatch.setattr(oauth_refresh, "try_refresh", _rotate)

    results: list = []

    def _run():
        results.append(pc.fetch_models_authed(pc.XAI, auth_type="oauth_xai"))

    threads = [threading.Thread(target=_run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert refreshes == ["fresh-1"]                # exactly one rotation
    assert all(r and r[0].id == "grok" for r in results)   # both listings succeeded


def test_cross_process_late_follower_reuses_rotation(tmp_path, monkeypatch):
    """Two separate processes (a TUI and a WebOS) both list with the same stale
    OAuth token. The late follower must reuse the leader's rotation via the
    token-file lock + store re-read, NOT rotate the grant a second time. Unlike
    a stubbed-try_refresh shape (which bypasses that lock), this drives the REAL
    refresh_xai_token; only the network exchange (_do_refresh_xai) is stubbed."""
    import multiprocessing
    import sys

    if sys.platform == "win32":
        pytest.skip("fork-based pin is POSIX-only")
    from modulatio import oauth_helpers, oauth_refresh

    store = tmp_path / "xai.json"
    monkeypatch.setattr(oauth_helpers, "MODULATIO_XAI_OAUTH_FILE", store)
    oauth_helpers.write_xai_credentials({"access_token": "stale", "refresh_token": "r0"})
    monkeypatch.setattr(pc, "_listing_heal", {})
    monkeypatch.setattr(pc, "listing_key",
                        lambda **kw: (oauth_helpers.read_own_xai_credentials() or {}).get(
                            "access_token"))

    ctx = multiprocessing.get_context("fork")
    both_stale = ctx.Barrier(2)
    leader_done = ctx.Event()
    exchanges = ctx.Value("i", 0)
    xlock = ctx.Lock()

    def _do_refresh(refresh_token, *, timeout):
        with xlock:
            exchanges.value += 1
            n = exchanges.value
        oauth_helpers.write_xai_credentials(
            {"access_token": f"fresh-{n}", "refresh_token": f"r{n}"})
        leader_done.set()
        return f"fresh-{n}"

    monkeypatch.setattr(oauth_refresh, "_do_refresh_xai", _do_refresh)

    def _fetch(provider, *, api_key=None, **kw):
        if api_key == "stale":
            both_stale.wait(timeout=5)
            if multiprocessing.current_process().name == "late":
                leader_done.wait(timeout=5)      # follower handles its 403 after the rotation
            raise pc.urllib.error.HTTPError(
                "https://api.x.ai/v1/models", 403, "", None, None)
        return []

    monkeypatch.setattr(pc, "fetch_models", _fetch)

    def _run():
        pc.fetch_models_authed(pc.XAI, auth_type="oauth_xai")

    procs = [ctx.Process(target=_run, name="leader"),
             ctx.Process(target=_run, name="late")]
    for proc in procs:
        proc.start()
    for proc in procs:
        proc.join(timeout=15)
    for proc in procs:
        assert proc.exitcode == 0
    assert exchanges.value == 1                   # the grant rotated exactly once


def test_heal_cache_never_reuses_the_token_just_rejected(monkeypatch):
    """The heal cache must not hand back the exact token that just failed. A
    cached replacement is keyed to the token it replaced; when THAT replacement
    is later itself rejected, the cache is bypassed and try_refresh runs (whose
    store re-read decides), rather than retrying the dead token as-is."""
    rejected = "fresh-but-rejected"
    # Cache lineage: an earlier "stale-0" was healed to `rejected`.
    monkeypatch.setattr(pc, "_listing_heal",
                        {"oauth_xai": ("stale-0", rejected, pc.time.monotonic())})
    monkeypatch.setattr(pc, "listing_key", lambda **kw: rejected)

    calls = []

    def _fetch(provider, *, api_key=None, **kw):
        calls.append(api_key)
        if api_key == rejected:
            raise pc.urllib.error.HTTPError("https://x", 403, "", None, None)
        return []

    monkeypatch.setattr(pc, "fetch_models", _fetch)
    from modulatio import oauth_refresh

    refreshes = []
    monkeypatch.setattr(oauth_refresh, "try_refresh",
                        lambda auth_type, failed=None: refreshes.append(failed) or "new-token")

    pc.fetch_models_authed(pc.XAI, auth_type="oauth_xai")
    assert refreshes == [rejected]              # bypassed the cache, refreshed
    assert calls == [rejected, "new-token"]     # retried with the NEW token, not the dead one


def test_heal_cache_reuses_replacement_for_the_same_failed_token(monkeypatch):
    """The intended reuse: a follower rejected with the SAME token the cached
    replacement was minted for reuses that replacement without a second refresh."""
    monkeypatch.setattr(pc, "_listing_heal",
                        {"oauth_xai": ("stale", "the-replacement", pc.time.monotonic())})
    monkeypatch.setattr(pc, "listing_key", lambda **kw: "stale")

    def _fetch(provider, *, api_key=None, **kw):
        if api_key == "stale":
            raise pc.urllib.error.HTTPError("https://x", 403, "", None, None)
        return []

    monkeypatch.setattr(pc, "fetch_models", _fetch)
    from modulatio import oauth_refresh

    refreshes = []
    monkeypatch.setattr(oauth_refresh, "try_refresh",
                        lambda auth_type, failed=None: refreshes.append(failed) or "unexpected")

    pc.fetch_models_authed(pc.XAI, auth_type="oauth_xai")
    assert refreshes == []                       # reused the cached replacement, no refresh
