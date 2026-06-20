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
    assert "--output-format" in argv and argv[argv.index("--output-format") + 1] == "json"
    assert argv[argv.index("--model") + 1] == "claude-opus-4-8"
    assert argv[argv.index("--append-system-prompt") + 1] == "You are helpful."
    assert argv[argv.index("--permission-mode") + 1] == "bypassPermissions"
    assert argv[argv.index("--add-dir") + 1] == "/proj"
    assert argv[-1] == "say hi"


def test_claude_env_scrubs_anthropic_key():
    env = claude_cli.claude_env({"PATH": "/bin", "ANTHROPIC_API_KEY": "sk-leak", "HOME": "/h"})
    assert "ANTHROPIC_API_KEY" not in env
    assert env["PATH"] == "/bin" and env["HOME"] == "/h"


def test_text_from_claude_json():
    payload = json.dumps({"result": "Hello", "is_error": False})
    assert claude_cli.text_from_claude_json(payload) == "Hello"


def test_text_from_claude_json_malformed_degrades():
    assert claude_cli.text_from_claude_json("not json") == ""


def test_seat_context_sets_and_restores(tmp_path):
    with claude_cli.seat_context(tmp_path, ("/granted",)):
        ws, add_dirs = claude_cli.current_seat_context()
        assert ws == tmp_path and add_dirs == ["/granted"]
    # restored after the block → temp fallback (never the leaked prior workspace)
    assert claude_cli.seat_context_var.get() == (None, ())  # var restored to default


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
