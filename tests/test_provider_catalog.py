"""Tests for the provider catalog — the Configuration tab's data layer.

The catalog describes a provider richly enough that the configurator fills in
model id, base_url, api_format, and auth for the operator — who only ever
supplies the key. These cover OpenRouter (the first provider built out): free
detection, pinned models (auto + free), the curated-default view, search, and
the catalog→preset wiring onto the existing model_presets backend.
"""
from __future__ import annotations

from modulatio import model_presets, provider_catalog as pc

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
    # free is never presented as unlimited — both providers carry a caveat
    assert "limit" in pc.OPENROUTER.free_note.lower()
    note = pc.OLLAMA_CLOUD.free_note.lower()
    assert "limit" in note and "5-hour" in note and "7-day" in note


def test_ollama_cloud_models_are_all_free_tier():
    models = pc.parse_models(pc.OLLAMA_CLOUD, OLLAMA_PAYLOAD)
    assert {m.id for m in models} == {"deepseek-v4-pro", "gemma4:31b", "qwen3-next:80b"}
    assert all(m.is_free for m in models)  # free tier (rate-limited)
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


def test_existing_providers_default_to_text_modality():
    models = pc.parse_models(pc.OPENROUTER, PAYLOAD)
    assert all(m.modality == "text" for m in models)


# ── Anthropic (dual auth, picklist source, no free tier) ────────────────────


def test_anthropic_dual_auth_anthropic_format_no_free():
    p = pc.get_provider("anthropic")
    assert p is not None
    assert p.api_format == "anthropic"
    assert p.free_detect == "none"
    # both ways in: OAuth and an API key
    assert [a.auth_type for a in p.auth_options] == ["oauth_anthropic", "api_key"]
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
    assert [a.auth_type for a in p.auth_options] == ["oauth_openai", "api_key"]
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


def test_nvidia_public_listing_free_tier_rate_limited():
    p = pc.get_provider("nvidia")
    assert p is not None
    assert p.base_url == "https://integrate.api.nvidia.com/v1"
    assert p.auth_options[0].env_var == "NVIDIA_API_KEY"
    assert p.models_source.auth_required is False  # listing is public
    assert p.free_detect == "all"
    assert "40" in p.free_note  # the 40-RPM caveat is surfaced


def test_nvidia_models_flagged_free(monkeypatch):
    monkeypatch.setattr(pc, "_http_get_json", lambda url, h, t: NVIDIA_PAYLOAD)
    models = pc.fetch_models(pc.NVIDIA)
    assert len(models) == 3
    assert all(m.is_free for m in models)
    assert all(m.modality == "text" for m in models)


def test_google_gemini_openai_compat_key_gated_free_tier():
    p = pc.get_provider("google")
    assert p is not None
    assert p.base_url == "https://generativelanguage.googleapis.com/v1beta/openai"
    assert p.api_format == "openai"  # Gemini's OpenAI-compat endpoint
    assert p.auth_options[0].env_var == "GEMINI_API_KEY"
    assert p.models_source.auth_required is True  # key needed to list
    assert p.free_detect == "all"
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
    assert all(m.is_free for m in models)
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
