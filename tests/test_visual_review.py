"""Visual review — the judgment seats can be DELIVERED AN IMAGE.

QC review and Leader goal-verify were text-only: a binary image artifact was
an environmental fail, an engine-composited media file a checksum-only pass
("Perceptual content NOT machine-verifiable"), and every SVG stance judgment
was coordinate-inference from markup. When the seat's model has vision, the
review now carries the artifact's image as a content block; without vision
(or without a renderer, for SVG) behavior is byte-identical to before.
"""
from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from modulatio import roster, vault
from modulatio.orchestration import Orchestrator
from modulatio.types import Project, ProjectState, Task, TaskStatus

PROJECT_CODE = "VIS"

#: Minimal valid-magic PNG bytes (the house idiom for image fixtures).
_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64

_PASS_JSON = '```json\n{"check": "looks right", "passed": true}\n```'


@pytest.fixture
def project(tmp_path: Path, monkeypatch) -> Project:
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    vault.init_project(PROJECT_CODE, "visual fixture", "obj")
    return Project(
        code=PROJECT_CODE, name="visual fixture", objective="obj",
        state=ProjectState.ACTIVE, leader_model="stub",
        wiki_path=str(vault.project_dir(PROJECT_CODE)),
    )


def _task(**overrides) -> Task:
    fields = dict(
        id="VIS-T-001", project_id=uuid4(), goal_id="VIS-G-001",
        description="an image deliverable",
    )
    fields.update(overrides)
    return Task(**fields)


def _orch(project, **kw) -> Orchestrator:
    runners = {
        "leader": lambda p: "", "planner": lambda p: "```json\n[]\n```",
        "drafter": lambda p: "", "qc": lambda p: _PASS_JSON,
    }
    return Orchestrator(project, runners, **kw)


# ── the vision gate ─────────────────────────────────────────────────────


def test_model_has_vision_preset_and_raw(monkeypatch):
    from modulatio import model_presets

    presets = {
        "seeing": {"model": "openai/some-model", "capability_tags": ["vision"]},
        "blind": {"model": "openai/other-model", "capability_tags": ["fast"]},
    }
    monkeypatch.setattr(model_presets, "get_preset", lambda k: presets.get(k))
    assert roster.model_has_vision("seeing") is True
    assert roster.model_has_vision("blind") is False
    assert roster.model_has_vision("raw/litellm-id") is False  # no preset
    assert roster.model_has_vision(None) is False


# ── the shared multimodal dispatch ──────────────────────────────────────


def test_run_multimodal_call_resolves_preset(project, tmp_path, monkeypatch):
    from modulatio import runners as _runners
    from modulatio.attachments import build_attachment

    img = tmp_path / "art.png"
    img.write_bytes(_PNG)
    att = build_attachment(img, kind="image")
    monkeypatch.setattr(
        _runners, "_resolve_model_call_args",
        lambda key: (f"resolved/{key}", {"api_key": "k"}),
    )
    captured: dict = {}

    def fake_completion(*, model, messages, **kwargs):
        captured["model"] = model
        captured["messages"] = messages
        from types import SimpleNamespace
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=_PASS_JSON))])

    orch = _orch(project)
    out = orch._run_multimodal_call(
        model_id="my-preset", prompt="judge this", attachments=[att],
        chat_completion=fake_completion, budget_role="qc",
        runner_role="qc", agent_id="qc",
    )
    assert captured["model"] == "resolved/my-preset"  # preset key resolved
    assert out == _PASS_JSON
    blocks = captured["messages"][0]["content"]
    assert blocks[0]["type"] == "text"
    assert blocks[1]["type"] == "image_url"  # the image rode as a block


# ── QC review in image mode ─────────────────────────────────────────────


def test_qc_review_raster_with_vision_reviews_pixels(project, monkeypatch):
    orch = _orch(project)
    draft = orch._resolve_draft_path(_task(output_path="art.png"))
    draft = draft.parent / "art.png"
    draft.parent.mkdir(parents=True, exist_ok=True)
    draft.write_bytes(_PNG)

    monkeypatch.setattr(orch, "_qc_vision_model", lambda a: "seeing-preset")
    seen: dict = {}

    def fake_mm(*, model_id, prompt, attachments, **kw):
        seen.update(model_id=model_id, prompt=prompt, attachments=attachments)
        return _PASS_JSON

    monkeypatch.setattr(orch, "_run_multimodal_call", fake_mm)
    verdict, notes, defect = orch._qc_review(
        _task(output_path="art.png"), draft, "sha")
    assert verdict.passed is True                       # reviewed, not environmental
    assert seen["model_id"] == "seeing-preset"
    assert seen["attachments"][0].path == draft         # the artifact's own pixels
    assert "Visual evidence" in seen["prompt"]          # the image note rode


def test_qc_review_raster_without_vision_unchanged(project, monkeypatch):
    orch = _orch(project)
    draft = orch._resolve_draft_path(_task()).parent / "art.png"
    draft.parent.mkdir(parents=True, exist_ok=True)
    draft.write_bytes(_PNG)

    monkeypatch.setattr(orch, "_qc_vision_model", lambda a: None)
    called: list = []
    monkeypatch.setattr(
        orch, "_run_multimodal_call",
        lambda **kw: called.append(1) or _PASS_JSON)
    verdict, notes, defect = orch._qc_review(_task(), draft, "sha")
    assert verdict.passed is False and defect == "environmental"  # as today
    assert called == []                                 # multimodal never used


def test_qc_review_svg_attaches_render(project, tmp_path, monkeypatch):
    from modulatio import multimodal

    orch = _orch(project)
    draft = orch._resolve_draft_path(_task()).parent / "hero.svg"
    draft.parent.mkdir(parents=True, exist_ok=True)
    draft.write_text("<svg xmlns='http://www.w3.org/2000/svg'/>")

    render = tmp_path / "hero-render.png"
    render.write_bytes(_PNG)
    monkeypatch.setattr(
        multimodal, "render_svg_to_png", lambda p, d, timeout=10.0: render)
    monkeypatch.setattr(orch, "_qc_vision_model", lambda a: "seeing-preset")
    seen: dict = {}

    def fake_mm(*, prompt, attachments, **kw):
        seen.update(prompt=prompt, attachments=attachments)
        return _PASS_JSON

    monkeypatch.setattr(orch, "_run_multimodal_call", fake_mm)
    verdict, _n, _d = orch._qc_review(_task(), draft, "sha")
    assert verdict.passed is True
    assert "<svg" in seen["prompt"]                     # markup still reviewed
    assert seen["attachments"][0].path == render        # AND the render attached


def test_qc_review_svg_no_renderer_keeps_text_path(project, monkeypatch):
    from modulatio import multimodal

    orch = _orch(project)
    draft = orch._resolve_draft_path(_task()).parent / "hero.svg"
    draft.parent.mkdir(parents=True, exist_ok=True)
    draft.write_text("<svg xmlns='http://www.w3.org/2000/svg'/>")

    monkeypatch.setattr(
        multimodal, "render_svg_to_png", lambda p, d, timeout=10.0: None)
    monkeypatch.setattr(orch, "_qc_vision_model", lambda a: "seeing-preset")
    called: list = []
    monkeypatch.setattr(
        orch, "_run_multimodal_call",
        lambda **kw: called.append(1) or _PASS_JSON)
    verdict, _n, _d = orch._qc_review(_task(), draft, "sha")
    assert verdict.passed is True                       # normal text review ran
    assert called == []                                 # no image → no multimodal


# ── the renderer hook ───────────────────────────────────────────────────


def test_render_svg_to_png_degrades(tmp_path, monkeypatch):
    import shutil

    from modulatio.multimodal import render_svg_to_png

    svg = tmp_path / "x.svg"
    svg.write_text("<svg/>")
    monkeypatch.setattr(shutil, "which", lambda name: None)
    assert render_svg_to_png(svg, tmp_path) is None     # no renderer → None

    fake = tmp_path / "fake-renderer"
    fake.write_text("#!/bin/sh\nexit 1\n")
    fake.chmod(0o755)
    monkeypatch.setattr(shutil, "which", lambda name: str(fake))
    assert render_svg_to_png(svg, tmp_path) is None     # failing renderer → None


def test_render_temp_cleaned_after_review(project, monkeypatch):
    from modulatio import multimodal

    orch = _orch(project)
    draft = orch._resolve_draft_path(_task()).parent / "hero.svg"
    draft.parent.mkdir(parents=True, exist_ok=True)
    draft.write_text("<svg xmlns='http://www.w3.org/2000/svg'/>")

    made: list = []

    def fake_render(p, d, timeout=10.0):
        out = Path(d) / "hero.png"
        out.write_bytes(_PNG)
        made.append(out)
        return out

    monkeypatch.setattr(multimodal, "render_svg_to_png", fake_render)
    monkeypatch.setattr(orch, "_qc_vision_model", lambda a: "seeing-preset")
    monkeypatch.setattr(orch, "_run_multimodal_call", lambda **kw: _PASS_JSON)
    orch._qc_review(_task(), draft, "sha")
    assert made and not made[0].exists()                # temp released
    scope = orch._scope_root()
    assert not list(scope.rglob("hero.png"))            # nothing in the run tree


# ── Leader-verify in image mode ─────────────────────────────────────────


def _completed_goal_with_png(orch, project):
    from modulatio.types import EvidenceClass, Goal

    root = orch._shared_artifacts_root()
    root.mkdir(parents=True, exist_ok=True)
    (root / "art.png").write_bytes(_PNG)
    goal = Goal(
        id="VIS-G-001", project_id=uuid4(), description="g",
        success_criteria="s", evidence_class=EvidenceClass.OBJECTIVE,
    )
    task = _task(output_path="art.png", status=TaskStatus.COMPLETED)
    return goal, [task]


def test_leader_verify_image_dispatches_multimodal(project, monkeypatch):
    from modulatio.orchestration import RunSummary

    orch = _orch(project)
    project.agent_models = {"leader": "leader-preset"}
    monkeypatch.setattr(roster, "model_has_vision", lambda k: k == "leader-preset")
    seen: dict = {}

    def fake_mm(*, prompt, attachments, chat_completion, budget_role):
        seen.update(budget_role=budget_role, attachments=attachments)
        return '```json\n{"verdict": "satisfied", "rationale": "looks right"}\n```'

    monkeypatch.setattr(orch, "_run_multimodal_leader", fake_mm)
    goal, tasks = _completed_goal_with_png(orch, project)
    orch._leader_verify_goal(goal, tasks, RunSummary(project=project))
    assert seen["budget_role"] == "leader-reflect"
    assert seen["attachments"][0].path.name == "art.png"


def test_leader_verify_without_vision_stays_single_shot(project, monkeypatch):
    from modulatio.orchestration import RunSummary

    orch = _orch(project)
    project.agent_models = {"leader": "leader-preset"}
    monkeypatch.setattr(roster, "model_has_vision", lambda k: False)
    called: list = []
    monkeypatch.setattr(
        orch, "_run_multimodal_leader", lambda **kw: called.append(1) or "")
    ran: dict = {}

    def fake_run(role, p, **kw):
        ran["prompt"] = p
        return '```json\n{"verdict": "satisfied", "rationale": "ok"}\n```'

    monkeypatch.setattr(orch, "_run", fake_run)
    goal, tasks = _completed_goal_with_png(orch, project)
    orch._leader_verify_goal(goal, tasks, RunSummary(project=project))
    assert called == []                                 # no multimodal
    assert "art.png" in ran["prompt"]                   # text path as today


def test_leader_verify_caps_images_at_four(project, monkeypatch):
    from modulatio.orchestration import RunSummary
    from modulatio.types import EvidenceClass, Goal

    orch = _orch(project)
    project.agent_models = {"leader": "leader-preset"}
    monkeypatch.setattr(roster, "model_has_vision", lambda k: True)
    root = orch._shared_artifacts_root()
    root.mkdir(parents=True, exist_ok=True)
    tasks = []
    for i in range(6):
        (root / f"art{i}.png").write_bytes(_PNG)
        tasks.append(_task(
            id=f"VIS-T-{i:03d}", output_path=f"art{i}.png",
            status=TaskStatus.COMPLETED))
    goal = Goal(id="VIS-G-001", project_id=uuid4(), description="g",
                success_criteria="s", evidence_class=EvidenceClass.OBJECTIVE)
    seen: dict = {}

    def fake_mm(*, prompt, attachments, chat_completion, budget_role):
        seen["attachments"] = attachments
        return '```json\n{"verdict": "satisfied", "rationale": "ok"}\n```'

    monkeypatch.setattr(orch, "_run_multimodal_leader", fake_mm)
    orch._leader_verify_goal(goal, tasks, RunSummary(project=project))
    assert len(seen["attachments"]) == 4


# ── media composite perceptual upgrade ──────────────────────────────────


def _media_orch_with_record(project, monkeypatch, *, mechanical_passes: bool):
    from types import SimpleNamespace

    from modulatio import review_ledger
    from modulatio.orchestration import AssertionEvidence

    orch = _orch(project)
    task = _task(output_path="art.png")
    draft = orch._resolve_draft_path(task).parent / "art.png"
    draft.parent.mkdir(parents=True, exist_ok=True)
    draft.write_bytes(_PNG)
    orch._assembly_records[task.id] = SimpleNamespace(
        strategy="media", digest=None, final_checksum="c",
    )
    monkeypatch.setattr(
        review_ledger, "verify_assembly",
        lambda *a, **k: (False, "media not cheap-pass eligible", "none"),
    )
    mech = AssertionEvidence(
        producer="qc", primary=True,
        check="media composite checks", passed=mechanical_passes,
    )
    monkeypatch.setattr(
        orch, "_qc_media_verdict",
        lambda *a, **k: (mech, "mechanical notes", None if mechanical_passes else "environmental"),
    )
    return orch, task, draft


def test_qc_media_perceptual_review_when_vision(project, monkeypatch):
    orch, task, draft = _media_orch_with_record(
        project, monkeypatch, mechanical_passes=True)
    monkeypatch.setattr(orch, "_qc_vision_model", lambda a: "seeing-preset")
    seen: dict = {}

    def fake_mm(**kw):
        seen.update(kw)
        return '```json\n{"check": "ugly composition", "passed": false, "defect_type": "substantive"}\n```'

    monkeypatch.setattr(orch, "_run_multimodal_call", fake_mm)
    verdict, notes, defect = orch._qc_review(task, draft, "sha")
    assert seen["attachments"][0].path == draft   # perceptual review ran
    assert verdict.passed is False                # and its verdict is HONORED
    assert defect == "substantive"                # not the disclaimer pass


def test_qc_media_mechanical_fail_stays_terminal(project, monkeypatch):
    orch, task, draft = _media_orch_with_record(
        project, monkeypatch, mechanical_passes=False)
    monkeypatch.setattr(orch, "_qc_vision_model", lambda a: "seeing-preset")
    called: list = []
    monkeypatch.setattr(
        orch, "_run_multimodal_call",
        lambda **kw: called.append(1) or _PASS_JSON)
    verdict, notes, defect = orch._qc_review(task, draft, "sha")
    assert verdict.passed is False and called == []  # terminal, no LLM call
