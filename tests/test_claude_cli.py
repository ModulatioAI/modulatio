# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
import json
from modulatio import auth_strategies, claude_cli, oauth_helpers, provider_catalog
from modulatio.provider_catalog import CatalogModel


def test_build_claude_argv_single_shot():
    argv = claude_cli.build_claude_argv(
        claude_bin="/usr/bin/claude", model="claude-opus-4-8",
        prompt="say hi", system="You are helpful.", add_dirs=["/proj"],
    )
    assert argv[:2] == ["/usr/bin/claude", "-p"]
    # stream-json (+ --verbose) so Clay's in-sandbox tool calls are observable.
    assert argv[argv.index("--output-format") + 1] == "stream-json"
    assert "--verbose" in argv
    assert argv[argv.index("--model") + 1] == "claude-opus-4-8"
    assert argv[argv.index("--append-system-prompt") + 1] == "You are helpful."
    assert argv[argv.index("--permission-mode") + 1] == "bypassPermissions"
    assert argv[argv.index("--add-dir") + 1] == "/proj"
    assert argv[-1] == "say hi"
    # HARNESS default: no tools stripped unless the caller opts in (the Leader's
    # solo/converse lane keeps its full agentic loadout).
    assert "--disallowedTools" not in argv


def test_build_claude_argv_disallows_background_workflow_but_keeps_subagents():
    """KICKOFF guard: a confined seat strips Claude Code's UNBOUNDED background
    ``Workflow`` orchestrator (the 'launched a workflow, watch /workflows'
    deferral) but KEEPS the synchronous ``Task``/``Agent`` spawners — Clif: an
    LLM may spawn 1-2 helper agents if it needs to, it just can't fire off an
    invisible background crew."""
    argv = claude_cli.build_claude_argv(
        claude_bin="/usr/bin/claude", model="m", prompt="go",
        disallowed_tools=claude_cli._DISALLOWED_TOOLS,
    )
    i = argv.index("--disallowedTools")
    disallowed = argv[i + 1 : i + 1 + len(claude_cli._DISALLOWED_TOOLS)]
    assert "Workflow" in disallowed
    assert "Task" not in disallowed and "Agent" not in disallowed
    # the variadic must be followed by another flag, never swallow the prompt
    assert argv[i + 1 + len(claude_cli._DISALLOWED_TOOLS)].startswith("-")
    assert argv[-1] == "go"


def test_claude_env_scrubs_anthropic_key():
    env = claude_cli.claude_env({"PATH": "/bin", "ANTHROPIC_API_KEY": "sk-leak", "HOME": "/h"})
    assert "ANTHROPIC_API_KEY" not in env
    assert env["PATH"] == "/bin" and env["HOME"] == "/h"


def test_text_from_claude_json():
    payload = json.dumps({"result": "Hello", "is_error": False})
    assert claude_cli.text_from_claude_json(payload) == "Hello"


def test_text_from_claude_json_malformed_degrades():
    assert claude_cli.text_from_claude_json("not json") == ""


def test_parse_claude_stream_extracts_tools_and_result():
    """The stream parser pairs each tool_use with its tool_result, calls the
    sink, and returns the final result — malformed lines are skipped."""
    calls = []
    stream = [
        '{"type":"system","subtype":"init"}',
        '{"type":"assistant","message":{"content":[{"type":"tool_use","id":"t1",'
        '"name":"WebSearch","input":{"query":"rc cars"}}]}}',
        '{"type":"user","message":{"content":[{"type":"tool_result",'
        '"tool_use_id":"t1","content":[{"type":"text","text":"results..."}]}]}}',
        '{"type":"result","subtype":"success","result":"Final answer"}',
        "not json — skipped",
    ]
    out = claude_cli.parse_claude_stream(
        stream, on_tool_call=lambda n, a, r: calls.append((n, a, r))
    )
    assert out == "Final answer"
    assert calls == [("WebSearch", {"query": "rc cars"}, "results...")]


def test_parse_claude_stream_no_sink_just_returns_result():
    out = claude_cli.parse_claude_stream(
        ['{"type":"assistant","message":{"content":[{"type":"tool_use","id":"t1",'
         '"name":"X","input":{}}]}}',
         '{"type":"result","result":"R"}'],
        on_tool_call=None,
    )
    assert out == "R"


def test_seat_context_sets_and_restores(tmp_path):
    with claude_cli.seat_context(tmp_path, ("/granted",)):
        ws, add_dirs = claude_cli.current_seat_context()
        assert ws == tmp_path and add_dirs == ["/granted"]
    # restored after the block → temp fallback (never the leaked prior workspace)
    assert claude_cli.seat_context_var.get() == (None, ())  # var restored to default


def test_claude_cli_strategy_reads_no_secret():
    strat = auth_strategies.build_strategy("claude_cli", {})
    assert strat.load_token() is None
    assert strat.attribution_kwargs() == {}


def test_claude_cli_registered():
    assert "claude_cli" in auth_strategies.registered_auth_types()


def test_claude_cli_strategy_is_available(monkeypatch):
    from modulatio import oauth_helpers
    strat = auth_strategies.build_strategy("claude_cli", {})
    monkeypatch.setattr(oauth_helpers, "find_claude_binary", lambda: "/x/claude")
    assert strat.is_available() is True
    monkeypatch.setattr(oauth_helpers, "find_claude_binary", lambda: None)
    assert strat.is_available() is False


def test_clay_provider_registered_and_reads_as_avatar():
    p = provider_catalog.PROVIDERS["claude_cli"]
    assert p.request_endpoint == "claude_cli"
    assert "avatar" in p.name.lower() or "clay" in p.name.lower()  # teaches who Clay is
    assert p.auth_options[0].auth_type == "claude_cli"
    assert p.models_source.picklist_key == "claude_cli"
    m = CatalogModel(id="claude-opus-4-8", name="Claude Opus 4.8", provider_id="claude_cli")
    kw = provider_catalog.preset_kwargs(p, m, p.auth_options[0])
    assert kw["endpoint"] == "claude_cli"


def test_doctor_clay_check_present_when_binary_found(monkeypatch, capsys):
    from modulatio import cli, oauth_helpers
    monkeypatch.setattr(oauth_helpers, "find_claude_binary", lambda: "/x/claude")
    cli._clay_doctor_check()
    out = capsys.readouterr().out.lower()
    assert "claude" in out


def test_doctor_clay_check_warns_when_missing(monkeypatch, capsys):
    from modulatio import cli, oauth_helpers
    monkeypatch.setattr(oauth_helpers, "find_claude_binary", lambda: None)
    cli._clay_doctor_check()
    out = capsys.readouterr().out.lower()
    assert "claude" in out and ("not" in out or "install" in out)




def test_find_claude_binary_env_override(monkeypatch, tmp_path):
    fake = tmp_path / "claude"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    monkeypatch.setenv("MODULATIO_CLAUDE_BIN", str(fake))
    assert oauth_helpers.find_claude_binary() == str(fake)


def test_find_claude_binary_path(monkeypatch):
    monkeypatch.delenv("MODULATIO_CLAUDE_BIN", raising=False)
    monkeypatch.setattr(oauth_helpers.shutil, "which", lambda n: "/x/claude" if n == "claude" else None)
    assert oauth_helpers.find_claude_binary() == "/x/claude"


def test_find_claude_binary_missing(monkeypatch):
    monkeypatch.delenv("MODULATIO_CLAUDE_BIN", raising=False)
    monkeypatch.setattr(oauth_helpers.shutil, "which", lambda n: None)
    assert oauth_helpers.find_claude_binary() is None
