# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
import json
from modulatio import claude_cli


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
