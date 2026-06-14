"""Regression tests for the Opus R2 full-debug MEDIUM/LOW audit fixes in
``orchestration.py``.

Each test fails before its fix and passes after:

- _load_conversation must not strict-UTF-8-wedge the converse surface on a
  single bad byte in the durable log.
- revise/edit producer mode must not crash on a BINARY draft (UnicodeDecodeError
  masked as a confusing BLOCKED) — it falls back to generate.
- the wave merge must not let a reused tool-call id silently overwrite another
  worker's raw tool-result in the durable audit tree.
- the QC-as-fixer rescue must re-assert the P5 declared-format gate (no fake
  binary shipped as a clean completion) and must refuse a single-file rescue of
  a multi-file draft.
- the QC patch must not mangle an off-contract ``=== FILE: ===`` body via
  _extract_code_from_prose.
- the vision CONVERSE prompt must correct the "future slice" wording (the image
  IS attached as a content block on this surface).
- the goal redo-fingerprint dict must be pruned when a goal terminalizes.
- a wave where every ready task is DEFERRED_CAPACITY with no slot freeing must
  BLOCK those tasks visibly rather than orphan them PENDING.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from modulatio import store, vault
from modulatio.orchestration import (
    Orchestrator,
    RunSummary,
    TaskExecutionResult,
    _extract_code_from_prose,
)
from modulatio.types import (
    AssertionEvidence,
    Goal,
    GoalStatus,
    Project,
    ProjectState,
    Task,
    TaskStatus,
)


PROJECT_CODE = "R2A"


@pytest.fixture
def project(tmp_path: Path, monkeypatch) -> Project:
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    vault.init_project(PROJECT_CODE, "r2-audit fixture", "obj")
    return Project(
        code=PROJECT_CODE, name="r2-audit fixture", objective="obj",
        state=ProjectState.ACTIVE, leader_model="stub",
        run_id="run-r2",
        wiki_path=str(vault.project_dir(PROJECT_CODE)),
    )


def _runners() -> dict:
    return {
        "leader": lambda p: "", "planner": lambda p: "```json\n[]\n```",
        "drafter": lambda p: "", "qc": lambda p: "",
    }


def _orch(project: Project) -> Orchestrator:
    return Orchestrator(project, _runners())


def _make_task(pid, tid: str, **kw) -> Task:
    return Task(
        id=tid, project_id=pid, goal_id="R2A-G-001",
        description=f"task {tid}", **kw,
    )


# ── _load_conversation: a bad byte must not wedge converse ────────────────────
def test_load_conversation_survives_non_utf8_byte(project: Project):
    """A non-UTF-8 byte in the durable conversation log must degrade to a
    best-effort thread, not raise UnicodeDecodeError on every converse turn."""
    orch = _orch(project)
    path = orch._conversation_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    good = json.dumps({"role": "operator", "content": "hi", "ts": "t"})
    # One valid line, one line with an invalid UTF-8 byte (0xff).
    path.write_bytes(good.encode("utf-8") + b"\n\xff\xfe garbage\n")

    # Must not raise; the valid line still surfaces, the bad one is dropped.
    turns = orch._load_conversation()
    assert any(t.get("content") == "hi" for t in turns)


def test_load_conversation_missing_file_returns_empty(project: Project):
    orch = _orch(project)
    assert orch._load_conversation() == []


# ── revise/edit producer mode: binary draft falls back, no crash ──────────────
def test_producer_revise_binary_draft_falls_back_to_generate(project: Project):
    """A BINARY draft routed to producer_mode='revise' (via the leader-redo /
    auto-resume lanes, since _draft_is_multifile is False for binary) must not
    raise UnicodeDecodeError — it falls through to the generate branch."""
    orch = _orch(project)
    pid = project.id
    task = _make_task(pid, "R2A-T-001", artifact_kind="document",
                      output_path="report.pdf")
    task.producer_mode = "revise"

    # Lay a binary "draft" at the task's output path so path.exists() is True
    # but read_text() would raise UnicodeDecodeError.
    artifacts = orch._artifacts_root()
    draft = artifacts / "report.pdf"
    draft.parent.mkdir(parents=True, exist_ok=True)
    draft.write_bytes(b"%PDF-1.4\x00\x01\xff\xfebinary-bytes")

    captured: dict = {}

    def _fake_run(agent_id, role, prompt, **kw):
        # If we reach the model call without crashing on read_text, the fix
        # held. Capture the prompt so we can assert it's the generate path
        # (no "existing draft" wording from the revise prompt).
        captured["prompt"] = prompt
        return "regenerated body of sufficient length " * 10

    orch._run_agent_call = _fake_run  # type: ignore[assignment]

    # Must not raise UnicodeDecodeError.
    out_path, _checksum, _tokens = orch._producer_execute(task)
    assert "prompt" in captured, "the producer must reach the model call"
    assert out_path is not None


# ── wave merge: reused tool-call id must not clobber the audit record ─────────
def test_merge_wave_namespaces_colliding_tool_results(project: Project):
    """Two workers each stage tool_calls/call_1.txt with DIFFERENT content. The
    merge must keep both (namespacing the loser under tool_calls/<task>/) rather
    than letting the later task silently overwrite the earlier audit record."""
    orch = _orch(project)
    pid = project.id

    shared = orch._scope_root() / "artifacts"
    shared.mkdir(parents=True, exist_ok=True)

    done: dict[str, TaskExecutionResult] = {}
    for tid, content in (("R2A-T-AAA", b"result-A"), ("R2A-T-BBB", b"result-B")):
        task = _make_task(pid, tid, output_path=f"{tid}.md")
        staging = orch._scope_root() / ".staging" / tid
        (staging / "tool_calls").mkdir(parents=True, exist_ok=True)
        # The colliding reused-id raw tool-result file.
        (staging / "tool_calls" / "call_1.txt").write_bytes(content)
        # A primary output so the merge has a declared artifact too.
        (staging / f"{tid}.md").write_text("body")
        done[tid] = TaskExecutionResult(
            task=task, staging_root=staging,
            artifact_writes=[f"{tid}.md"],
        )

    orch._merge_wave_artifacts(done, RunSummary(project=project))

    tc = shared / "tool_calls"
    # The first task claims tool_calls/call_1.txt; the second is namespaced.
    bodies = {p.read_bytes() for p in tc.rglob("call_1.txt")}
    assert b"result-A" in bodies
    assert b"result-B" in bodies, (
        "the colliding tool-result must be preserved, not overwritten"
    )


def test_merge_wave_identical_tool_result_not_duplicated(project: Project):
    """When two workers stage byte-identical tool-result files under the same
    name, the merge must NOT create a spurious namespaced copy."""
    orch = _orch(project)
    pid = project.id
    shared = orch._scope_root() / "artifacts"
    shared.mkdir(parents=True, exist_ok=True)

    done: dict[str, TaskExecutionResult] = {}
    for tid in ("R2A-T-CCC", "R2A-T-DDD"):
        task = _make_task(pid, tid, output_path=f"{tid}.md")
        staging = orch._scope_root() / ".staging" / tid
        (staging / "tool_calls").mkdir(parents=True, exist_ok=True)
        (staging / "tool_calls" / "call_0.txt").write_bytes(b"same")
        (staging / f"{tid}.md").write_text("body")
        done[tid] = TaskExecutionResult(
            task=task, staging_root=staging, artifact_writes=[f"{tid}.md"],
        )

    orch._merge_wave_artifacts(done, RunSummary(project=project))
    copies = list((shared / "tool_calls").rglob("call_0.txt"))
    assert len(copies) == 1, "identical results must not be needlessly duplicated"


# ── QC-patch: P5 declared-format gate re-asserted on the rescue path ──────────
def test_qc_patch_rejects_text_under_binary_extension(project: Project):
    """_qc_patch_artifact writes TEXT; on a declared-binary output_path it must
    raise (P5 declared-format integrity) so the caller never completes a fake
    binary as a clean qc_authored_fix."""
    orch = _orch(project)
    pid = project.id
    task = _make_task(pid, "R2A-T-PDF", artifact_kind="document",
                      output_path="out.pdf")
    artifacts = orch._artifacts_root()
    draft = artifacts / "out.pdf"
    draft.parent.mkdir(parents=True, exist_ok=True)
    draft.write_text("storm partial text")

    # QC returns plain text — which is NOT a real PDF.
    orch._run_agent_call = (  # type: ignore[assignment]
        lambda agent_id, role, prompt, **kw: "Here is the fixed text body."
    )

    with pytest.raises(ValueError, match="declared-format"):
        orch._qc_patch_artifact(task, draft, "defects", "storm partial text")


def test_qc_patch_allows_plain_text_extension(project: Project):
    """A text artifact (.md) patched to text must pass the format gate."""
    orch = _orch(project)
    pid = project.id
    task = _make_task(pid, "R2A-T-MD", artifact_kind="document",
                      output_path="out.md")
    artifacts = orch._artifacts_root()
    draft = artifacts / "out.md"
    draft.parent.mkdir(parents=True, exist_ok=True)
    draft.write_text("old body")

    orch._run_agent_call = (  # type: ignore[assignment]
        lambda agent_id, role, prompt, **kw: "patched markdown body"
    )
    out = orch._qc_patch_artifact(task, draft, "defects", "old body")
    assert "patched" in out


# ── QC rescue: refuse single-file rescue of a multi-file draft ────────────────
def test_qc_fix_forward_refuses_multifile_draft(project: Project, monkeypatch):
    """A multi-file draft (=== FILE: === headers) must NOT be rescued by the
    single-file QC patch — the rescue would patch only the primary and stamp
    COMPLETED while a sibling defect survives. It falls through (returns False)."""
    orch = _orch(project)
    pid = project.id
    task = _make_task(pid, "R2A-T-MF", artifact_kind="code",
                      output_path="src/main.py")
    artifacts = orch._artifacts_root()
    draft = artifacts / "src" / "main.py"
    draft.parent.mkdir(parents=True, exist_ok=True)
    # A concatenated multi-file body carrying FILE headers (what
    # _draft_is_multifile detects).
    draft.write_text(
        "=== FILE: src/main.py ===\nprint('hi')\n"
        "=== FILE: src/util.py ===\nx = 1\n"
    )

    patched_called: list = []
    orch._qc_patch_artifact = (  # type: ignore[assignment]
        lambda *a, **k: patched_called.append(True) or "x"
    )

    verdict = AssertionEvidence(producer="qc", primary=True,
                                check="sibling defect", passed=False)
    rescued = orch._attempt_qc_fix_forward(
        task, draft, (verdict, "fix util.py"), RunSummary(project=project),
    )
    assert rescued is False, "a multi-file draft must not be single-file rescued"
    assert not patched_called, "the single-file patch must not run"
    assert task.status != TaskStatus.COMPLETED


# ── _extract_code_from_prose: skip FILE-header bodies ─────────────────────────
def test_extract_code_from_prose_unchanged_for_plain_prose():
    """Sanity: the helper still extracts the largest fenced block from prose."""
    text = (
        "Here is some explanatory prose before the code that is reasonably long "
        "enough to dominate.\n\n```python\nprint('hello world')\n```\n\nMore prose.\n"
    )
    assert _extract_code_from_prose(text) == "print('hello world')"


def test_qc_patch_preserves_file_header_body(project: Project):
    """If the QC patch returns an (off-contract) concatenated multi-file body
    with FILE headers, _qc_patch_artifact must NOT run prose-extraction (which
    would drop all but the largest fenced block) — the headers survive."""
    orch = _orch(project)
    pid = project.id
    task = _make_task(pid, "R2A-T-HDR", artifact_kind="code",
                      output_path="a.py")
    artifacts = orch._artifacts_root()
    draft = artifacts / "a.py"
    draft.parent.mkdir(parents=True, exist_ok=True)
    draft.write_text("old")

    multi = (
        "=== FILE: a.py ===\n```python\nprint('a')\n```\n"
        "=== FILE: b.py ===\n```python\nprint('b')\n```\n"
    )
    orch._run_agent_call = (  # type: ignore[assignment]
        lambda agent_id, role, prompt, **kw: multi
    )
    out = orch._qc_patch_artifact(task, draft, "defects", "old")
    assert "=== FILE: a.py ===" in out
    assert "=== FILE: b.py ===" in out, "FILE headers must not be mangled away"


# ── vision converse prompt: correct the "future slice" wording ────────────────
def test_converse_prompt_corrects_future_slice_for_image(project: Project):
    """When a converse turn carries an image, the built prompt must include the
    corrective note (the image IS attached as a content block here), not just
    the kickoff 'future slice' wording."""
    orch = _orch(project)
    image = SimpleNamespace(kind="image", name="pic.png", path="pic.png",
                            content=None)
    prompt = orch._build_converse_prompt([], "look", [image])
    assert "content blocks below" in prompt
    assert "examine them" in prompt


def test_converse_prompt_no_image_note_for_document(project: Project):
    """A document-only converse turn must NOT get the image corrective note."""
    orch = _orch(project)
    doc = SimpleNamespace(kind="document", name="d.md", path="d.md",
                          content="hi")
    prompt = orch._build_converse_prompt([], "read", [doc])
    assert "content blocks below — examine them" not in prompt


# ── goal redo-fingerprints pruned on terminal ─────────────────────────────────
def test_redo_fingerprints_pruned_when_goal_terminalizes(project: Project):
    """A goal's redo loop-breaker fingerprint must be dropped once the goal
    reaches a terminal (COMPLETED) verdict so the per-run dict doesn't grow
    stale entries for every goal that ever redid."""
    orch = _orch(project)
    pid = project.id
    goal = Goal(
        id="R2A-G-FP", project_id=pid, description="g",
        success_criteria="sc", status=GoalStatus.IN_PROGRESS,
    )
    store.save_goal(project.code, goal, run_id=project.run_id)
    # Seed a fingerprint as if a redo was dispatched.
    orch._goal_redo_fingerprints[goal.id] = "fp-abc"

    task = _make_task(pid, "R2A-T-FP", deliverable=True)
    task.status = TaskStatus.COMPLETED

    # Drive the leader-verify completion with a 'satisfied' verdict — the goal
    # terminalizes and the fingerprint must be pruned. _leader_verify_goal calls
    # self._run (simple-shot, no chat runner wired) then _extract_json.
    orch._run = (  # type: ignore[assignment]
        lambda *a, **k: "```json\n" + json.dumps({
            "verdict": "satisfied", "rationale": "ok",
            "report_body": "## R\n", "recommendations": [],
        }) + "\n```"
    )
    orch._leader_verify_goal(goal, [task], RunSummary(project=project))

    assert goal.status == GoalStatus.COMPLETED

    assert goal.id not in orch._goal_redo_fingerprints, (
        "a terminalized goal must not leak its redo fingerprint"
    )


# ── wave loop: DEFERRED_CAPACITY-only wave blocks visibly, not orphan ─────────
def test_wave_capacity_starved_blocks_tasks(project: Project, monkeypatch):
    """When every ready task is DEFERRED_CAPACITY and no slot will free (a
    pathological roster), the wave loop must BLOCK those tasks with a capacity
    rationale and surface an error — not break leaving them PENDING-orphaned."""
    from modulatio import dispatch

    orch = _orch(project)
    pid = project.id
    goal = Goal(
        id="R2A-G-CAP", project_id=pid, description="g",
        success_criteria="sc", status=GoalStatus.IN_PROGRESS,
    )
    task = _make_task(pid, "R2A-T-CAP", required_skills=["writing"])
    task.status = TaskStatus.PENDING
    store.save_task(project.code, task, run_id=project.run_id)

    # Force schedule_wave to return an empty assignment set (capacity starved):
    # the only producers carry cap 0, so the skill-routed task is deferred and
    # never assigned.
    monkeypatch.setattr(
        dispatch, "schedule_wave",
        lambda *a, **k: SimpleNamespace(assignments={}),
    )

    task_map = {task.id: task}
    summary = RunSummary(project=project)
    # Must terminate (no infinite spin) and BLOCK the starved task.
    orch._run_task_waves(goal, [task], summary, task_map)

    assert task.status == TaskStatus.BLOCKED, (
        "a capacity-starved ready task must be BLOCKED, not left PENDING"
    )
    assert any("capacity" in e for e in summary.errors), (
        "the saturated roster must surface a visible error"
    )
