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
    # KICKOFF seat (producer/QC/plan/reflect): the sub-agent/workflow tools are
    # stripped so Clay can't spawn a background crew and ship a deferral note.
    assert captured["disallowed_tools"] == runners.claude_cli._DISALLOWED_TOOLS
    # Fail-closed confinement: a POSITIVE allowlist of
    # non-process built-ins + safe-mode (no MCP/hooks/plugins) so a configured
    # MCP command-tool or hook can't become a process-exec surface.
    assert captured["allowed_tools"] == runners.claude_cli._ALLOWED_CONFINED_TOOLS
    assert captured["safe_mode"] is True


def test_clay_chat_runner_returns_chatresponse(monkeypatch):
    monkeypatch.setattr(model_presets, "load_presets", lambda: {"clay": dict(_CLAY_PRESET)})
    monkeypatch.setattr(oauth_helpers, "find_claude_binary", lambda: "/x/claude")
    captured = {}
    monkeypatch.setattr(runners.claude_cli, "run_claude",
                        lambda **kw: (captured.update(kw) or "ARTIFACT BODY"))

    runner = runners.litellm_chat_runner("clay")
    resp = runner(messages=[{"role": "system", "content": "sys"},
                            {"role": "user", "content": "build the thing"}], tools=[])
    assert resp.content == "ARTIFACT BODY"
    assert resp.tool_calls == ()
    # The interactive seat carries NO tools of its own: it acts through the
    # engine's tool loop, where each request meets the operator's typed grants
    # before it happens. A native tool cannot — it runs under bypassPermissions
    # against mounts fixed before the turn, so a folder granted for reading
    # arrives writable and a shell inside it executes there.
    assert captured.get("allowed_tools") == runners.claude_cli.TOOLS_NONE
    assert captured.get("safe_mode") is True
    assert captured.get("disallowed_tools") == runners.claude_cli._DISALLOWED_TOOLS


def test_interactive_seat_asks_the_engine_to_act_instead_of_acting(monkeypatch):
    """The seat's request comes back as a tool call for the engine to dispatch,
    which is what puts it in front of the operator's typed grants. Acting
    inside the subprocess reaches nothing that can check it."""
    monkeypatch.setattr(model_presets, "load_presets", lambda: {"clay": dict(_CLAY_PRESET)})
    monkeypatch.setattr(oauth_helpers, "find_claude_binary", lambda: "/x/claude")
    captured = {}
    monkeypatch.setattr(
        runners.claude_cli, "run_claude",
        lambda **kw: (captured.update(kw) or
                      'reading it now\n```modulatio-tool\n'
                      '{"name": "read_file", "arguments": {"path": "/outside/notes.md"}}\n```'))

    runner = runners.litellm_chat_runner("clay")
    resp = runner(
        messages=[{"role": "user", "content": "read the notes"}],
        tools=[{"type": "function", "function": {
            "name": "read_file", "description": "read a file",
            "parameters": {"type": "object"}}}],
    )

    assert [c.name for c in resp.tool_calls] == ["read_file"]
    assert resp.tool_calls[0].args == {"path": "/outside/notes.md"}
    # The prose survives; the request is not left in it twice.
    assert resp.content == "reading it now"
    assert "modulatio-tool" not in resp.content
    # The seat is told what it may ask for.
    assert "read_file" in (captured.get("system") or "")


def test_the_interactive_seat_is_shown_what_it_already_asked_and_got(monkeypatch):
    """The binary is invoked fresh each turn and keeps nothing between calls,
    so a flattening that drops the assistant turns and tool results hides from
    the seat both what it asked for and what came back — and it asks again."""
    monkeypatch.setattr(model_presets, "load_presets", lambda: {"clay": dict(_CLAY_PRESET)})
    monkeypatch.setattr(oauth_helpers, "find_claude_binary", lambda: "/x/claude")
    captured = {}
    monkeypatch.setattr(runners.claude_cli, "run_claude",
                        lambda **kw: (captured.update(kw) or "done"))

    runners.litellm_chat_runner("clay")(
        messages=[
            {"role": "user", "content": "read the notes"},
            {"role": "assistant", "content": "reading", "tool_calls": [
                {"id": "clay-0", "type": "function",
                 "function": {"name": "read_file", "arguments": '{"path": "n.md"}'}}]},
            {"role": "tool", "tool_call_id": "clay-0", "content": "THE FILE BODY"},
        ],
        tools=[],
    )

    prompt = captured["prompt"]
    assert "THE FILE BODY" in prompt
    assert "read_file" in prompt
    # The result is tied to the call it answers, so several in one turn stay apart.
    assert "clay-0" in prompt
