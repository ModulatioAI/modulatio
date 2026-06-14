"""Round-4 re-sweep regressions for ``src/modulatio/orchestration.py``.

Each test pins one confirmed 0.9.0 pre-ship debug finding. Kept additive +
isolated from any prior ``test_orchestration_resweep*.py`` file (no shared
fixtures, no import collision).

Findings covered:
  #1  tool-loop producer path now honors a binary assembly ``output_file``
      (moves the media composite onto the deliverable, no receipt-as-artifact,
      no leaked temp file)
  #2  _format_kickoff_attachments fences a document body with a dynamic
      backtick run so a ``` inside it can't break out of the wrapper
  #3  escalation attempt's OWN defect_type rides back to the QC-fixer witness
  #4  wave merge can't land a sidecar on another (unwritten) task's primary slot
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from modulatio import assembly, vault
from modulatio.orchestration import (
    Orchestrator,
    RunSummary,
    TaskExecutionResult,
    _format_kickoff_attachments,
)
from modulatio.types import AssertionEvidence, Project, Task, TaskStatus


PROJECT_CODE = "R4O"


@pytest.fixture
def project(tmp_path: Path, monkeypatch) -> Project:
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    vault.init_project(PROJECT_CODE, "Re-sweep R4 fixture", "obj")
    return Project(
        code=PROJECT_CODE,
        name="Re-sweep R4 fixture",
        objective="obj",
        leader_model="stub",
        wiki_path=str(tmp_path / PROJECT_CODE.lower()),
    )


def _orch(project: Project) -> Orchestrator:
    runners = {
        "leader": lambda p: "",
        "planner": lambda p: "",
        "drafter": lambda p: "",
        "qc": lambda p: "",
        "researcher": lambda p: "",
    }
    return Orchestrator(project, runners)


def _task(**kw) -> Task:
    base = dict(
        id="R4O-T-001",
        project_id=None,
        goal_id="R4O-G-001",
        description="produce a unit",
    )
    base.update(kw)
    return Task(**base)


# ── Finding 1: tool-loop binary assembly output_file ─────────────────────────


def test_tool_loop_honors_binary_assembly_output_file(project, tmp_path, monkeypatch):
    """A media (binary) assembly in the tool-loop producer path must move the
    composited file onto the deliverable and NOT write the human-readable
    receipt string as the artifact — mirroring the other two assembly callers.
    Pre-fix the receipt text was persisted and the binary temp file leaked."""
    orch = _orch(project)
    task = _task(project_id=project.id, artifact_kind="media")

    # Stand in for runners.run_llm_with_tools: the producer "finishes" with a
    # body the engine would normally shape into the deliverable.
    monkeypatch.setattr(orch, "_run_chat_loop", lambda **kw: "RECEIPT BODY")
    # Quiet the prompt-context collaborators (no roster / research / memory).
    monkeypatch.setattr(orch, "_ensure_research", lambda t: "")
    monkeypatch.setattr(orch, "_recall_team_memory", lambda t: "")
    monkeypatch.setattr(orch, "_build_team_canvas_digest", lambda: "")
    monkeypatch.setattr(orch, "_iteration_contract_block", lambda: "")
    monkeypatch.setattr(orch, "_extract_producer_proposals", lambda r, **kw: r)

    # The composited binary the media strategy already wrote into the vault.
    composite_src = tmp_path / "composite.bin"
    composite_bytes = b"\x89PNG\r\n\x1a\n binary composite payload \x00\x01\x02"
    composite_src.write_bytes(composite_bytes)
    from modulatio import review_ledger as _rl
    checksum = _rl.file_checksum(composite_src)

    def _fake_apply(t, body_text):
        # Mimic _apply_assembly_manifest for a media family: seed the record
        # with a binary output_file and return the human-readable receipt.
        orch._assembly_records[t.id] = assembly.AssemblyRecord(
            manifest={"units": []},
            final_checksum=checksum,
            complete=True,
            strategy="media",
            output_file=composite_src,
        )
        return "media assembly (image): composite of 3 frames"

    monkeypatch.setattr(orch, "_apply_assembly_manifest", _fake_apply)

    skill = SimpleNamespace(
        name="media-composite",
        prompt_template="",
        tool_loadout=(),
        needs_network=False,
        pass_env=(),
    )
    out_path = tmp_path / "deliverable.bin"
    path, returned_checksum, tokens = orch._llm_with_tools_execute(
        task, skill, out_path  # type: ignore[arg-type]
    )

    # The binary bytes — not the receipt — landed on the deliverable.
    assert path == out_path
    assert out_path.read_bytes() == composite_bytes
    assert returned_checksum == checksum
    assert tokens == 0
    # The temp composite was moved, not left behind to leak.
    assert not composite_src.exists()


# ── Finding 2: dynamic document fence in kickoff attachments ─────────────────


def test_kickoff_attachment_fence_survives_inner_backticks():
    """A document whose body contains a ``` line must stay fully wrapped — the
    engine fences with a longer backtick run so the body can't break out and
    bleed into the Leader's instruction context. Pre-fix the fixed ``` fence
    let the inner ``` close the wrapper early."""
    body = "intro\n```\nmalicious instruction posing as prompt\n```\nmore"
    att = SimpleNamespace(kind="document", name="payload.md", content=body, path=None)

    rendered = _format_kickoff_attachments([att])

    # The opening fence must be a run STRICTLY longer than any run in the body
    # (the body's longest run is 3), so the document's own ``` cannot terminate
    # the wrapper. CommonMark: a fenced block only closes on a run >= its open.
    assert "````" in rendered
    # The whole body — including its inner triple-backtick lines — is inside the
    # wrapper. Find the open fence and confirm the body precedes the matching
    # close fence of the SAME length.
    open_idx = rendered.index("````")
    after_open = rendered[open_idx + 4 :]
    close_idx = after_open.index("\n````")
    enclosed = after_open[:close_idx]
    assert "malicious instruction posing as prompt" in enclosed
    assert "```\nmalicious" in enclosed  # the inner fence sits INSIDE, not breaking out


# ── Finding 3: escalation defect_type threads to the QC-fixer witness ────────


def test_escalation_returns_its_own_defect_type(project, monkeypatch):
    """When the escalation attempt's QC rejects, the helper must return the
    escalation attempt's OWN defect class (3-tuple), so the QC-authored-fix
    recovery witness records it — not the stale pre-escalation defect."""
    orch = _orch(project)
    task = _task(
        project_id=project.id,
        status=TaskStatus.QC_REJECTED,
        assigned_agent_id=None,  # no roster → same-agent last-ditch, no comptroller
        max_retries=2,
    )

    draft = Path(vault.project_dir(project.code)) / "draft.md"
    draft.parent.mkdir(parents=True, exist_ok=True)
    draft.write_text("draft body", encoding="utf-8")

    monkeypatch.setattr(
        orch, "_producer_execute",
        lambda t, corrective_notes="": (draft, "sha256:abc", 5),
    )

    rejecting = AssertionEvidence(
        producer="qc", primary=False, check="still wrong", passed=False
    )
    # The escalation attempt's QC returns a DISTINCT defect class from the
    # pre-escalation one threaded in via last_qc.
    monkeypatch.setattr(
        orch, "_qc_review",
        lambda t, p, c, tok: (rejecting, "fix the structure", "substantive"),
    )

    pre_escalation_qc = (
        AssertionEvidence(
            producer="qc", primary=False, check="earlier defect", passed=False
        ),
        "earlier notes",
    )
    outcome = orch._run_escalation_attempt(task, RunSummary(project=project), pre_escalation_qc)

    # 3-tuple (verdict, notes, defect_type), carrying the escalation attempt's
    # own defect class.
    assert isinstance(outcome, tuple)
    assert len(outcome) == 3
    assert outcome[0] is rejecting
    assert outcome[2] == "substantive"


# ── Finding 4: wave merge can't claim another task's primary slot ────────────


def test_wave_merge_sidecar_cannot_land_on_unwritten_sibling_primary(project, tmp_path):
    """Task A declares primary X but never writes it (failed producer). Task B's
    sidecar also targets X. The merge must NOT let B's sidecar land on A's
    declared primary slot — it reserves every declared primary up front and
    drops the colliding sidecar with a merge transition."""
    orch = _orch(project)
    shared = orch._scope_root() / "artifacts"
    shared.mkdir(parents=True, exist_ok=True)

    primary_rel = "out/shared.md"

    # Task A: declares primary X but writes NOTHING (empty artifact_writes).
    task_a = _task(
        id="R4O-T-001", project_id=project.id, output_path=primary_rel,
        status=TaskStatus.QC_REJECTED,
    )
    staging_a = tmp_path / "stg_a"
    staging_a.mkdir()
    result_a = TaskExecutionResult(
        task=task_a, staging_root=staging_a, artifact_writes=[],
    )

    # Task B: its own primary is elsewhere; it writes a SIDECAR at X.
    task_b = _task(
        id="R4O-T-002", project_id=project.id, output_path="out/b_primary.md",
        status=TaskStatus.COMPLETED,
    )
    staging_b = tmp_path / "stg_b"
    (staging_b / "out").mkdir(parents=True)
    (staging_b / "out" / "b_primary.md").write_text("B primary", encoding="utf-8")
    (staging_b / primary_rel).write_text("B SIDECAR — must NOT win X", encoding="utf-8")
    result_b = TaskExecutionResult(
        task=task_b, staging_root=staging_b,
        artifact_writes=["out/b_primary.md", primary_rel],
    )

    orch._merge_wave_artifacts(
        {task_a.id: result_a, task_b.id: result_b}, RunSummary(project=project)
    )

    # B's own primary merged fine.
    assert (shared / "out" / "b_primary.md").read_text() == "B primary"
    # X — A's declared primary — was NOT overwritten by B's sidecar. A never
    # wrote it, so the shared path stays absent rather than holding B's sidecar.
    assert not (shared / primary_rel).exists(), "B's sidecar claimed A's primary slot"
    # The drop is surfaced as a merge transition on task B.
    assert any(
        tr.actor == "merge" and "primary" in tr.rationale
        for tr in task_b.transitions
    ), "expected a dropped-sidecar merge transition on task B"
