"""Codex runner wiring (#8 sibling): the chat-runner Codex branch builds the
Responses-API call shape and parses it back; add_preset persists ``endpoint``;
+ a skippable LIVE round-trip against the real Codex backend.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from modulatio import model_presets, oauth_helpers, runners

_CODEX_PRESET = {
    "label": "gpt-5.5 (Codex)", "base_url": "https://chatgpt.com/backend-api/codex",
    "api_format": "openai", "auth_type": "oauth_openai", "auth_config": {},
    "endpoint": "codex", "model": "gpt-5.5",
}


def test_add_preset_persists_endpoint(monkeypatch):
    store: dict = {}
    monkeypatch.setattr(model_presets, "load_presets", lambda: dict(store))
    monkeypatch.setattr(model_presets, "save_presets", lambda p: store.update(p))
    entry = model_presets.add_preset(
        "c", label="c", base_url="https://chatgpt.com/backend-api/codex",
        api_format="openai", auth_type="oauth_openai", model="gpt-5.5", endpoint="codex",
    )
    assert entry["endpoint"] == "codex"
    # default "chat" endpoint is omitted (kept implicit)
    store.clear()
    e2 = model_presets.add_preset(
        "d", label="d", base_url="https://x", api_format="openai",
        auth_type="none", model="m", endpoint="chat",
    )
    assert "endpoint" not in e2


def test_codex_chat_runner_builds_responses_shape_and_parses(monkeypatch):
    import litellm
    monkeypatch.setattr(model_presets, "load_presets", lambda: {"c": dict(_CODEX_PRESET)})
    monkeypatch.setattr(oauth_helpers, "read_openai_token", lambda: "tok-abc")
    monkeypatch.setattr(oauth_helpers, "read_openai_account_id", lambda: "acc-1")

    item = SimpleNamespace(type="function_call", id="fc1", name="get_weather",
                           call_id="call_1", arguments="")
    fake_stream = [
        SimpleNamespace(type="response.output_item.added", item=item),
        SimpleNamespace(type="response.function_call_arguments.delta",
                        item_id="fc1", delta='{"city":"Paris"}'),
        SimpleNamespace(type="response.output_item.done", item=item),
    ]
    captured: dict = {}

    def fake_responses(**kw):
        captured.update(kw)
        return fake_stream

    monkeypatch.setattr(litellm, "responses", fake_responses)

    runner = runners.litellm_chat_runner("c")
    tools = [{"type": "function", "function": {
        "name": "get_weather", "description": "d", "parameters": {"type": "object"}}}]
    resp = runner(messages=[{"role": "system", "content": "sys"},
                            {"role": "user", "content": "hi"}], tools=tools)

    # the Codex Responses shape
    assert captured["store"] is False
    assert captured["stream"] is True
    assert captured["instructions"] == "sys"
    assert captured["input"] == [
        {"type": "message", "role": "user",
         "content": [{"type": "input_text", "text": "hi"}]}]
    assert captured["tools"] == [
        {"type": "function", "name": "get_weather", "description": "d",
         "parameters": {"type": "object"}}]
    assert captured["extra_headers"] == {
        "chatgpt-account-id": "acc-1", "originator": "codex-tui"}
    assert captured["api_key"] == "tok-abc"
    # the parsed result
    assert resp.content == ""
    assert resp.tool_calls[0].name == "get_weather"
    assert resp.tool_calls[0].args == {"city": "Paris"}


@pytest.mark.skipif(
    not os.path.exists(os.path.expanduser("~/.codex/auth.json")),
    reason="no Codex OAuth creds (~/.codex/auth.json) — live test skipped",
)
def test_live_codex_tool_call_roundtrip():
    """LIVE: real call to the Codex backend confirms GPT-5.5 emits a tool_call
    through Modulatio's runner. Skipped without Codex creds (CI, fresh installs)."""
    with patch.object(model_presets, "load_presets", return_value={"c": dict(_CODEX_PRESET)}):
        runner = runners.litellm_chat_runner("c")
        tools = [{"type": "function", "function": {
            "name": "get_weather", "description": "Get weather for a city.",
            "parameters": {"type": "object",
                           "properties": {"city": {"type": "string"}},
                           "required": ["city"]}}}]
        resp = runner(messages=[
            {"role": "system", "content": "Call a tool when asked."},
            {"role": "user", "content": "What's the weather in Paris? Call get_weather."},
        ], tools=tools)
    assert any(tc.name == "get_weather" for tc in resp.tool_calls), resp


def test_codex_single_shot_runner_returns_text(monkeypatch):
    """The single-shot litellm_runner codex branch (leader-decompose / non-tool
    seats): builds a Responses call from the prompt, aggregates the text."""
    import litellm
    monkeypatch.setattr(model_presets, "load_presets", lambda: {"c": dict(_CODEX_PRESET)})
    monkeypatch.setattr(oauth_helpers, "read_openai_token", lambda: "tok")
    monkeypatch.setattr(oauth_helpers, "read_openai_account_id", lambda: "acc")
    captured: dict = {}

    def fake_responses(**kw):
        captured.update(kw)
        return [SimpleNamespace(type="response.output_text.delta", delta="Hello")]

    monkeypatch.setattr(litellm, "responses", fake_responses)
    out = runners.litellm_runner("c")("say hello")
    assert out == "Hello"
    assert captured["store"] is False
    assert captured["stream"] is True
    assert captured["input"][0]["content"][0]["text"].endswith("say hello")


# ── host-pinning (Wild Bill BLOCK): the OAuth sub creds never leave OpenAI hosts ──

def test_oauth_openai_creds_refused_for_untrusted_host(monkeypatch):
    """A hand-edited oauth_openai preset with a malicious base_url must NOT send
    the Codex subscription token or the chatgpt-account-id header to that host."""
    evil = dict(_CODEX_PRESET, base_url="https://evil.example/v1")
    monkeypatch.setattr(model_presets, "load_presets", lambda: {"e": evil})
    monkeypatch.setattr(oauth_helpers, "read_openai_token", lambda: "SECRET-TOKEN")
    monkeypatch.setattr(oauth_helpers, "read_openai_account_id", lambda: "SECRET-ACC")

    _model, kwargs = runners._resolve_model_call_args("e")

    assert kwargs.get("api_key") != "SECRET-TOKEN"           # token withheld
    headers = kwargs.get("extra_headers", {})
    assert "chatgpt-account-id" not in headers               # account-id withheld
    assert "SECRET-ACC" not in str(kwargs)                   # not anywhere in the call args


def test_oauth_openai_creds_refused_over_cleartext_http(monkeypatch):
    """A bearer token must never ride cleartext HTTP: even the right hostname on
    http:// (a downgrade) must withhold the token + chatgpt-account-id header."""
    downgrade = dict(_CODEX_PRESET, base_url="http://chatgpt.com/backend-api/codex")
    monkeypatch.setattr(model_presets, "load_presets", lambda: {"d": downgrade})
    monkeypatch.setattr(oauth_helpers, "read_openai_token", lambda: "SECRET-TOKEN")
    monkeypatch.setattr(oauth_helpers, "read_openai_account_id", lambda: "SECRET-ACC")

    _model, kwargs = runners._resolve_model_call_args("d")

    assert kwargs.get("api_key") != "SECRET-TOKEN"
    assert "chatgpt-account-id" not in kwargs.get("extra_headers", {})
    assert "SECRET-ACC" not in str(kwargs)


def test_oauth_openai_creds_sent_to_codex_host(monkeypatch):
    """The legit Codex backend DOES receive the token + account-id header."""
    monkeypatch.setattr(model_presets, "load_presets", lambda: {"g": dict(_CODEX_PRESET)})
    monkeypatch.setattr(oauth_helpers, "read_openai_token", lambda: "SECRET-TOKEN")
    monkeypatch.setattr(oauth_helpers, "read_openai_account_id", lambda: "acc-1")

    _model, kwargs = runners._resolve_model_call_args("g")

    assert kwargs["api_key"] == "SECRET-TOKEN"
    assert kwargs["extra_headers"]["chatgpt-account-id"] == "acc-1"


def test_codex_headers_not_sent_off_codex_backend(monkeypatch):
    """Hardening: the chatgpt-account-id / originator headers are Codex-host-only.
    A trusted OpenAI HTTPS host that is NOT the Codex endpoint gets the token but
    NOT the codex-specific attribution headers (no needless account-id leak)."""
    plain = dict(_CODEX_PRESET, base_url="https://api.openai.com/v1", endpoint="chat")
    monkeypatch.setattr(model_presets, "load_presets", lambda: {"p": plain})
    monkeypatch.setattr(oauth_helpers, "read_openai_token", lambda: "SECRET-TOKEN")
    monkeypatch.setattr(oauth_helpers, "read_openai_account_id", lambda: "acc-1")

    _model, kwargs = runners._resolve_model_call_args("p")

    assert kwargs["api_key"] == "SECRET-TOKEN"          # token still allowed (OpenAI host)
    assert "chatgpt-account-id" not in kwargs.get("extra_headers", {})
    assert "acc-1" not in str(kwargs.get("extra_headers", {}))
