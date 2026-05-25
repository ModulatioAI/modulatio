"""Tests for #31d — kickoff with attachments.

The Prompt-tab kickoff bar can attach docs/images alongside the
objective. They flow through ``Orchestrator.kickoff`` →
``_leader_decompose`` and land in the Leader's prompt as added
context, so goal decomposition can lean on user-supplied reference
material.

Image-vision in kickoff is deferred (Leader runs single-shot for now);
images travel as path references with a note. Document content is
quoted inline.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from modulatio import vault
from modulatio.attachments import build_attachment
from modulatio.orchestration import Orchestrator
from modulatio.types import Project


PROJECT_CODE = "KFA"


@pytest.fixture
def project(tmp_path: Path, monkeypatch) -> Project:
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    vault.init_project(PROJECT_CODE, "Kickoff-attachment fixture", "obj")
    return Project(
        code=PROJECT_CODE,
        name="Kickoff-attachment fixture",
        objective="obj",
        leader_model="stub",
        wiki_path=str(tmp_path / PROJECT_CODE.lower()),
    )


def _capturing_runners(captured: dict[str, list[str]]):
    """Stub runners that record prompts so tests can assert on what
    the Leader / Coordinator / etc. saw."""
    def _record(role: str):
        captured.setdefault(role, [])
        if role == "leader":
            def _r(prompt: str) -> str:
                captured[role].append(prompt)
                # Empty goal list is fine — we're testing the prompt only.
                return "```json\n[]\n```"
            return _r
        return lambda p: ""
    return {
        "leader": _record("leader"),
        "planner": _record("coordinator"),
        "drafter": _record("drafter"),
        "qc": _record("qc"),
        "researcher": _record("researcher"),
    }


def test_kickoff_accepts_attachments_kwarg(project: Project):
    """Just the contract: kickoff(objective, attachments=...) is a
    valid call shape."""
    captured: dict[str, list[str]] = {}
    runners = _capturing_runners(captured)
    orch = Orchestrator(project, runners)
    summary = orch.kickoff("a tiny objective", attachments=[])
    assert summary is not None


def test_kickoff_leader_prompt_quotes_document_content(
    project: Project, tmp_path: Path,
):
    """A document attached to kickoff is quoted in the Leader's
    decompose prompt — Leader can read it when planning goals."""
    spec = tmp_path / "spec.md"
    spec.write_text("BUDGET: $5K\nDEADLINE: end of month\n")

    captured: dict[str, list[str]] = {}
    runners = _capturing_runners(captured)
    orch = Orchestrator(project, runners)
    orch.kickoff(
        "produce a launch plan",
        attachments=[build_attachment(spec, kind="document")],
    )

    leader_prompt = captured["leader"][0]
    assert "BUDGET: $5K" in leader_prompt
    assert "DEADLINE: end of month" in leader_prompt
    assert "spec.md" in leader_prompt


def test_kickoff_leader_routes_to_multimodal_when_image_attached(
    project: Project, tmp_path: Path,
):
    """After vision-in-kickoff: image attachments take the multimodal
    path (chat_completion), not the runner. The single-shot 'leader'
    runner is NOT called when images are present."""
    from unittest.mock import MagicMock

    img = tmp_path / "wireframe.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n")

    captured: dict[str, list[str]] = {}
    runners = _capturing_runners(captured)

    completion_calls: list = []
    def _completion(*, model, messages, **kwargs):
        completion_calls.append({"model": model, "messages": messages})
        m = MagicMock()
        m.choices = [MagicMock()]
        m.choices[0].message.content = "```json\n[]\n```"
        return m

    orch = Orchestrator(project, runners)
    orch.kickoff(
        "implement this wireframe",
        attachments=[build_attachment(img, kind="image")],
        chat_completion=_completion,
    )
    assert len(completion_calls) == 1
    assert captured.get("leader", []) == [], (
        "single-shot leader runner should be skipped when images "
        "trigger the multimodal path"
    )


def test_kickoff_with_no_attachments_renders_neutral_marker(project: Project):
    """When no attachments, the prompt's attachments slot still renders
    something stable — empty marker so prompt diffs are clean across
    runs with/without attachments."""
    captured: dict[str, list[str]] = {}
    runners = _capturing_runners(captured)
    orch = Orchestrator(project, runners)
    orch.kickoff("plain objective")

    leader_prompt = captured["leader"][0]
    # No attached-document content should leak.
    assert "BUDGET" not in leader_prompt
    # The objective still lands.
    assert "plain objective" in leader_prompt


def test_kickoff_multimodal_user_text_inlines_documents(
    project: Project, tmp_path: Path,
):
    """Multiple docs + image: documents inline in the user message's
    text block; image arrives as an image_url content block."""
    from unittest.mock import MagicMock

    spec = tmp_path / "spec.md"
    spec.write_text("Spec content here.")
    notes = tmp_path / "notes.md"
    notes.write_text("Decisions log.")
    img = tmp_path / "diagram.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n")

    completion_calls: list = []
    def _completion(*, model, messages, **kwargs):
        completion_calls.append({"model": model, "messages": messages})
        m = MagicMock()
        m.choices = [MagicMock()]
        m.choices[0].message.content = "```json\n[]\n```"
        return m

    captured: dict[str, list[str]] = {}
    runners = _capturing_runners(captured)
    orch = Orchestrator(project, runners)
    orch.kickoff(
        "design something",
        attachments=[
            build_attachment(spec, kind="document"),
            build_attachment(notes, kind="document"),
            build_attachment(img, kind="image"),
        ],
        chat_completion=_completion,
    )

    user_content = completion_calls[0]["messages"][-1]["content"]
    text_block = next(b for b in user_content if b["type"] == "text")
    assert "Spec content here" in text_block["text"]
    assert "Decisions log" in text_block["text"]
    # Image surfaces as a content block, not in the text.
    types = [b["type"] for b in user_content]
    assert "image_url" in types
