"""Tests for the Leader's converse function — the conversational endpoint.

`Orchestrator.converse(message)` is the Leader's conversational function (the
same Leader who decomposes/verifies, talking to the operator). It reuses the
tool-loop, persists a per-project conversation thread, and falls back to a
plain acknowledgement offline (no leader chat runner).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from modulatio import vault
from modulatio.orchestration import Orchestrator
from modulatio.runners import ChatResponse
from modulatio.types import Project, ProjectState


PROJECT_CODE = "CNV"


@pytest.fixture
def project(tmp_path: Path, monkeypatch) -> Project:
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    vault.init_project(PROJECT_CODE, "converse fixture", "obj")
    return Project(
        code=PROJECT_CODE, name="converse fixture", objective="obj",
        state=ProjectState.ACTIVE, leader_model="stub",
        wiki_path=str(vault.project_dir(PROJECT_CODE)),
    )


def _runners() -> dict:
    return {
        "leader": lambda p: "", "planner": lambda p: "```json\n[]\n```",
        "drafter": lambda p: "", "qc": lambda p: "",
    }


def test_converse_offline_acknowledges_and_persists(project: Project):
    """With no leader chat runner (stub/offline), converse returns a plain
    acknowledgement and persists the thread — never crashes, never silent."""
    orch = Orchestrator(project, _runners())
    reply = orch.converse("hey Leader, can we talk without running a job?")
    assert reply.strip()
    assert "offline" in reply.lower()

    thread = orch._load_conversation()
    assert [t["role"] for t in thread] == ["operator", "leader"]
    assert orch._conversation_path().exists()


def test_converse_thread_accumulates_across_turns(project: Project):
    orch = Orchestrator(project, _runners())
    orch.converse("first message")
    orch.converse("second message")
    thread = orch._load_conversation()
    assert [t["role"] for t in thread] == [
        "operator", "leader", "operator", "leader",
    ]
    assert thread[0]["content"] == "first message"
    assert thread[2]["content"] == "second message"


def test_converse_runs_the_tool_loop_with_a_chat_runner(project: Project):
    """With a leader chat runner wired, converse runs the real tool-loop:
    the prompt carries the conversational persona + the operator's message,
    and the runner's reply comes back."""
    captured: dict = {}

    def mock_leader(*, messages, tools, tool_choice=None):
        captured["prompt"] = messages[0]["content"]
        return ChatResponse(
            content="Sure — what are we building? (mock leader)", tool_calls=(),
        )

    orch = Orchestrator(
        project, _runners(),
        chat_runners={"leader": mock_leader},
        chat_runner_models={"leader": "mock-model"},
    )
    reply = orch.converse("let's design a skill — no job yet")
    assert "mock leader" in reply
    # the persona + the operator message reached the model
    assert "Leader of this Modulatio project" in captured["prompt"]
    assert "design a skill" in captured["prompt"]
    # never says "I only run jobs" — that's the whole point
    assert "only run jobs" not in reply.lower()


def test_converse_prompt_carries_operator_context(project: Project):
    """The {operator_context} slot renders — the partnership framing the
    Leader reasons within (present vs autonomous)."""
    present = Orchestrator(project, _runners(), operator_present=True)
    prompt = present._build_converse_prompt([], "hi")
    assert "COLLABORATING" in prompt


def test_converse_prompt_carries_the_constitution(project: Project):
    """The {constitution} slot renders the Leader's values (the seed ships a
    default) into the conversational prompt."""
    orch = Orchestrator(project, _runners())
    block = orch._constitution_block()
    assert block  # the seed default is always present
    assert block in orch._build_converse_prompt([], "hi")


def test_converse_inlines_a_document(project: Project, tmp_path: Path):
    """A document attachment is inlined into the converse prompt so the Leader
    can read it (the text path keeps full tool-use)."""
    from modulatio.attachments import build_attachment

    doc = tmp_path / "notes.md"
    doc.write_text("SECRET DOC CONTENT", encoding="utf-8")
    att = build_attachment(doc, kind="document")
    orch = Orchestrator(project, _runners())
    prompt = orch._build_converse_prompt([], "read this", [att])
    assert "SECRET DOC CONTENT" in prompt


def test_converse_with_image_routes_to_multimodal(
    project: Project, tmp_path: Path, monkeypatch
):
    """An image attachment routes the turn through the multimodal path
    (carrying the image), not the text tool-loop."""
    from modulatio.attachments import build_attachment

    img = tmp_path / "pic.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
    att = build_attachment(img, kind="image")

    captured: dict = {}

    def fake_multimodal(*, prompt, attachments, chat_completion):
        captured["attachments"] = attachments
        return "I can see the image."

    orch = Orchestrator(
        project, _runners(),
        chat_runners={"leader": lambda **k: ChatResponse(
            content="unused", tool_calls=())},
    )
    monkeypatch.setattr(orch, "_run_multimodal_leader", fake_multimodal)
    reply = orch.converse("what's in this?", attachments=[att])
    assert reply == "I can see the image."
    assert captured["attachments"][0].kind == "image"


def test_pending_approvals_surface_and_decide(project: Project):
    """The Leader sees pending approvals in the prompt and resolves one via the
    decide_approval tool (the conversational-approval path)."""
    from uuid import uuid4

    from modulatio import store
    from modulatio.types import TicketPriority

    run_id = "20260602T000000Z-aaaa"
    vault.init_run(PROJECT_CODE, run_id, "scope")
    project.run_id = run_id
    t = store.create_ticket(
        project_id=uuid4(), project_code=PROJECT_CODE,
        priority=TicketPriority.CRITICAL, title="Approve the budget",
        body="continue?", approval_required=True, run_id=run_id,
    )
    orch = Orchestrator(project, _runners())

    # surfaced in the prompt
    block = orch._pending_approvals_block()
    assert t.id in block and "Approve the budget" in block
    assert "Pending approvals" in orch._build_converse_prompt([], "hi")

    # the Leader carries out the operator's decision via the tool
    tool = orch._leader_function_tools()["decide_approval"]
    out = tool.call(ticket_id=t.id, decision="approved", note="looks good")
    assert "approved" in out.lower()
    updated = store.get_ticket(PROJECT_CODE, t.id, run_id=run_id)
    assert updated is not None
    assert updated.approval_decision == "approved"
    assert updated.approval_decided_by == "operator"
    assert updated.approval_note == "looks good"

    # nothing pending now → the block disappears
    assert orch._pending_approvals_block() == ""


def test_leader_can_command_the_team(project: Project):
    """The converse loop offers the Leader his orchestrate function as tools:
    run_job (command the producer swarm) + list_job_templates."""
    offered: dict = {}

    def mock_leader(*, messages, tools, tool_choice=None):
        offered["names"] = [t["function"]["name"] for t in tools]
        return ChatResponse(content="will do", tool_calls=())

    orch = Orchestrator(
        project, _runners(),
        chat_runners={"leader": mock_leader},
        chat_runner_models={"leader": "mock-model"},
    )
    orch.converse("run the weekly brief for me")
    assert "run_job" in offered["names"]
    assert "list_job_templates" in offered["names"]

    # the tools themselves are bound to this Leader
    lft = orch._leader_function_tools()
    assert "run_job" in lft and "list_job_templates" in lft
    assert lft["run_job"].params_schema["required"] == ["objective"]
