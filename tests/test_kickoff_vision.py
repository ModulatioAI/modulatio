"""Tests for the vision-in-kickoff slice.

Closes the deferral from #31d. When a kickoff has image attachments,
Leader's decompose call now routes through the multimodal path
(litellm-format messages list with image_url content blocks) instead
of receiving image references as plain-text path mentions.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from modulatio import vault
from modulatio.attachments import build_attachment
from modulatio.orchestration import Orchestrator
from modulatio.types import Project


PROJECT_CODE = "VIK"


@pytest.fixture
def project(tmp_path: Path, monkeypatch) -> Project:
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    vault.init_project(PROJECT_CODE, "Vision-kickoff fixture", "obj")
    return Project(
        code=PROJECT_CODE,
        name="Vision-kickoff fixture",
        objective="obj",
        # Generic placeholder — Modulatio is model-agnostic. The test
        # asserts the model string round-trips through, not which model.
        leader_model="test/vision-capable-model",
        wiki_path=str(tmp_path / PROJECT_CODE.lower()),
    )


def _empty_runners():
    """Fall-back runners for non-vision slots."""
    return {
        "leader": lambda p: "should-not-fire-when-vision",
        "planner": lambda p: "```json\n[]\n```",
        "drafter": lambda p: "",
        "qc": lambda p: "",
        "researcher": lambda p: "",
    }


def _fake_completion_returning(goals_payload):
    """Build a stub LiteLLM completion fn that returns a fenced JSON
    array of goals. Captures the messages list for assertions."""
    captured: list = []
    def _fn(*, model, messages, **kwargs):
        captured.append({"model": model, "messages": messages})
        m = MagicMock()
        m.choices = [MagicMock()]
        m.choices[0].message.content = (
            f"```json\n{json.dumps(goals_payload)}\n```"
        )
        return m
    return _fn, captured


# ─── Image attachment routes Leader to multimodal ──────────────────────────


def test_kickoff_with_image_routes_leader_through_chat_completion(
    project: Project, tmp_path: Path,
):
    """An image attachment in kickoff routes Leader's decompose call
    to the multimodal chat_completion path; the runner-keyed
    'leader' callable is NOT used."""
    img = tmp_path / "wireframe.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8)

    fake_completion, captured = _fake_completion_returning([])
    runners = _empty_runners()
    orch = Orchestrator(project, runners)
    orch.kickoff(
        "design from this wireframe",
        attachments=[build_attachment(img, kind="image")],
        chat_completion=fake_completion,
    )

    assert len(captured) == 1
    assert captured[0]["model"] == "test/vision-capable-model"


def test_multimodal_user_message_includes_image_content_block(
    project: Project, tmp_path: Path,
):
    """The user message in the messages list contains an image_url
    block with a base64 data URI for the attached image."""
    img = tmp_path / "diagram.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\nimage-bytes")

    fake_completion, captured = _fake_completion_returning([])
    runners = _empty_runners()
    orch = Orchestrator(project, runners)
    orch.kickoff(
        "describe", attachments=[build_attachment(img, kind="image")],
        chat_completion=fake_completion,
    )

    messages = captured[0]["messages"]
    user = messages[-1]
    assert user["role"] == "user"
    assert isinstance(user["content"], list)
    types = [block["type"] for block in user["content"]]
    assert "image_url" in types
    img_block = next(b for b in user["content"] if b["type"] == "image_url")
    assert img_block["image_url"]["url"].startswith("data:image/png;base64,")


def test_multimodal_inlines_documents_into_text_block(
    project: Project, tmp_path: Path,
):
    """Documents continue to inline-quote into the text portion of
    the user message — they're text context, not image-style blocks."""
    img = tmp_path / "x.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n")
    doc = tmp_path / "spec.md"
    doc.write_text("BUDGET: $5K\n")

    fake_completion, captured = _fake_completion_returning([])
    runners = _empty_runners()
    orch = Orchestrator(project, runners)
    orch.kickoff(
        "build it",
        attachments=[
            build_attachment(doc, kind="document"),
            build_attachment(img, kind="image"),
        ],
        chat_completion=fake_completion,
    )

    user_content = captured[0]["messages"][-1]["content"]
    text_block = next(b for b in user_content if b["type"] == "text")
    assert "BUDGET: $5K" in text_block["text"]


def test_multimodal_response_parsed_into_goals(
    project: Project, tmp_path: Path,
):
    """JSON response from chat_completion is parsed into Goal objects
    just like the runner-path response."""
    img = tmp_path / "x.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n")

    payload = [
        {"description": "vision goal", "success_criteria": "ok",
         "evidence_required": []},
    ]
    fake_completion, _ = _fake_completion_returning(payload)
    runners = _empty_runners()
    orch = Orchestrator(project, runners)
    summary = orch.kickoff(
        "look", attachments=[build_attachment(img, kind="image")],
        chat_completion=fake_completion,
    )

    assert len(summary.goals) == 1
    assert summary.goals[0].description == "vision goal"


# ─── No image → existing single-shot path ──────────────────────────────────


def test_kickoff_with_no_image_uses_runner(project: Project):
    """Document-only or zero-attachment kickoff stays on the existing
    single-shot runner path. chat_completion is not consulted."""
    runner_called: list = []
    def _leader_runner(prompt: str) -> str:
        runner_called.append(prompt)
        return "```json\n[]\n```"

    runners = _empty_runners()
    runners["leader"] = _leader_runner

    completion_called: list = []
    def _completion(**kwargs):
        completion_called.append(kwargs)
        return None

    orch = Orchestrator(project, runners)
    orch.kickoff(
        "no images here", attachments=[],
        chat_completion=_completion,
    )
    assert len(runner_called) == 1
    assert completion_called == []


# ─── Phase 2.2: agent_models["leader"] supersedes leader_model ─────────────


def test_kickoff_vision_uses_agent_models_leader_when_set(
    tmp_path: Path, monkeypatch
):
    """Phase 2.2: when ``Project.agent_models['leader']`` is populated,
    the multimodal Leader path resolves through it instead of the
    legacy ``leader_model`` field. Verifies the new lookup chain.
    """
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    vault.init_project(PROJECT_CODE, "Vision-kickoff fixture", "obj")
    project = Project(
        code=PROJECT_CODE,
        name="Vision-kickoff fixture",
        objective="obj",
        agent_models={"leader": "newmap/agent-models-key"},
        leader_model="legacy/should-not-be-used",
        wiki_path=str(tmp_path / PROJECT_CODE.lower()),
    )
    captured: list = []
    def _completion(*, model, messages, **kwargs):
        captured.append({"model": model})
        m = MagicMock()
        m.choices = [MagicMock()]
        m.choices[0].message.content = "```json\n[]\n```"
        return m

    img_path = tmp_path / "test.png"
    img_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)

    orch = Orchestrator(project, _empty_runners())
    orch.kickoff(
        "look at this", attachments=[build_attachment(img_path, kind="image")],
        chat_completion=_completion,
    )
    assert len(captured) == 1
    assert captured[0]["model"] == "newmap/agent-models-key"


def test_kickoff_vision_falls_back_to_legacy_leader_model(
    tmp_path: Path, monkeypatch
):
    """Phase 2.2 back-compat: when ``agent_models`` doesn't have a
    'leader' key, the legacy ``leader_model`` field is still consumed.
    Existing callers that haven't migrated keep working.
    """
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    vault.init_project(PROJECT_CODE, "Vision-kickoff fixture", "obj")
    project = Project(
        code=PROJECT_CODE,
        name="Vision-kickoff fixture",
        objective="obj",
        # No agent_models — should fall through.
        leader_model="legacy/back-compat",
        wiki_path=str(tmp_path / PROJECT_CODE.lower()),
    )
    captured: list = []
    def _completion(*, model, messages, **kwargs):
        captured.append({"model": model})
        m = MagicMock()
        m.choices = [MagicMock()]
        m.choices[0].message.content = "```json\n[]\n```"
        return m

    img_path = tmp_path / "test.png"
    img_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)

    orch = Orchestrator(project, _empty_runners())
    orch.kickoff(
        "look at this", attachments=[build_attachment(img_path, kind="image")],
        chat_completion=_completion,
    )
    assert len(captured) == 1
    assert captured[0]["model"] == "legacy/back-compat"


def test_kickoff_vision_raises_when_no_leader_model_anywhere(
    tmp_path: Path, monkeypatch
):
    """Phase 2.2: both agent_models and leader_model empty → clear
    configuration error. No silent fallthrough."""
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    vault.init_project(PROJECT_CODE, "Vision-kickoff fixture", "obj")
    project = Project(
        code=PROJECT_CODE,
        name="Vision-kickoff fixture",
        objective="obj",
        # Both unset — peer-team-shaped project that omits hierarchy.
        wiki_path=str(tmp_path / PROJECT_CODE.lower()),
    )
    img_path = tmp_path / "test.png"
    img_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)

    orch = Orchestrator(project, _empty_runners())
    with pytest.raises(RuntimeError, match="multimodal Leader"):
        orch.kickoff(
            "look at this", attachments=[build_attachment(img_path, kind="image")],
            chat_completion=lambda **_: None,
        )


def test_project_constructible_without_leader_model(tmp_path):
    """Phase 2.2: leader_model is now Optional[str]. A peer-team-style
    project with no hierarchy can be constructed without it."""
    p = Project(
        code="pt1",
        name="peer team test",
        objective="obj",
        agent_models={"engineer": "x", "writer": "y"},
        wiki_path=str(tmp_path / "pt1"),
    )
    assert p.leader_model is None
    assert p.agent_models == {"engineer": "x", "writer": "y"}
