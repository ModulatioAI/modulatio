# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
from pathlib import Path
from unittest.mock import patch
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
        return types.SimpleNamespace(stdout='{"result":"Hello","is_error":false}', returncode=0)

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
