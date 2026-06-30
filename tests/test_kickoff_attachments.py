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


# ── iteration mode (2026-05-30): --attach pins a file to improve in place ──

def test_iteration_contract_empty_for_greenfield(project: Project):
    orch = Orchestrator(project, _capturing_runners({}))
    assert orch._pinned_files == []
    assert orch._iteration_contract_block() == ""


def test_iteration_contract_when_pinned(project: Project):
    orch = Orchestrator(project, _capturing_runners({}))
    orch._pinned_files = ["game.py"]
    block = orch._iteration_contract_block()
    assert "ITERATION" in block
    assert "`game.py`" in block
    assert "EDIT IN PLACE" in block
    assert "STAY IN THE FILE" in block          # no-scatter contract
    assert "SMALL, FOCUSED PLAN" in block        # no over-decompose / report tasks


def test_iteration_contract_reaches_decompose_prompt(project: Project):
    captured: dict[str, list[str]] = {}
    orch = Orchestrator(project, _capturing_runners(captured))
    orch._pinned_files = ["game.py"]  # simulate a pinned file
    orch._leader_decompose("improve the game", attachments=[])
    prompt = captured["leader"][0]
    assert "ITERATION — improve existing work" in prompt
    assert "`game.py`" in prompt


def test_greenfield_decompose_has_no_iteration_contract(project: Project):
    captured: dict[str, list[str]] = {}
    orch = Orchestrator(project, _capturing_runners(captured))
    orch._leader_decompose("build a new game", attachments=[])
    assert "ITERATION — improve existing work" not in captured["leader"][0]


def test_pin_attachments_copies_into_workspace(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    vault.init_project(PROJECT_CODE, "iter", "obj")
    run_id = "20260530T000000Z-iter01"
    vault.init_run(PROJECT_CODE, run_id, "obj")
    proj = Project(
        code=PROJECT_CODE, name="iter", objective="obj", leader_model="stub",
        wiki_path=str(tmp_path / PROJECT_CODE.lower()), run_id=run_id,
    )
    src = tmp_path / "game.py"
    src.write_text("import pygame\nJUMP = -12\n")
    att = build_attachment(src, kind="document")

    orch = Orchestrator(proj, _capturing_runners({}))
    orch._pin_attachments([att])

    assert orch._pinned_files == ["game.py"]
    # Durable artifacts are project-scoped + run-namespaced now (the project
    # is the persistent layer), so the pin lands under <project>/artifacts/<run>.
    pinned = (
        vault.project_dir(PROJECT_CODE) / "artifacts" / run_id / "game.py"
    )
    assert pinned.exists()
    assert pinned.read_text() == "import pygame\nJUMP = -12\n"
    # and the contract now activates
    assert "EDIT IN PLACE" in orch._iteration_contract_block()


def test_pin_attachments_noop_without_run_id(project: Project, tmp_path: Path):
    src = tmp_path / "game.py"
    src.write_text("x = 1\n")
    att = build_attachment(src, kind="document")
    orch = Orchestrator(project, _capturing_runners({}))  # project has no run_id
    orch._pin_attachments([att])
    assert orch._pinned_files == []  # nothing pinned, greenfield stays greenfield


# ── increment 3 (2026-05-30): surgical SEARCH/REPLACE patch for iteration ──

from uuid import uuid4  # noqa: E402
from modulatio.types import Task  # noqa: E402
from modulatio import orchestration as _orch  # noqa: E402


def test_parse_search_replace_blocks():
    resp = (
        "<<<<<<< SEARCH\nJUMP = -12\n=======\nJUMP = -20\n>>>>>>> REPLACE\n\n"
        "<<<<<<< SEARCH\nfoo()\n=======\nbar()\n>>>>>>> REPLACE\n"
    )
    blocks = _orch._parse_search_replace_blocks(resp)
    assert blocks == [("JUMP = -12", "JUMP = -20"), ("foo()", "bar()")]
    assert _orch._parse_search_replace_blocks("no blocks here") == []


def test_apply_search_replace_preserves_untouched():
    content = "JUMP = -12\nMOVE = 5\nATTACK = 'z'\n"
    blocks = [("JUMP = -12", "JUMP = -20")]
    new, applied, fails = _orch._apply_search_replace(content, blocks)
    assert applied == 1 and not fails
    assert "JUMP = -20" in new
    assert "MOVE = 5" in new and "ATTACK = 'z'" in new  # untouched preserved


def test_apply_search_replace_skips_nonmatching():
    content = "x = 1\n"
    new, applied, fails = _orch._apply_search_replace(content, [("NOPE", "y")])
    assert applied == 0 and new == content  # file untouched, not corrupted
    assert fails  # the miss is recorded


def test_is_iteration_target(project: Project):
    orch = Orchestrator(project, _capturing_runners({}))
    orch._pinned_files = ["game.py"]
    t_yes = Task(id="X-T-1", project_id=uuid4(), goal_id="X-G-1",
                 description="d", output_path="game.py")
    t_no = Task(id="X-T-2", project_id=uuid4(), goal_id="X-G-1",
                description="d", output_path="other.py")
    assert orch._is_iteration_target(t_yes) is True
    assert orch._is_iteration_target(t_no) is False


def _patch_orch(project):
    """Orchestrator with the heavy deps stubbed so _producer_patch's
    run→parse→apply→write glue can be exercised directly."""
    orch = Orchestrator(project, _capturing_runners({}))
    orch._prompt = lambda *a, **k: "P"
    orch._inbox_block_for = lambda *a, **k: ""
    orch._extract_producer_proposals = lambda resp, **k: resp
    orch._record_artifact_write = lambda *a, **k: None
    orch._maybe_trip_breaker = lambda *a, **k: None
    return orch


_PATCH_KW = dict(
    domain_standards="", research_context="", team_memory_context="",
    team_canvas_block="", design_intent_block="", team_state_block="",
    repo_map_block="", agent_identity="",
)


def test_producer_patch_applies_surgically(project: Project, tmp_path):
    orch = _patch_orch(project)
    f = tmp_path / "game.py"
    f.write_text("JUMP = -12\nMOVE = 5\nATTACK = 'z'\n")
    orch._run_agent_call = lambda *a, **k: (
        "<<<<<<< SEARCH\nJUMP = -12\n=======\nJUMP = -20\n>>>>>>> REPLACE"
    )
    task = Task(id="X-T-1", project_id=uuid4(), goal_id="X-G-1",
                description="raise the jump", output_path="game.py",
                artifact_kind="code")
    p, checksum, _ = orch._producer_patch(task, f, **_PATCH_KW)
    out = f.read_text()
    assert "JUMP = -20" in out                       # change applied
    assert "MOVE = 5" in out and "ATTACK = 'z'" in out  # rest PRESERVED
    assert checksum.startswith("sha256:")


def test_producer_patch_full_file_fallback(project: Project, tmp_path):
    """No SEARCH/REPLACE blocks → producer returned a full file → write it."""
    orch = _patch_orch(project)
    f = tmp_path / "game.py"
    f.write_text("old = 1\n")
    orch._run_agent_call = lambda *a, **k: "brand = 'new file'\nx = 2\n"
    task = Task(id="X-T-1", project_id=uuid4(), goal_id="X-G-1",
                description="rewrite", output_path="game.py", artifact_kind="code")
    orch._producer_patch(task, f, **_PATCH_KW)
    out = f.read_text()
    assert "brand = 'new file'" in out and "x = 2" in out
    assert "old = 1" not in out  # the full file replaced the old one


def test_producer_patch_nonmatching_leaves_file_unchanged(project: Project, tmp_path):
    """Blocks present but none match → don't write marker soup; keep file."""
    orch = _patch_orch(project)
    f = tmp_path / "game.py"
    f.write_text("real = 1\n")
    orch._run_agent_call = lambda *a, **k: (
        "<<<<<<< SEARCH\nHALLUCINATED\n=======\nx\n>>>>>>> REPLACE"
    )
    task = Task(id="X-T-1", project_id=uuid4(), goal_id="X-G-1",
                description="d", output_path="game.py", artifact_kind="code")
    orch._producer_patch(task, f, **_PATCH_KW)
    assert f.read_text() == "real = 1\n"  # unchanged, not marker soup


# ── per-task tool union (the first-brick composability) ──────────────────

import modulatio.skills as _sk  # noqa: E402


def test_task_tool_loadout_unions_required_skills(project: Project):
    """A task carrying [researcher, web-search] gets http_get + web_search,
    with neither skill bundling both — composed per task."""
    orch = Orchestrator(project, _capturing_runners({}))
    primary = _sk.load_with_metadata("researcher")  # tool_loadout [http_get]

    class FakeTask:
        required_skills = ["researcher", "web-search"]

    out = orch._task_tool_loadout(FakeTask(), primary)
    assert set(out) == {"http_get", "web_search"}
    assert out[0] == "http_get"  # primary skill's tools come first


def test_task_tool_loadout_primary_only_when_no_extras(project: Project):
    orch = Orchestrator(project, _capturing_runners({}))
    primary = _sk.load_with_metadata("researcher")

    class FakeTask:
        required_skills = ["researcher"]

    assert orch._task_tool_loadout(FakeTask(), primary) == ("http_get",)


def test_task_tool_loadout_appends_registered_skill_library_tools(project: Project):
    """Brick 1: when the registry carries the skill-library builtins, every
    producer loadout gains discover/checkout so a capability gap can self-heal
    into a checkout instead of a block."""
    from modulatio import tools as _tools
    orch = Orchestrator(
        project, _capturing_runners({}), tool_registry=_tools.build_registry()
    )
    primary = _sk.load_with_metadata("researcher")

    class FakeTask:
        required_skills = ["researcher"]

    out = orch._task_tool_loadout(FakeTask(), primary)
    assert out[0] == "http_get"  # task tools still come first
    assert {"search_skills", "load_skill", "drop_skill"} <= set(out)


def test_task_tool_loadout_skips_skill_library_tools_when_unregistered(project: Project):
    """Defensive: an empty/stub registry must NOT get skill-library tools wired
    in (they'd trip the loadout fail-fast). The bare orchestrator has {}."""
    orch = Orchestrator(project, _capturing_runners({}))  # default registry = {}
    primary = _sk.load_with_metadata("researcher")

    class FakeTask:
        required_skills = ["researcher"]

    out = orch._task_tool_loadout(FakeTask(), primary)
    assert out == ("http_get",)
    assert "load_skill" not in out
