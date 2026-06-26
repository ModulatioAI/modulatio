"""Tests for the Leader-artifact-visibility fix.

Surfaced 2026-04-28 in the WLT real-model run: T-004 produced a
guide artifact at `artifacts/WLT_crypto_wallets_guide.md`, QC passed
it, but the Leader's goal-verify prompt ONLY scanned
`artifacts/drafts/<task-id>.md` and missed the file entirely. The
Leader returned a 'disappointed' verdict claiming the artifact was
never produced.

The fix:
  1. Leader's scan respects ``task.output_path`` (relative to the
     project's artifacts/ directory) before falling back to the
     drafts/ convention.
  2. The verify prompt includes a snippet of each artifact's actual
     content so the Leader has something concrete to evaluate.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from modulatio import store, vault
from modulatio.orchestration import Orchestrator, RunSummary
from modulatio.types import (
    Goal, GoalStatus, Project, Task, TaskStatus,
)


PROJECT_CODE = "LVA"


@pytest.fixture
def project(tmp_path: Path, monkeypatch) -> Project:
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    vault.init_project(PROJECT_CODE, "Leader-verify-artifact fixture", "obj")
    return Project(
        code=PROJECT_CODE,
        name="Leader-verify-artifact fixture",
        objective="obj",
        leader_model="stub",
        wiki_path=str(tmp_path / PROJECT_CODE.lower()),
    )


def _capturing_orch(project: Project):
    """Orchestrator with a leader runner that captures the verify
    prompt for assertion."""
    captured: list[str] = []
    def _leader(prompt: str) -> str:
        captured.append(prompt)
        return "```json\n" + json.dumps({
            "verdict": "satisfied",
            "rationale": "ok",
            "report": "ok",
        }) + "\n```"
    runners = {
        "leader": _leader,
        "planner": lambda p: "```json\n[]\n```",
        "drafter": lambda p: "",
        "qc": lambda p: "",
        "researcher": lambda p: "",
    }
    return Orchestrator(project, runners), captured


def test_leader_verify_finds_artifact_at_task_output_path(project: Project, tmp_path: Path):
    """A completed task with ``output_path='guide.md'`` produces a file
    at ``<artifacts>/guide.md``. Leader's verify scan must find it
    there, not require a drafts/<task-id>.md naming."""
    artifacts_root = tmp_path / PROJECT_CODE.lower() / "artifacts"
    artifacts_root.mkdir(parents=True, exist_ok=True)
    guide_path = artifacts_root / "WLT_crypto_wallets_guide.md"
    guide_path.write_text(
        "# Beginner's Guide to Crypto Wallets\n\nIntro paragraph...\n"
    )

    goal = Goal(
        id="LVA-G-001",
        project_id=project.id,
        description="Produce the guide",
        success_criteria="guide exists",
        status=GoalStatus.IN_PROGRESS,
    )
    store.save_goal(project.code, goal)
    task = Task(
        id="LVA-T-001",
        project_id=project.id,
        goal_id=goal.id,
        description="Draft the guide",
        output_path="WLT_crypto_wallets_guide.md",
        status=TaskStatus.COMPLETED,
    )
    store.save_task(project.code, task)

    orch, captured = _capturing_orch(project)
    summary = RunSummary(project=project)
    orch._leader_verify_goal(goal, [task], summary)

    assert len(captured) == 1
    prompt = captured[0]
    assert "WLT_crypto_wallets_guide.md" in prompt, (
        "Leader prompt must reference the artifact at task.output_path "
        "(it lives at artifacts/<output_path>, not drafts/<task-id>.md)"
    )


def test_leader_verify_includes_artifact_content_in_prompt(project: Project, tmp_path: Path):
    """Path discovery alone isn't enough — Leader needs actual content
    to evaluate quality. The verify prompt must include a snippet of
    each completed task's artifact body, not just the path."""
    artifacts_root = tmp_path / PROJECT_CODE.lower() / "artifacts"
    artifacts_root.mkdir(parents=True, exist_ok=True)
    guide_path = artifacts_root / "guide.md"
    guide_body = (
        "# The Guide Title\n\n"
        "Distinctive sentence the Leader can recognize: "
        "BUDGETARY-SENTINEL-XYZ-12345.\n\n"
        "Section content...\n"
    )
    guide_path.write_text(guide_body)

    goal = Goal(
        id="LVA-G-002",
        project_id=project.id,
        description="Produce the guide",
        success_criteria="guide exists",
        status=GoalStatus.IN_PROGRESS,
    )
    store.save_goal(project.code, goal)
    task = Task(
        id="LVA-T-002",
        project_id=project.id,
        goal_id=goal.id,
        description="Draft the guide",
        output_path="guide.md",
        status=TaskStatus.COMPLETED,
    )
    store.save_task(project.code, task)

    orch, captured = _capturing_orch(project)
    summary = RunSummary(project=project)
    orch._leader_verify_goal(goal, [task], summary)

    prompt = captured[0]
    assert "BUDGETARY-SENTINEL-XYZ-12345" in prompt, (
        "Leader prompt must include the artifact's actual body so "
        "the Leader can evaluate quality, not just file existence."
    )


def test_leader_verify_falls_back_to_drafts_convention(project: Project, tmp_path: Path):
    """When a task has no output_path, the legacy drafts/ convention
    still works (back-compat for older roster fixtures)."""
    artifacts_root = tmp_path / PROJECT_CODE.lower() / "artifacts"
    drafts_dir = artifacts_root / "drafts"
    drafts_dir.mkdir(parents=True, exist_ok=True)
    legacy_path = drafts_dir / "lva-t-003.md"
    legacy_path.write_text("Legacy draft body — DRAFTS-FALLBACK-MARKER-789.\n")

    goal = Goal(
        id="LVA-G-003",
        project_id=project.id,
        description="Legacy goal",
        success_criteria="ok",
        status=GoalStatus.IN_PROGRESS,
    )
    store.save_goal(project.code, goal)
    task = Task(
        id="LVA-T-003",
        project_id=project.id,
        goal_id=goal.id,
        description="Legacy task",
        output_path="",  # ← no output_path → fall back to drafts/
        status=TaskStatus.COMPLETED,
    )
    store.save_task(project.code, task)

    orch, captured = _capturing_orch(project)
    summary = RunSummary(project=project)
    orch._leader_verify_goal(goal, [task], summary)

    prompt = captured[0]
    assert "DRAFTS-FALLBACK-MARKER-789" in prompt


def test_leader_verify_registry_run_shell_widened_to_run_dir(
    project: Project, tmp_path: Path, monkeypatch
):
    """B5: the Leader-verify registry's ``run_shell`` is rooted at the whole RUN
    dir (``_scope_root()``), not just ``artifacts/`` — so the trusted Leader-
    reviewer can ``ls``/``cat`` the entire harness (logs, reports, tickets) for
    its verdict, while producers stay confined to staging. READ widens; the
    verify loadout (``run_shell``, passive) keeps WRITE in its lane."""
    from modulatio import tools

    seen_roots: list[Path] = []
    real = tools.make_run_shell
    monkeypatch.setattr(
        tools, "make_run_shell",
        lambda root, *a, **k: (seen_roots.append(Path(root)), real(root, *a, **k))[1],
    )

    orch, _ = _capturing_orch(project)
    registry = orch._leader_verify_tool_registry()

    assert "run_shell" in registry
    assert seen_roots[-1] == orch._scope_root()
    assert seen_roots[-1] != orch._artifacts_root(), (
        "verify run_shell must be widened beyond artifacts/ to the run dir"
    )


def test_leader_verify_chat_loop_widens_registry_and_grants_run_dir(
    project: Project, tmp_path: Path, monkeypatch
):
    """B5 wiring (both seats, same scope): when leader-verify routes through the
    tool-using chat loop, (a) a litellm leader's registry is the run-dir-widened
    one (a different, wider-rooted ``run_shell``), and (b) the Clay seat is
    granted the run dir so a claude -p reviewer sees the harness — Clay treated
    like any model, writes still gated. Both thread-local hints restored after."""
    from types import SimpleNamespace

    from modulatio import tools

    artifacts_root = tmp_path / PROJECT_CODE.lower() / "artifacts"
    artifacts_root.mkdir(parents=True, exist_ok=True)
    (artifacts_root / "doc.md").write_text("# Doc\n\nbody\n")

    goal = Goal(
        id="LVA-G-009", project_id=project.id, description="d",
        success_criteria="c", status=GoalStatus.IN_PROGRESS,
    )
    store.save_goal(project.code, goal)
    task = Task(
        id="LVA-T-009", project_id=project.id, goal_id=goal.id,
        description="t", output_path="doc.md", status=TaskStatus.COMPLETED,
    )
    store.save_task(project.code, task)

    orch, _ = _capturing_orch(project)
    orch.tool_registry = tools.build_registry(
        artifacts_root=artifacts_root, project_code=project.code,
    )
    monkeypatch.setattr(
        orch, "_leader_verify_tool_loadout_skill",
        lambda: SimpleNamespace(
            tool_loadout=["run_shell"], prompt_template="", name="leader-verify",
            needs_network=False, pass_env=(),
        ),
    )
    monkeypatch.setattr(
        orch, "_resolve_chat_runner", lambda role: (lambda *a, **k: ""),
    )

    seen: dict = {}

    def _fake_loop(**kwargs):
        seen["registry"] = orch._active_tool_registry()  # capture mid-loop
        seen["grants"] = getattr(orch._tls, "seat_extra_grants", None)
        return (
            "```json\n"
            + json.dumps(
                {"verdict": "satisfied", "rationale": "ok", "report_body": "ok"}
            )
            + "\n```"
        )

    monkeypatch.setattr(orch, "_run_chat_loop", _fake_loop)

    summary = RunSummary(project=project)
    orch._leader_verify_goal(goal, [task], summary)

    assert "registry" in seen, "verify did not route through the chat loop"
    # (a) litellm seat: run_shell widened beyond the base (artifacts-rooted) one.
    assert "run_shell" in seen["registry"]
    assert seen["registry"]["run_shell"] is not orch.tool_registry["run_shell"], (
        "the chat loop must run under the widened override, not the base registry"
    )
    # (b) Clay seat: the run dir is granted so claude -p can see the harness.
    assert seen["grants"] and str(orch._scope_root()) in seen["grants"], (
        "the Clay verify seat must be granted the run dir"
    )
    # both restored
    assert getattr(orch._tls, "tool_registry_override", None) is None
    assert getattr(orch._tls, "seat_extra_grants", None) is None


def test_seat_context_folds_in_tls_extra_grants(project: Project, monkeypatch):
    """``_seat_context`` folds a thread-local ``seat_extra_grants`` hint into the
    grants it hands the Clay seat — additively, on top of the operator-widen
    gate's roots. This is the seam leader-verify uses to grant the run dir."""
    import contextlib

    from modulatio import claude_cli

    seen: dict = {}

    def _spy(ws, grants, **kw):
        seen["grants"] = grants
        return contextlib.nullcontext()

    monkeypatch.setattr(claude_cli, "seat_context", _spy)

    orch, _ = _capturing_orch(project)
    orch._tls.seat_extra_grants = ("/some/run/dir",)
    with orch._seat_context():
        pass

    assert "/some/run/dir" in seen["grants"]


def test_extract_json_resilient_parses_retries_and_gives_up():
    """The shared resilient-JSON helper: parse on the first try; on a parse
    failure retry ONCE with a correction appended; return None when both fail."""
    from modulatio.orchestration import _LEADER_JSON_CORRECTION, _extract_json_resilient

    seen: list = []
    assert _extract_json_resilient(
        lambda c: seen.append(c) or '{"v": 1}', context="t"
    ) == {"v": 1}
    assert seen == [""]  # parsed first try, no correction

    seen2: list = []

    def two(corr):
        seen2.append(corr)
        return "no json here" if corr == "" else '{"v": 2}'

    assert _extract_json_resilient(two, context="t") == {"v": 2}
    assert seen2 == ["", _LEADER_JSON_CORRECTION]  # retried with the correction

    assert _extract_json_resilient(lambda c: "never json", context="t") is None


def test_leader_verify_retries_an_unparseable_verdict(project: Project, tmp_path: Path):
    """An unparseable first verdict (Clay broke the JSON) is retried once with a
    strict correction and recovers — instead of spuriously settling the goal as
    'verdict unparseable' (the live 0.9.8.5 Clay-leader failure)."""
    artifacts_root = tmp_path / PROJECT_CODE.lower() / "artifacts"
    artifacts_root.mkdir(parents=True, exist_ok=True)
    (artifacts_root / "doc.md").write_text("# Doc\n\nbody\n")

    goal = Goal(
        id="LVA-G-RETRY", project_id=project.id, description="d",
        success_criteria="c", status=GoalStatus.IN_PROGRESS,
    )
    store.save_goal(project.code, goal)
    task = Task(
        id="LVA-T-RETRY", project_id=project.id, goal_id=goal.id,
        description="t", output_path="doc.md", status=TaskStatus.COMPLETED,
    )
    store.save_task(project.code, task)

    calls = {"n": 0}

    def _leader(prompt: str) -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            return "Looks complete to me, but here's a reply with no JSON object."
        return "```json\n" + json.dumps({
            "verdict": "satisfied", "rationale": "ok", "report_body": "good",
        }) + "\n```"

    runners = {
        "leader": _leader, "planner": lambda p: "```json\n[]\n```",
        "drafter": lambda p: "", "qc": lambda p: "",
    }
    orch = Orchestrator(project, runners)
    summary = RunSummary(project=project)
    orch._leader_verify_goal(goal, [task], summary)

    assert calls["n"] == 2, "should have retried the unparseable verdict once"
    assert not any("unparseable" in e for e in summary.errors), (
        "the retry's valid verdict should drive the outcome, not a settle-as-failed"
    )
    assert goal.status == GoalStatus.COMPLETED


def test_split_leader_report_body_extracts_section_after_header():
    """De-fragilize helper: the human report rides as a ``## Product Quality
    Report`` section after the verdict JSON; the helper returns the prose that
    follows that header."""
    from modulatio.orchestration import _split_leader_report_body

    raw = (
        '```json\n{"verdict": "satisfied"}\n```\n\n'
        "## Product Quality Report\n\n"
        "The team delivered a solid guide. It covers X, Y, Z.\n"
    )
    assert _split_leader_report_body(raw) == (
        "The team delivered a solid guide. It covers X, Y, Z."
    )


def test_split_leader_report_body_tolerates_bold_and_alt_heading():
    """Match the section whether the Leader headed it ``##``, ``#``, or **bold**."""
    from modulatio.orchestration import _split_leader_report_body

    assert _split_leader_report_body("**Product Quality Report**\nbody here") == "body here"
    assert _split_leader_report_body("# Product Quality Report\nbody here") == "body here"


def test_split_leader_report_body_missing_or_inline_mention_returns_empty():
    """No header → empty. An INLINE mention (not a heading line) must NOT be
    mistaken for the section start."""
    from modulatio.orchestration import _split_leader_report_body

    assert _split_leader_report_body("") == ""
    assert _split_leader_report_body("no header at all") == ""
    assert _split_leader_report_body(
        "See the Product Quality Report below for details."
    ) == ""


def test_leader_verify_report_rides_outside_the_verdict_json(project: Project, tmp_path: Path):
    """De-fragilize: the long human-facing report rides as a ``## Product Quality
    Report`` section AFTER the verdict JSON, not inside it — so prose with quotes
    and literal newlines that would break an inlined JSON field can't fail the
    verdict parse. The report still reaches the goal report artifact."""
    artifacts_root = tmp_path / PROJECT_CODE.lower() / "artifacts"
    artifacts_root.mkdir(parents=True, exist_ok=True)
    (artifacts_root / "doc.md").write_text("# Doc\n\nbody\n")

    goal = Goal(
        id="LVA-G-PQR", project_id=project.id, description="d",
        success_criteria="c", status=GoalStatus.IN_PROGRESS,
    )
    store.save_goal(project.code, goal)
    task = Task(
        id="LVA-T-PQR", project_id=project.id, goal_id=goal.id,
        description="t", output_path="doc.md", status=TaskStatus.COMPLETED,
    )
    store.save_task(project.code, task)

    # Prose that WOULD break JSON if inlined: literal newlines + unescaped quotes.
    report_prose = (
        'The team delivered a "complete" guide.\n'
        'It spans paragraphs — with "quotes" and\nliteral newlines.'
    )

    def _leader(prompt: str) -> str:
        # Verdict JSON carries ONLY the short structured fields — no report_body.
        return (
            "```json\n"
            + json.dumps(
                {"verdict": "satisfied", "rationale": "ok", "recommendations": []}
            )
            + "\n```\n\n## Product Quality Report\n\n"
            + report_prose
        )

    runners = {
        "leader": _leader, "planner": lambda p: "```json\n[]\n```",
        "drafter": lambda p: "", "qc": lambda p: "",
    }
    orch = Orchestrator(project, runners)
    summary = RunSummary(project=project)
    orch._leader_verify_goal(goal, [task], summary)

    assert goal.status == GoalStatus.COMPLETED
    assert not any("unparseable" in e for e in summary.errors), summary.errors
    report = (
        tmp_path / PROJECT_CODE.lower() / "reports" / "LVA-G-PQR.md"
    ).read_text()
    assert 'with "quotes" and' in report, (
        "the report body that rode outside the JSON must reach the report artifact"
    )


def _disappointed_orch(project: Project):
    """Orchestrator whose Leader always returns a 'disappointed' verdict."""
    calls: list[str] = []
    def _leader(prompt: str) -> str:
        calls.append(prompt)
        return "```json\n" + json.dumps({
            "verdict": "disappointed",
            "rationale": "the doc only covers events through 2024",
            "report": "r",
        }) + "\n```"
    runners = {
        "leader": _leader,
        "planner": lambda p: "```json\n[]\n```",
        "drafter": lambda p: "",
        "qc": lambda p: "",
        "researcher": lambda p: "",
    }
    return Orchestrator(project, runners), calls


def test_leader_verify_deadlock_bows_out_on_qc_authored(project: Project, tmp_path: Path):
    """fix-is-final + deadlock guard (2026-05-31): when QC already authored the
    fix (producer exhausted) AND a redo already happened, the Leader bows out
    and ships with a reservation instead of grinding the retry budget."""
    artifacts_root = tmp_path / PROJECT_CODE.lower() / "artifacts"
    artifacts_root.mkdir(parents=True, exist_ok=True)
    (artifacts_root / "doc.md").write_text("# Doc\n\nbody\n")

    goal = Goal(
        id="LVA-G-002", project_id=project.id,
        description="Produce a current summary", success_criteria="current",
        status=GoalStatus.IN_PROGRESS,
        retry_count=1,  # already redone once today…
        retry_count_date=__import__("datetime").date.today(),  # …kept past daily refresh
    )
    store.save_goal(project.code, goal)
    task = Task(
        id="LVA-T-002", project_id=project.id, goal_id=goal.id,
        description="Draft", output_path="doc.md",
        status=TaskStatus.COMPLETED, qc_authored_fix=True,  # QC had to fix it
    )
    store.save_task(project.code, task)

    orch, calls = _disappointed_orch(project)
    summary = RunSummary(project=project)
    orch._leader_verify_goal(goal, [task], summary)

    # bowed out: Leader called once (NO redo recursion), goal completed,
    # the deadlock reservation recorded for the human.
    assert len(calls) == 1, "should not have auto-redone (no recursion)"
    assert goal.status == GoalStatus.COMPLETED
    assert any(
        "limit of what it could verify" in r.get("concern", "")
        for r in summary.recommendations
    ), summary.recommendations


def test_leader_verify_no_midrun_budget_reset_on_date_roll(project: Project, tmp_path: Path):
    """HARD INVARIANT (2026-05-31): an infinite redo is not a possibility.
    A stale retry_count_date (a run that crossed midnight) must NOT reset the
    in-run budget and hand out fresh redos — the cap is absolute. Goal at
    max_retries with YESTERDAY's date + disappointed → ships with a reservation,
    does NOT redo. (Before the fix, the in-run daily refresh reset retry_count
    to 0 here and the loop ground on.)"""
    import datetime
    artifacts_root = tmp_path / PROJECT_CODE.lower() / "artifacts"
    artifacts_root.mkdir(parents=True, exist_ok=True)
    (artifacts_root / "doc.md").write_text("# Doc\n\nbody\n")

    yesterday = datetime.date.today() - datetime.timedelta(days=1)
    goal = Goal(
        id="LVA-G-003", project_id=project.id,
        description="Produce a current summary", success_criteria="current",
        status=GoalStatus.IN_PROGRESS,
        retry_count=4, max_retries=4,          # budget already exhausted this run
        retry_count_date=yesterday,            # …and the clock rolled over
    )
    store.save_goal(project.code, goal)
    task = Task(
        id="LVA-T-003", project_id=project.id, goal_id=goal.id,
        description="Draft", output_path="doc.md",
        status=TaskStatus.COMPLETED, qc_authored_fix=False,  # not a qc deadlock — pure budget cap
    )
    store.save_task(project.code, task)

    orch, calls = _disappointed_orch(project)
    summary = RunSummary(project=project)
    orch._leader_verify_goal(goal, [task], summary)

    assert len(calls) == 1, "stale date must NOT grant a fresh redo (no recursion)"
    assert goal.status == GoalStatus.COMPLETED
    assert goal.retry_count == 4, "retry_count must NOT have been reset by the date roll"
    assert any(
        "could not fully satisfy" in r.get("concern", "")
        for r in summary.recommendations
    ), summary.recommendations
