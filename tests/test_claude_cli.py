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


def test_build_claude_argv_disallows_background_workflow_and_subagent_spawners():
    """KICKOFF guard: a confined seat strips Claude Code's UNBOUNDED background
    ``Workflow`` orchestrator AND the ``Task``/``Agent`` sub-agent spawners. The
    claude CLI has no max-N-subagents knob, so a producer could loop ``Task`` to
    fold N hidden attempts into one seat call — dodging Modulatio's retry counter
    on the (unmetered) subscription budget. A confined seat produces its OWN
    artifact; the orchestrator owns the swarm. (The harness lane keeps the full
    loadout — see the default-no-strip test above.)"""
    argv = claude_cli.build_claude_argv(
        claude_bin="/usr/bin/claude", model="m", prompt="go",
        disallowed_tools=claude_cli._DISALLOWED_TOOLS,
    )
    i = argv.index("--disallowedTools")
    disallowed = argv[i + 1 : i + 1 + len(claude_cli._DISALLOWED_TOOLS)]
    assert "Workflow" in disallowed
    assert "Task" in disallowed and "Agent" in disallowed
    # the variadic must be followed by another flag, never swallow the prompt
    assert argv[i + 1 + len(claude_cli._DISALLOWED_TOOLS)].startswith("-")
    assert argv[-1] == "go"


def test_confined_seat_strips_shell_so_it_cannot_re_exec_claude():
    """Stripping only Workflow/Task/Agent left ``Bash`` available,
    and a confined seat (``--permission-mode bypassPermissions``) could use Bash to
    re-exec the claude binary WITHOUT ``--disallowedTools`` — the nested process
    regains the spawners, defeating the zero-sub-agent bound. Removing the shell
    tools (Bash + its background-shell management) leaves no process-exec surface,
    so there is no way to launch a nested claude at all."""
    disallowed = set(claude_cli._DISALLOWED_TOOLS)
    assert {"Bash", "BashOutput", "KillShell"} <= disallowed, (
        "a confined kickoff seat must have no shell/exec tool — otherwise it can "
        "re-exec claude -p and recover Workflow/Task/Agent"
    )


def test_confined_argv_is_fail_closed_tools_restricts_the_builtin_set():
    """Under ``--permission-mode bypassPermissions``,
    ``--allowedTools`` (a PERMISSION allow-list) does NOT make omitted built-ins
    unavailable — the option that restricts the available built-in SET is
    ``--tools``. So the confined argv must pass the non-process set through
    ``--tools`` (the fail-closed gate), plus ``--safe-mode`` (no customizations);
    ``--allowedTools`` + the denylist remain as belts."""
    argv = claude_cli.build_claude_argv(
        claude_bin="/usr/bin/claude", model="m", prompt="go",
        allowed_tools=claude_cli._ALLOWED_CONFINED_TOOLS,
        safe_mode=True,
        disallowed_tools=claude_cli._DISALLOWED_TOOLS,
    )
    assert "--safe-mode" in argv
    # the available-set gate: --tools with exactly the non-process built-ins
    i = argv.index("--tools")
    tools = set(argv[i + 1: i + 1 + len(claude_cli._ALLOWED_CONFINED_TOOLS)])
    assert tools == set(claude_cli._ALLOWED_CONFINED_TOOLS)
    assert not (tools & {"Bash", "BashOutput", "KillShell", "Task", "Agent", "Workflow"})
    # variadic must be followed by a flag, never swallow the prompt
    assert argv[i + 1 + len(claude_cli._ALLOWED_CONFINED_TOOLS)].startswith("-")
    assert argv[-1] == "go"
    # the harness lane (no confinement args) gets none of the restrictors
    plain = claude_cli.build_claude_argv(claude_bin="/usr/bin/claude", model="m", prompt="go")
    assert "--safe-mode" not in plain and "--tools" not in plain and "--allowedTools" not in plain


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
    with claude_cli.seat_context(tmp_path, ("/granted",), read_only_roots=("/ro",)):
        ws, add_dirs, ro_dirs = claude_cli.current_seat_context()
        assert ws == tmp_path and add_dirs == ["/granted"] and ro_dirs == ["/ro"]
    # restored after the block → temp fallback (never the leaked prior workspace)
    assert claude_cli.seat_context_var.get() == (None, (), ())  # var restored to default


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


def test_find_claude_binary_override_must_be_executable_file(monkeypatch, tmp_path):
    """A directory (searchable, so os.access X_OK is true) or a non-executable
    file is not a valid override — fall through to PATH rather than returning it."""
    monkeypatch.setattr(oauth_helpers.shutil, "which", lambda n: None)
    a_dir = tmp_path / "claude-dir"
    a_dir.mkdir()
    monkeypatch.setenv("MODULATIO_CLAUDE_BIN", str(a_dir))
    assert oauth_helpers.find_claude_binary() is None
    non_exec = tmp_path / "claude.txt"
    non_exec.write_text("not a binary\n")
    monkeypatch.setenv("MODULATIO_CLAUDE_BIN", str(non_exec))
    assert oauth_helpers.find_claude_binary() is None


def test_find_claude_binary_path(monkeypatch):
    monkeypatch.delenv("MODULATIO_CLAUDE_BIN", raising=False)
    monkeypatch.setattr(oauth_helpers.shutil, "which", lambda n: "/x/claude" if n == "claude" else None)
    assert oauth_helpers.find_claude_binary() == "/x/claude"


def test_find_claude_binary_missing(monkeypatch):
    monkeypatch.delenv("MODULATIO_CLAUDE_BIN", raising=False)
    monkeypatch.setattr(oauth_helpers.shutil, "which", lambda n: None)
    assert oauth_helpers.find_claude_binary() is None


# ── empty-reply fallback guard (arc #3, 2026-07-02) ─────────────────────────
# A `claude -p` runtime hiccup can exit 0 with NO assistant text (observed
# live post-B5: Clay's converse returned an empty message). An empty reply is
# a failed call, not a completion — reason it, retry it, and let run_claude's
# out-of-retries path raise ClaudeUnavailable into the model-fallback chain
# instead of propagating "" downstream.


def test_empty_reply_on_clean_exit_is_an_error_reason():
    reason = claude_cli._claude_error_reason(0, "")
    assert reason is not None
    assert "empty reply" in reason


def test_whitespace_only_reply_is_an_error_reason():
    assert claude_cli._claude_error_reason(0, "  \n\t ") is not None


def test_empty_reply_is_retriable():
    reason = claude_cli._claude_error_reason(0, "")
    assert claude_cli._claude_error_retriable(reason)


def test_real_text_on_clean_exit_is_not_an_error():
    assert claude_cli._claude_error_reason(0, "Here is the plan.") is None


def test_an_unparseable_tool_request_is_dropped_not_guessed():
    """A request the engine cannot read is one it cannot check against a grant,
    and filling in arguments for it would act on a guess. The block still
    leaves the prose, so the seat reads its own malformed output back."""
    from modulatio.claude_cli import parse_tool_protocol

    prose, calls = parse_tool_protocol(
        "before\n```modulatio-tool\n{not json at all}\n```\nafter")
    assert calls == []
    assert "before" in prose and "after" in prose
    assert "not json at all" not in prose

    # A block naming no tool is equally unusable.
    _, unnamed = parse_tool_protocol('```modulatio-tool\n{"arguments": {"a": 1}}\n```')
    assert unnamed == []

    # An unterminated fence is not a request; it stays as text.
    tail_prose, none = parse_tool_protocol('```modulatio-tool\n{"name": "x"}')
    assert none == []
    assert "name" in tail_prose


def test_ordinary_fenced_code_in_a_reply_is_not_a_tool_request():
    """The protocol carries its own fence label, so a seat writing an ordinary
    json block is not read as asking the engine to run something."""
    from modulatio.claude_cli import parse_tool_protocol

    prose, calls = parse_tool_protocol(
        'here is config\n```json\n{"name": "run_shell"}\n```')
    assert calls == []
    assert "run_shell" in prose


def test_a_seat_that_is_not_resuming_writes_no_session_file():
    """The engine replays the whole conversation into every invocation, so a
    persisted session adds nothing to read back — and leaves a transcript of
    the operator's work in a place the engine does not manage and a wipe does
    not reach."""
    from modulatio.claude_cli import build_claude_argv

    argv = build_claude_argv(claude_bin="/x/claude", model="m", prompt="hi")
    assert "--no-session-persistence" in argv

    # A seat deliberately resuming or pinning a conversation still may.
    assert "--no-session-persistence" not in build_claude_argv(
        claude_bin="/x/claude", model="m", prompt="hi", resume="abc")
    assert "--no-session-persistence" not in build_claude_argv(
        claude_bin="/x/claude", model="m", prompt="hi", session_id="abc")
