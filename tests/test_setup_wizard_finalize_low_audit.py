# SPDX-License-Identifier: Apache-2.0
"""LOW-audit regression tests for setup_wizard.finalize.

Finding #89: ``finalize.confirm`` printed a Providers line from a state key
(``configured_providers``) the wizard never sets, so it always rendered
``(none)``. Providers are now derived from ``staged_api_keys`` (env-var keyed).
"""

from __future__ import annotations

from modulatio.setup_wizard import finalize


def test_derive_providers_from_staged_keys():
    state = {
        "staged_api_keys": {
            "OPENAI_API_KEY": "sk-x",
            "ANTHROPIC_API_KEY": "sk-y",
            "XAI_API_KEY": "sk-z",
        }
    }
    assert finalize._derive_providers(state) == ["anthropic", "openai", "xai"]


def test_derive_providers_dedup_and_sorted():
    # Two differently-cased entries for the same provider collapse to one.
    state = {"staged_api_keys": {"openai_api_key": "a", "OPENAI_API_KEY": "b"}}
    assert finalize._derive_providers(state) == ["openai"]


def test_derive_providers_empty_when_nothing_staged():
    assert finalize._derive_providers({}) == []
    assert finalize._derive_providers({"staged_api_keys": {}}) == []


def test_derive_providers_non_standard_env_var():
    # A custom key without the _API_KEY suffix still yields a lowercased name
    # rather than being dropped.
    state = {"staged_api_keys": {"GROQ_TOKEN": "v"}}
    assert finalize._derive_providers(state) == ["groq_token"]


def test_confirm_renders_providers_not_none(monkeypatch, capsys):
    # The bug: with staged keys present the summary still showed "(none)".
    # Drive confirm() to the print path, auto-confirming the prompt.
    monkeypatch.setattr("builtins.input", lambda *a, **k: "y")
    state = {
        "vault_root": "/tmp/v",
        "shared_resources_path": "/tmp/s",
        "staged_api_keys": {"OPENAI_API_KEY": "sk-x", "ANTHROPIC_API_KEY": "sk-y"},
        "configured_models": ["m1"],
        "triad_agents": [],
        "worker_agents": [],
    }
    result = finalize.confirm(state)
    assert result is True
    out = capsys.readouterr().out
    assert "Providers:" in out
    # Both staged providers appear; the line is no longer "(none)".
    assert "openai" in out
    assert "anthropic" in out
    # Locate the Providers line specifically and confirm it isn't empty.
    providers_line = next(ln for ln in out.splitlines() if "Providers:" in ln)
    assert "(none)" not in providers_line


# --- r2 audit: providers derived from model presets (OAuth/local-only) ---


def test_provider_from_base_url_variants():
    f = finalize._provider_from_base_url
    assert f("https://api.openai.com/v1") == "openai"
    assert f("https://api.anthropic.com") == "anthropic"
    assert f("https://api.x.ai/v1") == "x"
    assert f("https://openrouter.ai/api/v1") == "openrouter"
    assert f("http://127.0.0.1:11434/v1") == "local"
    assert f("http://localhost:1234/v1") == "local"
    assert f("not a url") is None
    assert f("") is None


def test_derive_providers_includes_oauth_only_preset(monkeypatch):
    # OAuth-only setup stages NO api keys; the old code rendered (none).
    # Now the endpoint behind each configured model represents the provider.
    monkeypatch.setattr(
        "modulatio.model_presets.load_presets",
        lambda: {
            "claude-oauth": {
                "base_url": "https://api.anthropic.com",
                "auth_type": "oauth_anthropic",
            },
        },
    )
    state = {"staged_api_keys": {}, "configured_models": ["claude-oauth"]}
    assert finalize._derive_providers(state) == ["anthropic"]


def test_derive_providers_includes_local_only_preset(monkeypatch):
    # Local-only setup: auth_type none, no api key, loopback endpoint.
    monkeypatch.setattr(
        "modulatio.model_presets.load_presets",
        lambda: {
            "ollama-llama": {
                "base_url": "http://127.0.0.1:11434/v1",
                "auth_type": "none",
            },
        },
    )
    state = {"staged_api_keys": {}, "configured_models": ["ollama-llama"]}
    assert finalize._derive_providers(state) == ["local"]


def test_derive_providers_unions_keys_and_presets(monkeypatch):
    monkeypatch.setattr(
        "modulatio.model_presets.load_presets",
        lambda: {
            "ollama-llama": {"base_url": "http://127.0.0.1:11434/v1"},
            "claude-oauth": {"base_url": "https://api.anthropic.com"},
        },
    )
    state = {
        "staged_api_keys": {"OPENAI_API_KEY": "sk-x"},
        "configured_models": ["ollama-llama", "claude-oauth"],
    }
    # openai (from key) + anthropic + local (from presets), sorted/deduped.
    assert finalize._derive_providers(state) == ["anthropic", "local", "openai"]


def test_derive_providers_ignores_unconfigured_presets(monkeypatch):
    # A preset on disk that the user did NOT pick (not in configured_models)
    # is not represented in the summary.
    monkeypatch.setattr(
        "modulatio.model_presets.load_presets",
        lambda: {
            "picked": {"base_url": "https://api.anthropic.com"},
            "unpicked": {"base_url": "https://api.openai.com"},
        },
    )
    state = {"staged_api_keys": {}, "configured_models": ["picked"]}
    assert finalize._derive_providers(state) == ["anthropic"]


def test_derive_providers_survives_presets_load_failure(monkeypatch):
    def boom():
        raise RuntimeError("disk gone")

    monkeypatch.setattr("modulatio.model_presets.load_presets", boom)
    state = {"staged_api_keys": {"OPENAI_API_KEY": "sk-x"}}
    # Degrades to staged-keys-only rather than crashing the summary.
    assert finalize._derive_providers(state) == ["openai"]


# --- r2 audit: Producers line no longer renders '?, ?, ?' ---


def test_producer_label_prefers_model():
    assert finalize._producer_label({"model": "gpt-4o-mini", "skills": []}) == "gpt-4o-mini"


def test_producer_label_falls_back_to_caps_then_name():
    assert finalize._producer_label(
        {"model": "", "capability_tags": ["fast", "cheap"]}
    ) == "fast, cheap"
    assert finalize._producer_label({"model": "", "name": "Scout"}) == "Scout"
    assert finalize._producer_label({}) == "?"


def test_confirm_producers_line_not_all_question_marks(monkeypatch, capsys):
    # The bug: 'Skill-holders' rendered '?, ?, ?' because Agent.skills is
    # always empty (skills are checked out from the library per task).
    monkeypatch.setattr("builtins.input", lambda *a, **k: "y")
    monkeypatch.setattr("modulatio.model_presets.load_presets", lambda: {})
    state = {
        "vault_root": "/tmp/v",
        "shared_resources_path": "/tmp/s",
        "staged_api_keys": {},
        "configured_models": [],
        "triad_agents": [],
        "worker_agents": [
            {"name": "p1", "model": "gpt-4o-mini", "skills": [], "tier": "producer"},
            {"name": "p2", "model": "claude-haiku", "skills": [], "tier": "producer"},
        ],
    }
    assert finalize.confirm(state) is True
    out = capsys.readouterr().out
    prod_line = next(ln for ln in out.splitlines() if "Producers:" in ln)
    assert "?" not in prod_line
    assert "gpt-4o-mini" in prod_line
    assert "claude-haiku" in prod_line
