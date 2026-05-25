"""Tests for #23 — UX pickers / smart defaults in the wizard.

Closes the typo-trap class of bugs surfaced 2026-04-26 when the
wizard's free-text 'Model id at api.anthropic.com' produced wrong
entries (xai_grok_4_2 with the wrong model id, anthropic_ql from a
typo).

Two interventions:
  - Curated model lists for Anthropic / OpenAI OAuth flows so the
    wizard can offer a picker instead of free-text.
  - Smart env-var pre-fill from the base_url hostname so the user
    doesn't have to remember conventions like XAI_API_KEY.
"""
from __future__ import annotations

from modulatio.setup_wizard.provider_step import (
    ANTHROPIC_OAUTH_MODELS,
    OPENAI_OAUTH_MODELS,
    default_env_var_for,
)


# ─── Curated model lists ────────────────────────────────────────────────────


def test_anthropic_oauth_models_includes_current_haiku_sonnet_opus():
    """Anthropic curated list covers the live Claude 4 family the user
    is most likely to pick. Refresh when new models ship."""
    assert "claude-haiku-4-5" in ANTHROPIC_OAUTH_MODELS
    assert "claude-sonnet-4-6" in ANTHROPIC_OAUTH_MODELS
    assert "claude-opus-4-7" in ANTHROPIC_OAUTH_MODELS


def test_anthropic_oauth_models_is_sorted_for_stable_picker_order():
    """Sorted so picker rendering is deterministic across runs."""
    assert list(ANTHROPIC_OAUTH_MODELS) == sorted(ANTHROPIC_OAUTH_MODELS)


def test_openai_oauth_models_includes_common_gpt_and_o_models():
    """OpenAI curated list covers the common GPT and reasoning lines."""
    # Match by prefix so a future minor-version bump doesn't break the test.
    assert any(m.startswith("gpt-4o") for m in OPENAI_OAUTH_MODELS)
    assert any(m.startswith("o1") for m in OPENAI_OAUTH_MODELS)


# ─── Env-var smart default ──────────────────────────────────────────────────


def test_default_env_var_for_xai():
    assert default_env_var_for("https://api.x.ai/v1") == "XAI_API_KEY"


def test_default_env_var_for_openai():
    assert default_env_var_for("https://api.openai.com/v1") == "OPENAI_API_KEY"


def test_default_env_var_for_anthropic():
    assert default_env_var_for("https://api.anthropic.com") == "ANTHROPIC_API_KEY"


def test_default_env_var_for_openrouter():
    assert default_env_var_for("https://openrouter.ai/api/v1") == "OPENROUTER_API_KEY"


def test_default_env_var_for_ollama_cloud():
    assert default_env_var_for("https://ollama.com/v1") == "OLLAMA_API_KEY"


def test_default_env_var_for_unknown_host_derives_from_hostname():
    """Unknown vendor: pull the first hostname segment, uppercase,
    append _API_KEY. e.g. api.deepseek.com → DEEPSEEK_API_KEY."""
    assert default_env_var_for("https://api.deepseek.com/v1") == "DEEPSEEK_API_KEY"
    assert default_env_var_for("https://groq.com/openai/v1") == "GROQ_API_KEY"


def test_default_env_var_for_invalid_url_returns_neutral_fallback():
    """Malformed URL doesn't crash — returns a neutral placeholder so
    the wizard can still proceed (user types real value)."""
    out = default_env_var_for("not-a-url")
    # Non-empty, ALL_CAPS, ends in API_KEY.
    assert out.endswith("API_KEY")
    assert out.upper() == out
