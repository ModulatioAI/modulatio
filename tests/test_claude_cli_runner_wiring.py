# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Clay (claude_cli) runner wiring: single-shot + chat runner branches dispatch
to run_claude and return the expected shapes without calling real LLM APIs."""

from modulatio import model_presets, runners, oauth_helpers

_CLAY_PRESET = {
    "label": "Clay (opus)", "base_url": "claude-cli", "api_format": "anthropic",
    "auth_type": "claude_cli", "auth_config": {}, "endpoint": "claude_cli",
    "model": "claude-opus-4-8",
}


def test_clay_single_shot_runner_returns_text(monkeypatch):
    monkeypatch.setattr(model_presets, "load_presets", lambda: {"clay": dict(_CLAY_PRESET)})
    monkeypatch.setattr(oauth_helpers, "find_claude_binary", lambda: "/x/claude")
    captured = {}

    def fake_run_claude(**kw):
        captured.update(kw)
        return "DECOMPOSED"

    monkeypatch.setattr(runners.claude_cli, "run_claude", fake_run_claude)
    out = runners.litellm_runner("clay")("break this down")
    assert out == "DECOMPOSED"
    assert captured["model"] == "claude-opus-4-8"
    assert captured["prompt"].endswith("break this down")


def test_clay_chat_runner_returns_chatresponse(monkeypatch):
    monkeypatch.setattr(model_presets, "load_presets", lambda: {"clay": dict(_CLAY_PRESET)})
    monkeypatch.setattr(oauth_helpers, "find_claude_binary", lambda: "/x/claude")
    monkeypatch.setattr(runners.claude_cli, "run_claude", lambda **kw: "ARTIFACT BODY")

    runner = runners.litellm_chat_runner("clay")
    resp = runner(messages=[{"role": "system", "content": "sys"},
                            {"role": "user", "content": "build the thing"}], tools=[])
    assert resp.content == "ARTIFACT BODY"
    assert resp.tool_calls == ()
