"""Tests for litellm_runner's preset-resolution + OAuth + alert path.

Mocks litellm.completion so we never hit a real LLM. Verifies the runner:
- Resolves preset keys to (litellm_model, kwargs) reading the flat schema
- Passes raw LiteLLM ids through unchanged
- Fires an auth alert on AuthenticationError; clears it on success
"""

from __future__ import annotations

import json
import sys
from unittest.mock import MagicMock

import pytest

from modulatio import auth_alerts, config, model_presets, oauth_helpers, runners


@pytest.fixture(autouse=True)
def isolate_config(tmp_path, monkeypatch):
    cfg = tmp_path / "config"
    monkeypatch.setattr(config, "CONFIG_DIR", cfg)
    monkeypatch.setattr(config, "AUTH_ALERTS_FILE", cfg / "auth_alerts.json")
    monkeypatch.setattr(model_presets, "PRESETS_FILE", cfg / "model_presets.json")
    monkeypatch.setattr(oauth_helpers, "MODULATIO_OPENAI_OAUTH_FILE", tmp_path / "openai.json")


@pytest.fixture(autouse=True)
def disable_external_alert_channels(monkeypatch):
    monkeypatch.setattr(auth_alerts, "_try_desktop_notification", lambda *a, **k: None)
    monkeypatch.setattr(auth_alerts, "_try_telegram_notification", lambda *a, **k: None)


# === _resolve_model_call_args ===

def test_resolve_unknown_key_passes_through_as_raw_id():
    model, kwargs = runners._resolve_model_call_args("anthropic/claude-opus-4-7")
    assert model == "anthropic/claude-opus-4-7"
    assert kwargs == {}


def test_resolve_preset_with_api_key_auth(monkeypatch):
    monkeypatch.setenv("OLLAMA_API_KEY", "sk-ollama-test")
    model_presets.add_preset(
        "glm", label="GLM", base_url="https://ollama.com/v1",
        api_format="openai", auth_type="api_key",
        auth_config={"env_var": "OLLAMA_API_KEY"}, model="glm-5.1",
    )
    model, kwargs = runners._resolve_model_call_args("glm")
    assert model == "openai/glm-5.1"
    assert kwargs["api_base"] == "https://ollama.com/v1"
    assert kwargs["api_key"] == "sk-ollama-test"


def test_resolve_preset_with_local_endpoint_gets_placeholder_api_key():
    # A keyless local OpenAI-compatible endpoint (LM Studio / llama.cpp /
    # Ollama-local) must resolve a placeholder api_key (0.9.4.1 fix): LiteLLM's
    # openai handler raises "Missing credentials" without one even though the
    # local server ignores it.
    model_presets.add_preset(
        "llama", label="Llama", base_url="http://127.0.0.1:11434/v1",
        api_format="openai", auth_type="none", model="llama3",
    )
    model, kwargs = runners._resolve_model_call_args("llama")
    assert model == "openai/llama3"
    assert kwargs["api_base"] == "http://127.0.0.1:11434/v1"
    assert kwargs["api_key"] == "modulatio-local"


def test_resolve_preset_missing_model_field_raises():
    """Defensive: malformed preset with no model field → ValueError."""
    config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    model_presets.PRESETS_FILE.write_text(json.dumps({
        "broken": {"label": "X", "base_url": "u", "api_format": "openai", "auth_type": "none"}
    }))
    with pytest.raises(ValueError, match="missing 'model'"):
        runners._resolve_model_call_args("broken")


# === litellm_runner — happy path ===

def _stub_litellm_completion(monkeypatch, responses):
    fake_completion = MagicMock(side_effect=responses)
    fake_litellm = MagicMock()
    fake_litellm.completion = fake_completion
    fake_exceptions = MagicMock()
    fake_exceptions.AuthenticationError = type("AuthenticationError", (Exception,), {})
    monkeypatch.setitem(sys.modules, "litellm", fake_litellm)
    monkeypatch.setitem(sys.modules, "litellm.exceptions", fake_exceptions)
    return fake_completion, fake_exceptions.AuthenticationError


def _ok_response(content: str = "hello"):
    msg = MagicMock()
    msg.content = content
    choice = MagicMock()
    choice.message = msg
    resp = MagicMock()
    resp.choices = [choice]
    return resp


def test_litellm_runner_dispatches_with_resolved_model(monkeypatch):
    model_presets.add_preset(
        "k", label="K", base_url="https://x", api_format="openai",
        auth_type="none", model="m1",
    )
    fake_completion, _ = _stub_litellm_completion(monkeypatch, [_ok_response("yo")])
    runner = runners.litellm_runner("k")
    out = runner("hello?")
    assert out == "yo"
    call_kwargs = fake_completion.call_args.kwargs
    assert call_kwargs["model"] == "openai/m1"
    assert call_kwargs["api_base"] == "https://x"


def test_litellm_runner_clears_alert_on_success(monkeypatch):
    model_presets.add_preset(
        "k", label="K", base_url="https://x", api_format="openai",
        auth_type="none", model="m1",
    )
    auth_alerts.raise_alert("k", error_message="prior failure", auth_type="none")
    assert auth_alerts.has_active_alerts() is True
    _stub_litellm_completion(monkeypatch, [_ok_response()])
    runner = runners.litellm_runner("k")
    runner("ping")
    assert auth_alerts.has_active_alerts() is False


def test_litellm_runner_passes_raw_id_through(monkeypatch):
    fake_completion, _ = _stub_litellm_completion(monkeypatch, [_ok_response("ok")])
    runner = runners.litellm_runner("anthropic/claude-opus-4-7")
    runner("hi")
    assert fake_completion.call_args.kwargs["model"] == "anthropic/claude-opus-4-7"
