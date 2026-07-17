# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""ESC interrupt for the Leader's code harness.

The operator can stop the Leader mid-thought in the converse / solo-coding lane:
ESC in the TUI sets the converse orchestrator's ``abort_event``; the tool-loop
(`run_llm_with_tools`) checks it at the top of every iteration and returns a
clean note instead of grinding on. Cooperative — a single long in-flight call
finishes first; the interrupt lands at the next step boundary.
"""
from __future__ import annotations

import json
import threading
import types
from pathlib import Path

import pytest

from modulatio import runners, tools, vault
from modulatio.orchestration import Orchestrator
from modulatio.runners import ChatResponse, ToolCall
from modulatio.types import Project, ProjectState

PROJECT_CODE = "INT"


# ── runner-level: the cooperative abort check ───────────────────────────────

def test_run_llm_with_tools_aborts_before_first_model_call():
    runner = runners.stub_chat_runner([ChatResponse(content="never", tool_calls=())])
    out = runners.run_llm_with_tools(
        chat_runner=runner, prompt="x", tool_loadout=(), tool_registry={},
        max_iters=5, should_abort=lambda: True,
    )
    assert out == runners.INTERRUPTED_REPLY
    assert len(runner.calls) == 0  # bailed before ever calling the model


def test_run_llm_with_tools_aborts_after_a_tool_step():
    """A churning loop bails at the NEXT iteration once the flag flips (as ESC
    would mid tool-step) — without running to completion."""
    flag = {"abort": False}

    def echo(**k):
        flag["abort"] = True  # simulate ESC arriving during the tool step
        return "ok"

    runner = runners.stub_chat_runner([
        ChatResponse(content=None, tool_calls=(ToolCall(id="c1", name="echo", args={}),)),
        ChatResponse(content="done", tool_calls=()),
    ])
    registry = {"echo": tools.Tool(name="echo", description="e", call=echo)}
    out = runners.run_llm_with_tools(
        chat_runner=runner, prompt="x", tool_loadout=("echo",), tool_registry=registry,
        max_iters=5, should_abort=lambda: flag["abort"],
    )
    assert out == runners.INTERRUPTED_REPLY
    assert len(runner.calls) == 1  # only the first model call ran; bailed before the 2nd


def test_run_llm_with_tools_no_should_abort_runs_normally():
    runner = runners.stub_chat_runner([ChatResponse(content="done", tool_calls=())])
    out = runners.run_llm_with_tools(
        chat_runner=runner, prompt="x", tool_loadout=(), tool_registry={}, max_iters=5,
    )
    assert out == "done"


# ── orchestration wiring: converse threads the abort + resets it per turn ────

@pytest.fixture
def project(tmp_path: Path, monkeypatch) -> Project:
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    vault.init_project(PROJECT_CODE, "interrupt fixture", "obj")
    return Project(
        code=PROJECT_CODE, name="interrupt fixture", objective="obj",
        state=ProjectState.ACTIVE, leader_model="stub",
        wiki_path=str(vault.project_dir(PROJECT_CODE)),
    )


def _runners() -> dict:
    return {"leader": lambda p: "", "planner": lambda p: "```json\n[]\n```",
            "drafter": lambda p: "", "qc": lambda p: ""}


def test_converse_bails_when_abort_event_set_mid_loop(project: Project):
    """WIRING: converse passes ``should_abort=abort_event.is_set`` into the tool-
    loop, so an abort tripped during the turn returns the interrupted note rather
    than completing — proving the ESC path reaches the real loop."""
    calls = {"n": 0}

    def mock_leader(*, messages, tools, tool_choice=None):
        calls["n"] += 1
        orch.abort_event.set()  # ESC arrives during this model turn
        return ChatResponse(content=None, tool_calls=(
            ToolCall(id="c1", name="team_status", args={}),
        ))

    orch = Orchestrator(
        project, _runners(),
        chat_runners={"leader": mock_leader},
        chat_runner_models={"leader": "mock-model"},
    )
    reply = orch.converse("do a long multi-step thing")
    assert reply == runners.INTERRUPTED_REPLY
    assert calls["n"] == 1  # bailed before a second model call


def test_interrupted_turn_is_marked_in_durable_thread(project: Project):
    """An interrupted turn must be DISTINGUISHABLE from a real
    Leader reply in the durable jsonl. converse identity-compares the public
    sentinel and writes ``interrupted: true`` on that turn, so a future reader
    (an undo, a goal-evidence filter, a TUI 'interrupted' affordance) isn't left
    string-matching the prose."""
    def mock_leader(*, messages, tools, tool_choice=None):
        orch.abort_event.set()  # ESC during the model turn
        return ChatResponse(content=None, tool_calls=(
            ToolCall(id="c1", name="team_status", args={}),
        ))

    orch = Orchestrator(
        project, _runners(),
        chat_runners={"leader": mock_leader},
        chat_runner_models={"leader": "mock-model"},
    )
    reply = orch.converse("do a long multi-step thing")
    assert reply is runners.INTERRUPTED_REPLY  # identity, not string-equality

    turns = [json.loads(ln) for ln in
             orch._conversation_path().read_text(encoding="utf-8").splitlines() if ln.strip()]
    leader_turns = [t for t in turns if t["role"] == "leader"]
    assert leader_turns and leader_turns[-1].get("interrupted") is True
    # ordinary (non-interrupted) turns carry no flag — the field is opt-in metadata
    assert all(not t.get("interrupted") for t in turns if t["role"] != "leader")


def test_converse_clears_stale_abort_on_a_new_turn(project: Project):
    """A prior ESC must not poison the next turn — converse clears the flag at
    the start of every turn."""
    orch = Orchestrator(project, _runners())  # offline path still clears the flag
    orch.abort_event.set()
    orch.converse("fresh turn")
    assert not orch.abort_event.is_set()


# ── TUI action: ESC sets the converse orchestrator's abort_event ────────────

def test_action_interrupt_leader_signals_only_when_working():
    from modulatio.tui.app import ModulatioApp

    fake = types.SimpleNamespace(
        _conv_orch=types.SimpleNamespace(abort_event=threading.Event()),
        _converse_worker_live=lambda: True,
    )
    ModulatioApp.action_interrupt_leader(fake)
    assert fake._conv_orch.abort_event.is_set()  # working → interrupt fires

    fake._conv_orch.abort_event.clear()
    fake._converse_worker_live = lambda: False
    ModulatioApp.action_interrupt_leader(fake)
    assert not fake._conv_orch.abort_event.is_set()  # idle → no-op

    # No converse orchestrator yet → must not raise.
    ModulatioApp.action_interrupt_leader(
        types.SimpleNamespace(_conv_orch=None, _converse_worker_live=lambda: True)
    )
