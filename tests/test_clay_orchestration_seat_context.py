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

import json
import stat

import pytest


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


def test_clay_single_shot_omits_no_think_prefix(monkeypatch, tmp_path):
    """Clay (claude_cli) must NOT get the ``/no_think`` prefix — the Claude CLI
    reads a leading ``/`` as a slash command, returning 'Unknown command:
    /no_think' and breaking every single-shot Clay call (planner/QC/reflect)."""
    from modulatio import claude_cli, model_presets, oauth_helpers, runners

    monkeypatch.setattr(model_presets, "load_presets", lambda: {"clay": _clay_preset()})
    monkeypatch.setattr(oauth_helpers, "find_claude_binary", lambda: "/x/claude")
    seen: dict = {}
    monkeypatch.setattr(
        runners.claude_cli, "run_claude", lambda **kw: seen.update(kw) or "ok"
    )

    ws = tmp_path / "ws"
    ws.mkdir()
    with claude_cli.seat_context(ws, ()):
        # disable_thinking defaults True — the prompt must still pass through clean.
        runners.litellm_runner("clay")("decompose this goal")

    assert seen["prompt"] == "decompose this goal"
    assert not seen["prompt"].startswith("/no_think")


def test_seat_context_confines_clay_chat(monkeypatch, tmp_path):
    """The chat seat runner (``_build_claude_cli_chat_runner``) threads the
    same seat context into ``run_claude``."""
    from modulatio import claude_cli, oauth_helpers, runners

    seen: dict = {}
    # Mock the binary lookup so the test never depends on `claude` being
    # installed (it isn't on CI) — the single-shot sibling does the same.
    monkeypatch.setattr(oauth_helpers, "find_claude_binary", lambda: "/x/claude")
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
        ws, grants, _ro = claude_cli.current_seat_context()
        captured["ws"] = ws
        captured["grants"] = grants
        return "ok"

    orch = Orchestrator(project, {"leader": _spy_runner})
    out = orch._run("leader", "hello")

    assert out == "ok"
    # The orchestrator set the contextvar to the Leader's confined workspace
    # (NOT the temp-dir fallback that an unset context would yield).
    assert captured["ws"] == orch._leader_workspace()


def test_orchestrator_enters_seat_context_on_run_agent_call(tmp_path, monkeypatch):
    """The orchestrator's per-agent direct path (``_run_agent_call``) ENTERS
    ``seat_context`` when an agent with a known model is in ``agent_runners`` —
    the spy runner sees the contextvar at call time (mirrors the ``_run`` test)."""
    from modulatio import claude_cli, roster, vault
    from modulatio.orchestration import Orchestrator
    from modulatio.types import Project

    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    code = "AGCALL"
    vault.init_project(code, "agent call ctx", "obj")
    project = Project(
        code=code, name="agent call ctx", objective="obj", leader_model="stub",
        wiki_path=str(tmp_path / code.lower()),
    )

    # Monkeypatch roster.load so the per-agent branch fires without disk I/O.
    fake_agent = roster.Agent(
        id="spy-agent", name="Spy", model="spy/model", tier="producer",
    )
    monkeypatch.setattr(roster, "load", lambda agent_id, project_code: fake_agent)

    captured: dict = {}

    def _spy_runner(prompt: str) -> str:
        ws, grants, _ro = claude_cli.current_seat_context()
        captured["ws"] = ws
        captured["grants"] = grants
        return "ok"

    orch = Orchestrator(
        project,
        {"leader": lambda p: "ok"},
        agent_runners={"spy/model": _spy_runner},
    )
    out = orch._run_agent_call("spy-agent", "leader", "hello")

    assert out == "ok"
    # The orchestrator set the contextvar to the Leader's confined workspace.
    assert captured["ws"] == orch._leader_workspace()


def test_seat_context_default_is_temp_fallback():
    """With no orchestrator-set context, a Clay seat resolves to a fresh temp
    workspace (never unconfined-by-accident) — proving the orchestrator MUST set
    it to confine the seat to the real workspace."""
    from modulatio import claude_cli

    ws, grants, _ro = claude_cli.current_seat_context()
    assert ws.exists()
    assert grants == []


def _project_for(tmp_path, code):
    from modulatio import vault
    from modulatio.types import Project

    vault.init_project(code, code, "obj")
    return Project(code=code, name=code, objective="obj", leader_model="stub",
                   wiki_path=str(tmp_path / code.lower()))


def test_single_shot_run_threads_activity_sink(tmp_path, monkeypatch):
    """The single-shot ``_run`` path must thread a tool-call sink
    into the seat context so a Clay seat's in-sandbox tool calls reach the
    activity feed (Team TV) — not only the chat-loop path. Before the fix
    ``_seat_context()`` was entered with no sink, so ``seat_activity_var`` was
    None and producer/QC tool use was invisible."""
    from modulatio import claude_cli, vault
    from modulatio.orchestration import Orchestrator

    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    project = _project_for(tmp_path, "SINKRUN")
    events: list = []

    def _spy_runner(prompt: str) -> str:
        sink = claude_cli.seat_activity_var.get()
        assert sink is not None, "single-shot seat context must carry a tool-call sink"
        sink("Read", {"path": "x"}, "ok")  # simulate run_claude emitting a parsed call
        return "ok"

    orch = Orchestrator(project, {"leader": _spy_runner})
    orch.activity_callback = events.append
    out = orch._run("leader", "hello", task_id="T1")

    assert out == "ok"
    assert any(getattr(e, "phase", None) == "tool_call_ended" for e in events), (
        "the single-shot seat's tool call did not reach the activity feed"
    )
    assert claude_cli.seat_activity_var.get() is None  # reset after the call


def test_single_shot_agent_call_threads_activity_sink(tmp_path, monkeypatch):
    """Per-agent path: ``_run_agent_call`` (producer / QC-fixer
    direct dispatch) must thread the same sink."""
    from modulatio import claude_cli, roster, vault
    from modulatio.orchestration import Orchestrator

    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    project = _project_for(tmp_path, "SINKAGENT")
    fake_agent = roster.Agent(id="spy-agent", name="Spy", model="spy/model", tier="qc")
    monkeypatch.setattr(roster, "load", lambda agent_id, project_code: fake_agent)
    events: list = []

    def _spy_runner(prompt: str) -> str:
        sink = claude_cli.seat_activity_var.get()
        assert sink is not None, "per-agent seat context must carry a tool-call sink"
        sink("Edit", {"path": "y"}, "ok")
        return "ok"

    orch = Orchestrator(project, {"leader": lambda p: "ok"},
                        agent_runners={"spy/model": _spy_runner})
    orch.activity_callback = events.append
    out = orch._run_agent_call("spy-agent", "qc", "hello", task_id="T2")

    assert out == "ok"
    assert any(getattr(e, "phase", None) == "tool_call_ended" for e in events)
    assert claude_cli.seat_activity_var.get() is None


def test_single_shot_seat_writes_durable_transcript_without_subscriber(tmp_path, monkeypatch):
    """The single-shot seat sink must write a durable owner-only
    JSONL audit record (task/role/agent/tool/args/result/ts) — INDEPENDENT of any
    activity subscriber. Team TV is liveness; the transcript is the audit, and it
    must capture args + result, not just the tool name."""
    from modulatio import claude_cli, vault
    from modulatio.orchestration import Orchestrator

    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    project = _project_for(tmp_path, "AUDITRUN")

    def _spy_runner(prompt: str) -> str:
        claude_cli.seat_activity_var.get()("Read", {"path": "x.md"}, "file body")
        return "ok"

    orch = Orchestrator(project, {"leader": _spy_runner})
    # NO activity_callback installed — the durable write must still happen.
    orch._run("leader", "hello", task_id="T9", agent_id="leader")

    files = list((orch._scope_root() / "tool_calls").glob("seat_*.jsonl"))
    assert files, "no durable seat transcript written"
    rec = json.loads(files[0].read_text(encoding="utf-8").splitlines()[-1])
    assert rec["tool"] == "Read"
    assert rec["args"] == {"path": "x.md"}
    assert rec["result"] == "file body"
    assert rec["task_id"] == "T9" and rec["role"] == "leader" and rec["agent_id"] == "leader"
    assert "timestamp" in rec
    assert stat.S_IMODE(files[0].stat().st_mode) == 0o600  # owner-only


def test_single_shot_qc_fixer_seat_writes_durable_transcript(tmp_path, monkeypatch):
    """Same durable-audit guarantee on the per-agent (producer / QC-fixer) path."""
    from modulatio import claude_cli, roster, vault
    from modulatio.orchestration import Orchestrator

    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    project = _project_for(tmp_path, "AUDITQC")
    fake_agent = roster.Agent(id="qc-agent", name="QC", model="qc/model", tier="qc")
    monkeypatch.setattr(roster, "load", lambda agent_id, project_code: fake_agent)

    def _spy_runner(prompt: str) -> str:
        claude_cli.seat_activity_var.get()("Edit", {"path": "y.md"}, "patched")
        return "ok"

    orch = Orchestrator(project, {"leader": lambda p: "ok"},
                        agent_runners={"qc/model": _spy_runner})
    orch._run_agent_call("qc-agent", "qc", "hello", task_id="T10")

    files = list((orch._scope_root() / "tool_calls").glob("seat_*.jsonl"))
    assert files, "no durable QC-fixer seat transcript written"
    rec = json.loads(files[0].read_text(encoding="utf-8").splitlines()[-1])
    assert rec["tool"] == "Edit" and rec["result"] == "patched" and rec["role"] == "qc"


def test_single_shot_seat_sink_resets_on_exception(tmp_path, monkeypatch):
    """The seat activity sink must be cleared even if the runner raises, so a
    later non-Clay call can't inherit a stale sink (reset on success
    AND exception)."""
    from modulatio import claude_cli, vault
    from modulatio.orchestration import Orchestrator

    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    project = _project_for(tmp_path, "SINKBOOM")

    def _boom(prompt: str) -> str:
        assert claude_cli.seat_activity_var.get() is not None
        raise RuntimeError("boom")

    orch = Orchestrator(project, {"leader": _boom})
    with pytest.raises(RuntimeError, match="boom"):
        orch._run("leader", "hello", task_id="T3")
    assert claude_cli.seat_activity_var.get() is None  # reset despite the raise
