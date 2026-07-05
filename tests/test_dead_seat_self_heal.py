"""ACCEPTANCE: the 2026-07-04 dead-seat run, replayed — and it self-heals.

Live run 20260704T081413Z: a crashed LM Studio model (seat "james") burned
retry budgets in ~1s, stayed "best-available" the whole outage, cascade-blocked
the goal's assembler, and the run shipped "disappointed over a hole" while QC
sat idle. The operator's bar for this arc: this exact failure shape, replayed,
ends SATISFIED with ZERO human touches — the router catches the dead seat AND
QC produces the missing pieces. These tests are that bar, encoded.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from litellm.exceptions import BadRequestError

from modulatio import roster, store, vault
from modulatio import orchestration as orch_mod
from modulatio.orchestration import Orchestrator
from modulatio.types import GoalStatus, Project, TaskStatus

PROJECT_CODE = "DSH"

_LM_STUDIO_CRASH = (
    "Error code: 400 - {'error': 'The model has crashed without additional "
    "information. (Exit code: null)'}"
)


@pytest.fixture
def project(tmp_path: Path, monkeypatch) -> Project:
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    monkeypatch.setenv("MODULATIO_CONCURRENT_WAVES", "1")
    # zero the availability backoff so the replay runs in test time
    monkeypatch.setattr(
        orch_mod, "_AVAILABILITY_RETRY_BACKOFF_S", (0.0, 0.0, 0.0))
    vault.init_project(PROJECT_CODE, "dead-seat fixture", "obj")
    return Project(
        code=PROJECT_CODE, name="dead-seat fixture", objective="obj",
        leader_model="stub",
        wiki_path=str(vault.project_dir(PROJECT_CODE)),
    )


def _seed_seats() -> None:
    """The live run's roster shape: two producers (one about to die), QC,
    leader. james's model key routes to the per-agent DEAD runner; olivia's
    'stub' model falls back to the role drafter runner."""
    roster.save(roster.Agent(
        id="james", name="James", identity="James id",
        model="lmstudio/dead", tier="producer", capacity_cap=1), PROJECT_CODE)
    roster.save(roster.Agent(
        id="olivia", name="Olivia", identity="Olivia id",
        model="stub", tier="producer", capacity_cap=1), PROJECT_CODE)


def _leader(prompt: str) -> str:
    """Decompose → one goal with the live run's dep chain; verify → satisfied
    only when the assembled deliverable actually exists on the scan."""
    if "LEADER GOAL VERIFICATION" in prompt:
        whole = "assembled_report.md" in prompt
        payload = {
            "verdict": "satisfied" if whole else "disappointed",
            "rationale": "the assembly exists" if whole else "assembly missing",
            "report_body": "## Goal Report\n\nscripted.\n",
        }
        return f"```json\n{json.dumps(payload)}\n```"
    goals = [{
        "description": "Research two topics and assemble the report",
        "success_criteria": "assembled_report.md exists and covers both",
        "evidence_required": [
            {"kind": "artifact", "description": "assembled report"}],
    }]
    return f"```json\n{json.dumps(goals)}\n```"


def _planner(prompt: str) -> str:
    tasks = [
        {"description": "Research topic A", "assignee_specialist": "drafter",
         "artifact_kind": "essay", "depends_on": [],
         "evidence_required": [{"kind": "artifact", "description": "notes"}]},
        {"description": "Research topic B", "assignee_specialist": "drafter",
         "artifact_kind": "essay", "depends_on": [],
         "evidence_required": [{"kind": "artifact", "description": "notes"}]},
        {"description": "Assemble both into the report",
         "assignee_specialist": "drafter", "artifact_kind": "essay",
         "depends_on": [0, 1], "deliverable": True,
         "output_path": "assembled_report.md",
         "evidence_required": [{"kind": "artifact", "description": "report"}]},
    ]
    return f"```json\n{json.dumps(tasks)}\n```"


def _drafter(prompt: str) -> str:
    return "PRODUCED-BY-OLIVIA " + " ".join(["word"] * 250)


def _qc(prompt: str) -> str:
    """Verdict JSON on review calls; artifact TEXT on the last-resort
    patch/build calls (the rescue prompts carry their own headers)."""
    if "LAST-RESORT rescue" in prompt:
        return "QC-AUTHORED-PIECE " + " ".join(["word"] * 250)
    verdict = {"check": "artifact exists and is substantive", "passed": True}
    return f"```json\n{json.dumps(verdict)}\n```"


def _dead_james(prompt: str) -> str:
    raise BadRequestError(
        message=_LM_STUDIO_CRASH, model="lmstudio/dead", llm_provider="openai")


def _run(project: Project) -> tuple[Orchestrator, object]:
    _seed_seats()
    orch = Orchestrator(
        project,
        {"leader": _leader, "planner": _planner,
         "drafter": _drafter, "qc": _qc},
        agent_runners={"lmstudio/dead": _dead_james},
    )
    summary = orch.kickoff("research two topics, assemble the report")
    return orch, summary


def test_dead_seat_run_ends_satisfied_with_zero_touches(project: Project):
    """THE BAR: the dead seat's tasks heal via the QC backstop, the assembler
    lands fed by real pieces, the goal verifies satisfied, nothing stays
    blocked, the dead seat cooled — zero human touches by construction."""
    orch, summary = _run(project)

    tasks = store.list_tasks(PROJECT_CODE, run_id=project.run_id)
    assert tasks, "the plan landed tasks"
    assert all(t.status is TaskStatus.COMPLETED for t in tasks), (
        [(t.id, t.status) for t in tasks])

    goals = store.list_goals(PROJECT_CODE, run_id=project.run_id)
    assert all(g.status is GoalStatus.COMPLETED for g in goals)
    assert summary.verdicts, "the Leader signed off"
    assert summary.verdicts[-1]["verdict"] == "satisfied"

    # every task that hit the dead seat healed via QC authorship
    dead_seat_tasks = [
        t.id for t in tasks if t.assigned_agent_id == "james"]
    for tid in dead_seat_tasks:
        assert tid in summary.qc_authored_fixes, (
            f"{tid} ran on the dead seat and must have been QC-rescued")

    # the deliverable exists and was assembled from real content
    report = (Path(vault.project_dir(PROJECT_CODE)) / "artifacts"
              / "assembled_report.md")
    assert report.exists()
    body = report.read_text()
    assert "PRODUCED-BY-OLIVIA" in body or "QC-AUTHORED-PIECE" in body

    # the dead seat went into cooldown (dispatch stopped feeding it)
    assert "james" in orch._seat_unavailable_until

    # zero blocked, zero tickets demanding a human
    assert summary.errors is not None  # errors may exist — but nothing BLOCKED
    assert not [t for t in tasks if t.status is TaskStatus.BLOCKED]


def test_logic_bug_producer_is_out_produced_by_the_sweep(project: Project):
    """Variant: a producer with a genuine LOGIC bug (not availability) blocks
    its task; the dependent cascade-blocks. The goal-end QC sweep builds the
    missing pieces in dep order and the goal still lands satisfied."""
    _seed_seats()

    def _buggy_drafter(prompt: str) -> str:
        raise ValueError("template placeholder missing")  # never backstopped in-loop

    orch = Orchestrator(
        project,
        {"leader": _leader, "planner": _planner,
         "drafter": _buggy_drafter, "qc": _qc},
        agent_runners={"lmstudio/dead": _buggy_drafter},
    )
    summary = orch.kickoff("research two topics, assemble the report")

    tasks = store.list_tasks(PROJECT_CODE, run_id=project.run_id)
    assert all(t.status is TaskStatus.COMPLETED for t in tasks), (
        [(t.id, t.status) for t in tasks])
    assert summary.verdicts[-1]["verdict"] == "satisfied"
    assert len(summary.qc_authored_fixes) == len(tasks), (
        "every piece was QC-produced — the producer never landed one")
    report = (Path(vault.project_dir(PROJECT_CODE)) / "artifacts"
              / "assembled_report.md")
    assert report.exists()
    assert "QC-AUTHORED-PIECE" in report.read_text()


def test_qc_down_too_ships_with_reservations_not_a_hang(project: Project):
    """Variant: QC's model is ALSO dead — no belt left. The run must end
    gracefully (goal terminal, reservations recorded), never hang or crash."""
    _seed_seats()

    def _dead_everything(prompt: str) -> str:
        raise BadRequestError(
            message=_LM_STUDIO_CRASH, model="m", llm_provider="openai")

    orch = Orchestrator(
        project,
        {"leader": _leader, "planner": _planner,
         "drafter": _dead_everything, "qc": _dead_everything},
        agent_runners={"lmstudio/dead": _dead_everything},
    )
    summary = orch.kickoff("research two topics, assemble the report")

    goals = store.list_goals(PROJECT_CODE, run_id=project.run_id)
    assert goals and all(g.status is GoalStatus.COMPLETED for g in goals), (
        "the goal reaches a terminal state even with every model down")
    assert summary.recommendations or summary.errors, (
        "the failure is surfaced, not swallowed")


def test_wedged_seat_releases_at_the_deadline_and_the_run_heals(
    project: Project, monkeypatch,
):
    """ACCEPTANCE (the cb6c0d shape): a producer whose completion NEVER
    returns — a spin no transport timeout reaches — is released by the hard
    kill-boundary at the deadline instead of 17 minutes, the recovery train
    lands the work, the run ends SATISFIED, and the wedge leaves a stack
    dump in the crash log (the evidence Stage 0 existed to capture)."""
    import time as _time

    from modulatio import logstore
    from modulatio.runners import _hard_deadline

    monkeypatch.setenv("MODULATIO_CRASH_DIR", str(
        Path(vault.VAULT_ROOT) / "crashes"))
    monkeypatch.setattr("modulatio.runners._HARD_DEADLINE_GRACE_S", 0.0)
    monkeypatch.setattr(
        orch_mod, "_AVAILABILITY_RETRY_BACKOFF_S", (0.0, 0.0, 0.0))
    _seed_seats()

    def _spinning_forever(prompt: str) -> str:
        _time.sleep(120)          # far past the test deadline — "never" returns
        return "too late"

    wedged = _hard_deadline(
        _spinning_forever, timeout_s=0.2, describe="chat lmstudio/dead")

    orch = Orchestrator(
        project,
        {"leader": _leader, "planner": _planner,
         "drafter": _drafter, "qc": _qc},
        agent_runners={"lmstudio/dead": wedged},
    )
    start = _time.monotonic()
    summary = orch.kickoff("research two topics, assemble the report")
    elapsed = _time.monotonic() - start

    tasks = store.list_tasks(PROJECT_CODE, run_id=project.run_id)
    assert all(t.status is TaskStatus.COMPLETED for t in tasks), (
        [(t.id, t.status) for t in tasks])
    assert summary.verdicts[-1]["verdict"] == "satisfied"
    assert elapsed < 60, f"the wedge must not hold the run ({elapsed:.0f}s)"
    # the wedge left its evidence
    dumps = [e for e in logstore.list_logs() if "hard-timeout" in e.summary]
    assert dumps, "the kill-boundary must dump the wedged stacks"
    assert "lmstudio/dead" in dumps[0].path.read_text()
