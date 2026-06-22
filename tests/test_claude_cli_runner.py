# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
from pathlib import Path
from unittest.mock import patch

import pytest

from modulatio import claude_cli


def test_run_claude_refuses_without_sandbox(tmp_path):
    from modulatio import sandbox
    with patch.object(sandbox, "is_sandbox_available", return_value=False):
        try:
            claude_cli.run_claude(
                claude_bin="/x/claude", model="m", prompt="hi",
                workspace=tmp_path, add_dirs=[],
            )
            assert False, "expected refusal"
        except RuntimeError as e:
            assert "sandbox" in str(e).lower()


def test_run_claude_never_binds_home_dir(tmp_path, monkeypatch):
    """Wild Bill BLOCK: a claude binary at $HOME/claude must NOT cause the whole
    home directory to be RO-bound back into the sandbox (after --tmpfs /home)."""
    from pathlib import Path
    from modulatio import sandbox, claude_cli
    captured = {}
    monkeypatch.setattr(sandbox, "is_sandbox_available", lambda: True)
    monkeypatch.setattr(sandbox, "build_sandboxed_argv",
                        lambda argv, root, **kw: (captured.update(kw) or (list(argv), {"PATH": "/bin"})))
    import types
    monkeypatch.setattr(claude_cli.subprocess, "run",
                        lambda *a, **k: types.SimpleNamespace(stdout='{"result":"ok"}', returncode=0))
    claude_cli.run_claude(claude_bin=str(Path.home() / "claude"), model="m", prompt="hi",
                          workspace=tmp_path, add_dirs=[], timeout=1)
    binds = [str(p) for p in captured["extra_binds"]]
    assert str(Path.home()) not in binds  # the HOME DIR is never bound
    # binding the single file $HOME/claude is acceptable; the home DIR is not


def test_run_claude_sandboxes_and_scrubs(tmp_path, monkeypatch):
    from modulatio import sandbox
    captured = {}

    def fake_build(argv, root, **kw):
        captured["argv"] = argv
        captured["kw"] = kw
        return list(argv), {"PATH": "/bin", "HOME": str(Path.home())}

    def fake_run(argv, env=None, **kw):
        captured["env"] = env
        import types
        # stream-json output: the final result arrives in a `result` event.
        return types.SimpleNamespace(
            stdout='{"type":"result","subtype":"success","result":"Hello"}',
            returncode=0,
        )

    monkeypatch.setattr(sandbox, "is_sandbox_available", lambda: True)
    monkeypatch.setattr(sandbox, "build_sandboxed_argv", fake_build)
    monkeypatch.setattr(claude_cli.subprocess, "run", fake_run)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-leak")

    out = claude_cli.run_claude(
        claude_bin="/x/claude", model="m", prompt="hi",
        workspace=tmp_path, add_dirs=[str(tmp_path / "proj")],
    )
    assert out == "Hello"
    rw = [str(p) for p in captured["kw"]["extra_rw_roots"]]
    assert str(Path.home() / ".claude") in rw
    assert captured["kw"]["allow_network"] is True
    assert "ANTHROPIC_API_KEY" not in captured["env"]


def test_run_claude_raises_unavailable_on_api_error(tmp_path, monkeypatch):
    """A ``claude -p`` provider error (529 overload) RAISES ClaudeUnavailable so the
    model-fallback chain engages — it is NOT returned as a 'completion' that a
    downstream JSON parse then crashes on. On a transient overload it waits +
    retries (the wait state) before giving up."""
    import types

    from modulatio import sandbox

    monkeypatch.setattr(sandbox, "is_sandbox_available", lambda: True)
    monkeypatch.setattr(
        sandbox, "build_sandboxed_argv",
        lambda argv, root, **kw: (list(argv), {"PATH": "/bin", "HOME": str(Path.home())}),
    )
    calls = {"n": 0}

    def fake_run(*a, **k):
        calls["n"] += 1
        return types.SimpleNamespace(
            stdout='{"type":"result","subtype":"error","is_error":true,'
                   '"result":"API Error: 529 Overloaded. Server-side, try again."}',
            returncode=1,
        )

    monkeypatch.setattr(claude_cli.subprocess, "run", fake_run)
    slept: list = []
    monkeypatch.setattr(claude_cli.time, "sleep", slept.append)  # don't actually wait

    with pytest.raises(claude_cli.ClaudeUnavailable):
        claude_cli.run_claude(claude_bin="/x/claude", model="m", prompt="hi",
                              workspace=tmp_path, add_dirs=[])
    assert calls["n"] > 1 and slept  # it waited + retried the transient overload


def test_run_claude_streams_tools_to_seat_activity_sink(tmp_path, monkeypatch):
    """WIRING: run_claude parses the stream-json output and feeds each tool call
    to the sink the orchestrator sets via seat_context — so Clay's in-sandbox
    activity reaches the SAME logger as a litellm producer's (not just the part:
    this drives the real run_claude -> parse -> sink path)."""
    from modulatio import sandbox

    monkeypatch.setattr(sandbox, "is_sandbox_available", lambda: True)
    monkeypatch.setattr(
        sandbox, "build_sandboxed_argv",
        lambda argv, root, **kw: (list(argv), {"PATH": "/bin", "HOME": str(Path.home())}),
    )
    stream = "\n".join([
        '{"type":"assistant","message":{"content":[{"type":"tool_use","id":"t1",'
        '"name":"WebFetch","input":{"url":"http://x"}}]}}',
        '{"type":"user","message":{"content":[{"type":"tool_result",'
        '"tool_use_id":"t1","content":"page text"}]}}',
        '{"type":"result","result":"done"}',
    ])
    import types
    monkeypatch.setattr(
        claude_cli.subprocess, "run",
        lambda *a, **k: types.SimpleNamespace(stdout=stream, returncode=0),
    )

    seen = []
    with claude_cli.seat_context(tmp_path, (), on_tool_call=lambda n, a, r: seen.append((n, a, r))):
        out = claude_cli.run_claude(
            claude_bin="/x/claude", model="m", prompt="hi",
            workspace=tmp_path, add_dirs=[],
        )

    assert out == "done"
    assert seen == [("WebFetch", {"url": "http://x"}, "page text")]
