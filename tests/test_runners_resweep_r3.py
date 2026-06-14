"""Round-3 re-sweep regressions for ``src/modulatio/runners.py``.

Two confirmed pre-ship findings:

Finding 1 (MEDIUM/integration): ``litellm_chat_runner.run`` — the PRIMARY
tool-using producer path — had no ``AuthenticationError`` handling, so an
expired OAuth access token (they die ~24h) killed every tool-using producer
overnight while the single-shot ``litellm_runner`` self-healed. The tool-loop
runner must now refresh-once + retry, fire an auth alert on persistent 401,
and clear a prior alert on success — same contract as ``litellm_runner``.

Finding 2 (LOW/product-agnostic): on the summarizer-FAILED truncation
fallback in ``run_llm_with_tools``, the kept head was budgeted with the
SUMMARIZER model's tokenizer (``count_model``) even though the truncated text
lands in the MAIN model's context. The head must be sized against the
tokenizer that will actually carry it (``model``).

Tests mock litellm so no real LLM is hit; isolated config dir per test.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest

from modulatio import (
    auth_alerts,
    config,
    model_presets,
    oauth_helpers,
    runners,
)
from modulatio import tool_summarization as _tool_sum


@pytest.fixture(autouse=True)
def isolate_config(tmp_path, monkeypatch):
    cfg = tmp_path / "config"
    monkeypatch.setattr(config, "CONFIG_DIR", cfg)
    monkeypatch.setattr(config, "AUTH_ALERTS_FILE", cfg / "auth_alerts.json")
    monkeypatch.setattr(model_presets, "PRESETS_FILE", cfg / "model_presets.json")
    monkeypatch.setattr(
        oauth_helpers, "ANTHROPIC_CREDENTIALS_FILE", tmp_path / "anthropic.json"
    )
    monkeypatch.setattr(
        oauth_helpers, "OPENAI_CODEX_CREDENTIALS_FILE", tmp_path / "openai.json"
    )


@pytest.fixture(autouse=True)
def disable_external_alert_channels(monkeypatch):
    monkeypatch.setattr(auth_alerts, "_try_desktop_notification", lambda *a, **k: None)
    monkeypatch.setattr(auth_alerts, "_try_telegram_notification", lambda *a, **k: None)


# ── litellm mock plumbing (mirrors test_runners_oauth) ─────────────────────


def _stub_litellm(monkeypatch, completion_side_effect):
    fake_completion = MagicMock(side_effect=completion_side_effect)
    fake_litellm = MagicMock()
    fake_litellm.completion = fake_completion
    fake_exceptions = MagicMock()
    fake_exceptions.AuthenticationError = type(
        "AuthenticationError", (Exception,), {}
    )
    fake_exceptions.RateLimitError = type("RateLimitError", (Exception,), {})
    monkeypatch.setitem(sys.modules, "litellm", fake_litellm)
    monkeypatch.setitem(sys.modules, "litellm.exceptions", fake_exceptions)
    return fake_completion, fake_exceptions.AuthenticationError


def _chat_response(content: str = "ok", tool_calls=None):
    """A minimal litellm chat-completion response object."""
    msg = MagicMock()
    msg.content = content
    msg.tool_calls = tool_calls or []
    choice = MagicMock()
    choice.message = msg
    resp = MagicMock()
    resp.choices = [choice]
    resp.usage = None
    return resp


def _add_oauth_preset(key="grok"):
    model_presets.add_preset(
        key, label="Grok", base_url="https://api.x.ai/v1",
        api_format="openai", auth_type="oauth_xai",
        auth_config={"creds_file": "~/.grok/auth.json"}, model="grok-4",
    )


# ── Finding 1: tool-loop chat runner recovers from a 401 via refresh ───────


def test_chat_runner_refreshes_on_auth_error_and_retries(monkeypatch):
    """A 401 followed by a successful refresh → retry once → return content."""
    _add_oauth_preset("grok")
    fake_completion, AuthErr = _stub_litellm(monkeypatch, None)
    fake_completion.side_effect = [AuthErr("401 expired token"), _chat_response("hi")]
    # Strategy refresh yields a fresh token.
    monkeypatch.setattr(runners, "_try_refresh_for", lambda m: "fresh-token")

    runner = runners.litellm_chat_runner("grok")
    resp = runner(messages=[{"role": "user", "content": "yo"}], tools=[])

    assert resp.content == "hi"
    assert fake_completion.call_count == 2
    # The retry call carried the REFRESHED token directly (in-memory refresh).
    assert fake_completion.call_args_list[1].kwargs["api_key"] == "fresh-token"


def test_chat_runner_fires_alert_when_refresh_unavailable(monkeypatch):
    """No refresh available → fire an auth alert and re-raise the 401."""
    _add_oauth_preset("grok")
    fake_completion, AuthErr = _stub_litellm(monkeypatch, None)
    fake_completion.side_effect = AuthErr("401 dead")
    monkeypatch.setattr(runners, "_try_refresh_for", lambda m: None)

    runner = runners.litellm_chat_runner("grok")
    with pytest.raises(AuthErr):
        runner(messages=[{"role": "user", "content": "yo"}], tools=[])

    assert auth_alerts.has_active_alerts() is True


def test_chat_runner_fires_alert_when_refresh_still_401(monkeypatch):
    """Refresh succeeds but the retry still 401s → fire alert and re-raise."""
    _add_oauth_preset("grok")
    fake_completion, AuthErr = _stub_litellm(monkeypatch, None)
    fake_completion.side_effect = [AuthErr("401 a"), AuthErr("401 b")]
    monkeypatch.setattr(runners, "_try_refresh_for", lambda m: "fresh")

    runner = runners.litellm_chat_runner("grok")
    with pytest.raises(AuthErr):
        runner(messages=[{"role": "user", "content": "yo"}], tools=[])

    assert fake_completion.call_count == 2
    assert auth_alerts.has_active_alerts() is True


def test_chat_runner_clears_alert_on_success(monkeypatch):
    """A clean dispatch clears any prior alert so the banner self-heals."""
    _add_oauth_preset("grok")
    auth_alerts.raise_alert("grok", error_message="old", auth_type="oauth_xai")
    assert auth_alerts.has_active_alerts() is True
    _stub_litellm(monkeypatch, [_chat_response("ok")])

    runner = runners.litellm_chat_runner("grok")
    runner(messages=[{"role": "user", "content": "yo"}], tools=[])

    assert auth_alerts.has_active_alerts() is False


# ── Finding 2: summarizer-failed truncation budgets with the MAIN model ────


def test_summarizer_failed_truncation_budgets_with_main_model(monkeypatch, tmp_path):
    """When the summarizer raises, the truncation fallback must size the kept
    head with the MAIN model's tokenizer (``model``), not the summarizer's
    (``count_model``) — the truncated text lands in the main model's context."""
    captured: dict = {}

    def _fake_truncate(text, *, call_id, max_tokens, model=None):
        captured["model"] = model
        return f"[truncated {call_id}]"

    monkeypatch.setattr(_tool_sum, "truncate_tool_result", _fake_truncate)
    # Big result → over threshold; persist is a no-op stub.
    monkeypatch.setattr(_tool_sum, "count_tokens", lambda *a, **k: 9999)
    monkeypatch.setattr(_tool_sum, "persist_raw_result", lambda *a, **k: None)

    def _boom(*a, **k):
        raise RuntimeError("summarizer down")

    monkeypatch.setattr(_tool_sum, "summarize_tool_result", _boom)

    cfg = _tool_sum.ToolSummarizationConfig(
        enabled=True,
        threshold_tokens=100,
        summarizer_model="summarizer/small",
        tool_calls_dir=tmp_path / "tc",
    )
    with _tool_sum.with_config(cfg):
        # One tool call returns a big result, then the model emits final content.
        big = "x" * 5000
        tool = MagicMock()
        tool.params_schema = None
        tool.cost_class = None
        tool.name = "big_tool"
        tool.description = "returns a lot"
        tool.call = MagicMock(return_value=big)
        registry = {"big_tool": tool}

        scripted = [
            runners.ChatResponse(
                content=None,
                tool_calls=(runners.ToolCall(id="c1", name="big_tool", args={}),),
            ),
            runners.ChatResponse(content="done", tool_calls=()),
        ]
        chat_runner = runners.stub_chat_runner(scripted)

        out = runners.run_llm_with_tools(
            chat_runner=chat_runner,
            prompt="go",
            tool_loadout=("big_tool",),
            tool_registry=registry,
            summarizer_chat_runner_factory=lambda m: (lambda p: "summary"),
            model="main/big-model",
        )
        assert out == "done"

    # The fix: the fallback budgets the head against the MAIN model, never the
    # summarizer model.
    assert captured["model"] == "main/big-model"
    assert captured["model"] != "summarizer/small"
