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

from modulatio import model_presets
from modulatio.setup_wizard import provider_step
from modulatio.setup_wizard.provider_step import (
    CLAUDE_CLI_MODELS,
    OPENAI_CODEX_MODELS,
    default_env_var_for,
)


# ─── Curated model lists ────────────────────────────────────────────────────


def test_clay_models_includes_current_haiku_sonnet_opus():
    """Clay's curated Claude list covers the live family the user is most
    likely to pick. Matched by family prefix so a version bump doesn't break
    the test (refresh the seed when new models ship)."""
    assert any(m.startswith("claude-haiku") for m in CLAUDE_CLI_MODELS)
    assert any(m.startswith("claude-sonnet") for m in CLAUDE_CLI_MODELS)
    assert any(m.startswith("claude-opus") for m in CLAUDE_CLI_MODELS)


def test_clay_models_is_sorted_for_stable_picker_order():
    """Sorted so picker rendering is deterministic across runs."""
    assert list(CLAUDE_CLI_MODELS) == sorted(CLAUDE_CLI_MODELS)


def test_codex_models_includes_current_gpt_line():
    """The Codex subscription curated list covers the current GPT line.
    Matched by prefix so a minor-version bump doesn't break the test (refresh
    the seed when new models ship)."""
    assert any(m.startswith("gpt-5") for m in OPENAI_CODEX_MODELS)


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


# ─── Subscription quick-adds register the RIGHT endpoint (the 0.9.5.1 bug) ───


# ─── Subscription quick-adds register the RIGHT endpoint (the 0.9.5.1 bug) ───


def test_clay_quick_add_registers_claude_cli_not_metered_anthropic(tmp_path, monkeypatch):
    """The Anthropic quick-add registers Clay (``claude_cli`` → the `claude -p`
    subscription), NEVER ``oauth_anthropic`` at api.anthropic.com — that combo
    401s a subscription token (the fresh-install bug)."""
    monkeypatch.setattr(model_presets, "PRESETS_FILE", tmp_path / "model_presets.json")
    monkeypatch.setattr(
        provider_step.oauth_helpers, "find_claude_binary", lambda: "/x/claude"
    )
    monkeypatch.setattr(
        provider_step.steps, "pick_option", lambda *a, **k: "claude-opus-4-8"
    )

    key = provider_step._quick_add_clay()

    preset = model_presets.get_preset(key)
    assert preset["auth_type"] == "claude_cli"
    assert preset["endpoint"] == "claude_cli"
    assert preset["base_url"] == "claude-cli"
    assert "api.anthropic.com" not in preset["base_url"]


def test_codex_quick_add_targets_subscription_backend_not_metered(tmp_path, monkeypatch):
    """The OpenAI quick-add registers the Codex subscription (``codex`` endpoint
    → the ChatGPT backend), NEVER the metered api.openai.com (which 401s a
    subscription token)."""
    monkeypatch.setattr(model_presets, "PRESETS_FILE", tmp_path / "model_presets.json")
    monkeypatch.setattr(provider_step, "_print_oauth_warning", lambda: None)
    monkeypatch.setattr(
        provider_step.steps, "pick_option", lambda *a, **k: "gpt-5.5"
    )

    key = provider_step._quick_add_openai_oauth()

    preset = model_presets.get_preset(key)
    assert preset["auth_type"] == "oauth_openai"
    assert preset["endpoint"] == "codex"
    assert preset["base_url"] == "https://chatgpt.com/backend-api/codex"
    assert "api.openai.com" not in preset["base_url"]


def test_default_env_var_for_invalid_url_returns_neutral_fallback():
    """Malformed URL doesn't crash — returns a neutral placeholder so
    the wizard can still proceed (user types real value)."""
    out = default_env_var_for("not-a-url")
    # Non-empty, ALL_CAPS, ends in API_KEY.
    assert out.endswith("API_KEY")
    assert out.upper() == out
