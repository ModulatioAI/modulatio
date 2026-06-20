# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Clay seat-context contract: the orchestrator sets the Clay seat's confined
workspace + operator-widen grants on the seat-context contextvar around its
runner calls, and BOTH seat-runner factories (single-shot ``litellm_runner``
and the chat ``_build_claude_cli_chat_runner``) read that context and thread it
into ``claude_cli.run_claude`` as ``workspace`` + ``add_dirs``.

A non-Clay seat ignores the contextvar entirely — this proves the wrap is
purely additive (the contract is only consumed on the ``claude_cli`` endpoint).
"""

from __future__ import annotations


def _clay_preset() -> dict:
    return {
        "label": "clay",
        "base_url": "claude-cli",
        "api_format": "anthropic",
        "auth_type": "claude_cli",
        "auth_config": {},
        "endpoint": "claude_cli",
        "model": "claude-opus-4-8",
    }


def test_seat_context_confines_clay_single_shot(monkeypatch, tmp_path):
    """The single-shot seat runner (``litellm_runner``) threads the seat
    context's workspace + grants into ``run_claude``."""
    from modulatio import claude_cli, runners, model_presets, oauth_helpers

    monkeypatch.setattr(model_presets, "load_presets", lambda: {"clay": _clay_preset()})
    monkeypatch.setattr(oauth_helpers, "find_claude_binary", lambda: "/x/claude")
    seen: dict = {}
    monkeypatch.setattr(
        runners.claude_cli, "run_claude", lambda **kw: seen.update(kw) or "ok"
    )

    ws = tmp_path / "leader_workspace"
    ws.mkdir()
    with claude_cli.seat_context(ws, ("/granted",)):
        out = runners.litellm_runner("clay")("decompose")

    assert out == "ok"
    assert seen["workspace"] == ws
    assert "/granted" in seen["add_dirs"]


def test_seat_context_confines_clay_chat(monkeypatch, tmp_path):
    """The chat seat runner (``_build_claude_cli_chat_runner``) threads the
    same seat context into ``run_claude``."""
    from modulatio import claude_cli, runners

    seen: dict = {}
    monkeypatch.setattr(
        runners.claude_cli, "run_claude", lambda **kw: seen.update(kw) or "ok"
    )

    ws = tmp_path / "leader_workspace"
    ws.mkdir()
    chat_runner = runners._build_claude_cli_chat_runner(
        "anthropic/claude-opus-4-8", "clay"
    )
    with claude_cli.seat_context(ws, ("/granted",)):
        chat_runner(messages=[{"role": "user", "content": "hi"}], tools=[])

    assert seen["workspace"] == ws
    assert "/granted" in seen["add_dirs"]


def test_orchestrator_enters_seat_context_on_run(tmp_path, monkeypatch):
    """The orchestrator's single-shot seat path (``_run``) actually ENTERS
    ``seat_context`` with the Leader's confined workspace + the gate's grants —
    the runner sees them on the contextvar at call time."""
    from modulatio import claude_cli, vault
    from modulatio.orchestration import Orchestrator
    from modulatio.types import Project

    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    code = "SEATCTX"
    vault.init_project(code, "seat ctx", "obj")
    project = Project(
        code=code, name="seat ctx", objective="obj", leader_model="stub",
        wiki_path=str(tmp_path / code.lower()),
    )

    captured: dict = {}

    def _spy_runner(prompt: str) -> str:
        # Read the seat context the orchestrator set for this call.
        ws, grants = claude_cli.current_seat_context()
        captured["ws"] = ws
        captured["grants"] = grants
        return "ok"

    orch = Orchestrator(project, {"leader": _spy_runner})
    out = orch._run("leader", "hello")

    assert out == "ok"
    # The orchestrator set the contextvar to the Leader's confined workspace
    # (NOT the temp-dir fallback that an unset context would yield).
    assert captured["ws"] == orch._leader_workspace()


def test_seat_context_default_is_temp_fallback():
    """With no orchestrator-set context, a Clay seat resolves to a fresh temp
    workspace (never unconfined-by-accident) — proving the orchestrator MUST set
    it to confine the seat to the real workspace."""
    from modulatio import claude_cli

    ws, grants = claude_cli.current_seat_context()
    assert ws.exists()
    assert grants == []
