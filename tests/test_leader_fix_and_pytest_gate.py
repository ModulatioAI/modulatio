# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""#43 pytest evidence gate + the Leader fix-in-place default.

Engine-run pytest is the test-suite EVIDENCE step for CODE goals: RED joins
``goal_spec_issues`` so the verdict clamp binds it as a measured HARD
violation — a code goal cannot be waved through without a recorded green run.

A 'disappointed' goal's default remediation is the LEADER patching the
deliverable in place with its own hands (no floor push); the producer
re-dispatch survives as ``MODULATIO_GOAL_REDO_ACTOR=floor`` and as the
fallback when the fix lane has no chat runner / write tools.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

from modulatio import store, vault
from modulatio.orchestration import Orchestrator
from modulatio.types import GoalStatus, Project, Task

from tests.test_orchestration import (
    _drafter_stub,
    _leader_stub,
    _planner_stub,
    _qc_stub,
)

PROJECT_CODE = "LFX"


@pytest.fixture
def project(tmp_path: Path, monkeypatch) -> Project:
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    vault.init_project(PROJECT_CODE, "leader fix", "fix goals in place")
    return Project(
        code=PROJECT_CODE, name="leader fix", objective="fix goals in place",
        leader_model="stub", wiki_path=str(tmp_path / PROJECT_CODE.lower()),
    )


@pytest.fixture
def project_with_run(tmp_path: Path, monkeypatch) -> Project:
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    vault.init_project(PROJECT_CODE, "leader fix", "fix goals in place")
    run_id = "run-lfx-001"
    vault.init_run(PROJECT_CODE, run_id, "fix goals in place")
    return Project(
        code=PROJECT_CODE, name="leader fix", objective="fix goals in place",
        leader_model="stub", wiki_path=str(tmp_path / PROJECT_CODE.lower()),
        run_id=run_id,
    )


def _orch(project: Project) -> Orchestrator:
    runner = lambda prompt: "stub"  # noqa: E731 — test stub
    return Orchestrator(project, runners=dict.fromkeys(
        ("leader", "planner", "drafter", "researcher", "qc"), runner))


def _code_task() -> Task:
    return Task(id="LFX-T-001", project_id=uuid4(), goal_id="LFX-G-001",
                description="build the app", artifact_kind="application")


def _text_task() -> Task:
    return Task(id="LFX-T-002", project_id=uuid4(), goal_id="LFX-G-001",
                description="write prose", artifact_kind="text")


# ---------------------------------------------------------------- pytest gate

def test_pytest_gate_skips_non_code_and_missing_repo(project_with_run):
    orch = _orch(project_with_run)
    # No code deliverable → no gate.
    assert orch._goal_pytest_gate([_text_task()]) is None
    # Code deliverable but nothing on disk that looks like a pytest repo.
    assert orch._goal_pytest_gate([_code_task()]) is None


def test_pytest_gate_green_red_and_empty_suite(project_with_run):
    orch = _orch(project_with_run)
    root = orch._shared_artifacts_root()
    root.mkdir(parents=True, exist_ok=True)
    (root / "pyproject.toml").write_text(
        '[project]\nname = "lfx-app"\nversion = "0.0.1"\n', encoding="utf-8")

    # Marker present but NO tests collected → RED ("no green evidence").
    empty = orch._goal_pytest_gate([_code_task()])
    assert empty is not None and empty[0] is False

    tests_dir = root / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_ok.py").write_text(
        "def test_ok():\n    assert True\n", encoding="utf-8")
    green = orch._goal_pytest_gate([_code_task()])
    assert green is not None
    assert green[0] is True
    assert "engine-run pytest" in green[1]

    (tests_dir / "test_bad.py").write_text(
        "def test_bad():\n    assert False\n", encoding="utf-8")
    red = orch._goal_pytest_gate([_code_task()])
    assert red is not None
    assert red[0] is False
    assert "test_bad" in red[1]


def test_red_pytest_clamps_satisfied_verdict(project, monkeypatch):
    """The Leader cannot wave a code goal through over a RED suite: the
    engine-measured failure joins goal_spec_issues and the verdict clamp
    forces 'disappointed' (here with a zero redo budget → honest settle)."""
    monkeypatch.setenv("MODULATIO_GOAL_MAX_RETRIES", "0")
    monkeypatch.setattr(
        Orchestrator, "_goal_pytest_gate",
        lambda self, tasks: (False, "engine-run pytest — exit 1\n1 failed"),
    )

    def _leader_satisfied(prompt: str) -> str:
        if "LEADER GOAL VERIFICATION" in prompt:
            payload = {
                "verdict": "satisfied",
                "rationale": "looks fine to me",
                "report_body": "## Report\n\nShip it.\n",
            }
            return f"```json\n{json.dumps(payload)}\n```"
        return _leader_stub(prompt)

    runners = {
        "leader": _leader_satisfied,
        "planner": _planner_stub,
        "drafter": _drafter_stub,
        "qc": _qc_stub,
    }
    orch = Orchestrator(project, runners)
    summary = orch.kickoff("code goal with red suite")

    goals = store.list_goals(PROJECT_CODE)
    assert goals[0].status == GoalStatus.COMPLETED  # settles, never blocks
    assert any("clamped verdict satisfied→disappointed" in e
               for e in summary.errors)
    assert summary.verdicts[-1]["verdict"] == "disappointed"


# ------------------------------------------------- leader fix-in-place lane

def _progressive_leader(verdicts: list[str], counter: dict):
    def _leader(prompt: str) -> str:
        if "LEADER GOAL VERIFICATION" in prompt:
            v = verdicts[min(counter["n"], len(verdicts) - 1)]
            counter["n"] += 1
            payload = {
                "verdict": v,
                "rationale": f"attempt {counter['n']}: {v}",
                "report_body": f"## Report\n\nVerdict: {v}\n",
            }
            return f"```json\n{json.dumps(payload)}\n```"
        return _leader_stub(prompt)
    return _leader


def _wire_fix_lane(monkeypatch, fix_calls: list):
    """Give the stub Orchestrator a leader chat runner + write tools so the
    fix lane is available, and capture the fix chat-loop dispatch."""
    monkeypatch.setattr(
        Orchestrator, "_resolve_chat_runner", lambda self, agent_id: object())
    monkeypatch.setattr(
        Orchestrator, "_leader_verify_tool_loadout_skill", lambda self: None)
    monkeypatch.setattr(
        Orchestrator, "_leader_verify_tool_registry",
        lambda self: {"run_shell": object(), "read_file": object(),
                      "read_tool_result": object(), "edit_file": object(),
                      "write_artifact": object()},
    )

    def _fake_chat_loop(self, **kwargs):
        fix_calls.append(kwargs)
        return "patched the deliverable"

    monkeypatch.setattr(Orchestrator, "_run_chat_loop", _fake_chat_loop)


def test_leader_fix_in_place_is_default_no_floor_push(project, monkeypatch):
    """Disappointed → the LEADER fixes in place (one retry slot consumed,
    fix chat-loop dispatched) and the producers are NEVER re-dispatched."""
    fix_calls: list = []
    _wire_fix_lane(monkeypatch, fix_calls)
    counter = {"n": 0}
    drafter_calls = {"n": 0}

    def _drafter_counting(prompt: str) -> str:
        drafter_calls["n"] += 1
        return _drafter_stub(prompt)

    runners = {
        "leader": _progressive_leader(["disappointed", "satisfied"], counter),
        "planner": _planner_stub,
        "drafter": _drafter_counting,
        "qc": _qc_stub,
    }
    orch = Orchestrator(project, runners)
    orch.kickoff("fix it yourself")

    goals = store.list_goals(PROJECT_CODE)
    assert goals[0].status == GoalStatus.COMPLETED
    assert goals[0].retry_count == 1          # one fix cycle consumed
    assert counter["n"] == 2                  # verify → fix → re-verify
    assert len(fix_calls) == 1
    assert fix_calls[0]["skill_name"] == "leader-fix"
    assert "LEADER FIX-IN-PLACE" in fix_calls[0]["prompt"]
    assert drafter_calls["n"] == 3            # initial pass only — NO floor push


def test_goal_redo_actor_floor_restores_floor_push(project, monkeypatch):
    """MODULATIO_GOAL_REDO_ACTOR=floor → the pre-1.0 producer re-dispatch,
    even with a fix lane available."""
    monkeypatch.setenv("MODULATIO_GOAL_REDO_ACTOR", "floor")
    # Floor mode needs a producer budget to re-run tasks: with the shipped
    # default of 0 the lifetime budget is already spent after the first pass
    # and the redo falls to QC-as-fixer instead of the producers.
    monkeypatch.setenv("MODULATIO_TASK_MAX_RETRIES", "3")
    fix_calls: list = []
    _wire_fix_lane(monkeypatch, fix_calls)
    counter = {"n": 0}
    drafter_calls = {"n": 0}

    def _drafter_counting(prompt: str) -> str:
        drafter_calls["n"] += 1
        return _drafter_stub(prompt)

    runners = {
        "leader": _progressive_leader(["disappointed", "satisfied"], counter),
        "planner": _planner_stub,
        "drafter": _drafter_counting,
        "qc": _qc_stub,
    }
    orch = Orchestrator(project, runners)
    orch.kickoff("floor redo by choice")

    goals = store.list_goals(PROJECT_CODE)
    assert goals[0].status == GoalStatus.COMPLETED
    assert goals[0].retry_count == 1
    assert fix_calls == []                    # fix lane never dispatched
    assert drafter_calls["n"] == 6            # producers re-ran all 3 tasks


def test_fix_lane_unavailable_falls_back_to_floor(project, monkeypatch):
    """No leader chat runner (the bare stub Orchestrator) → the fix lane
    declines WITHOUT consuming budget and the floor redo converges the goal
    exactly as before."""
    monkeypatch.setenv("MODULATIO_TASK_MAX_RETRIES", "3")
    counter = {"n": 0}
    drafter_calls = {"n": 0}

    def _drafter_counting(prompt: str) -> str:
        drafter_calls["n"] += 1
        return _drafter_stub(prompt)

    runners = {
        "leader": _progressive_leader(["disappointed", "satisfied"], counter),
        "planner": _planner_stub,
        "drafter": _drafter_counting,
        "qc": _qc_stub,
    }
    orch = Orchestrator(project, runners)
    orch.kickoff("no chat runner")

    goals = store.list_goals(PROJECT_CODE)
    assert goals[0].status == GoalStatus.COMPLETED
    assert goals[0].retry_count == 1
    assert drafter_calls["n"] == 6            # floor redo re-ran all 3 tasks
